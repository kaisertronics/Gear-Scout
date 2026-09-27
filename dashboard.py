#!/usr/bin/env python3
"""
Gear Scout dashboard — a small status page + source manager that runs
alongside the scraper. Shares the same /data volume (SQLite DB, last-run
status, Facebook session) and /config volume (config.yaml) as the scout
service, so it always reflects the real current state.

Run with: python3 dashboard.py  (inside the container — see docker-compose.yml)
"""
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_sock import Sock
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parent))
from scrapers.dispatch import dispatch_scrape
from scrapers.facebook_marketplace_regions import FACEBOOK_MARKETPLACE_REGIONS
from scrapers.store import (
    count_mismatched,
    favorite_listings,
    purge_all,
    purge_mismatched,
    recent_listings,
    search_listings,
    set_favorite,
    set_tags,
    stats as db_stats,
)

app = Flask(__name__)
# A fresh secret each container start is fine — it just invalidates existing
# login sessions on restart, which simply means logging in again.
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
sock = Sock(app)

NOVNC_STATIC_DIR = "/usr/share/novnc"
VNC_TCP_PORT = 5900  # the x11vnc server started by facebook_login_service.py

CONFIG_PATH = Path("/config/config.yaml")
RUN_STATUS_PATH = Path("/data/last_run.json")
FB_SESSION_PATH = Path("/data/fb_session.json")
FB_LOGIN_STATUS_PATH = Path("/data/fb_login_status.json")
FB_LOGIN_SIGNAL_PATH = Path("/data/.fb_login_signal")
LIVE_SEARCH_STATUS_PATH = Path("/data/live_search_status.json")
MANUAL_SCRAPE_STATUS_PATH = Path("/data/manual_scrape_status.json")

_fb_login_process = None  # the running facebook_login_service.py subprocess, if any
_live_search_thread = None  # the running live-search background thread, if any
_manual_scrape_thread = None  # the running manual-scrape background thread, if any

yaml_rt = YAML()
yaml_rt.preserve_quotes = True
yaml_rt.width = 100
yaml_rt.indent(mapping=2, sequence=4, offset=2)

SOURCE_TYPES = ["html", "rss", "craigslist", "craigslist_region", "facebook", "facebook_marketplace_region", "reddit"]


def load_config_raw():
    with open(CONFIG_PATH) as f:
        return yaml_rt.load(f)


def save_config_raw(data):
    with open(CONFIG_PATH, "w") as f:
        yaml_rt.dump(data, f)


def load_run_status():
    if RUN_STATUS_PATH.exists():
        try:
            return json.loads(RUN_STATUS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def next_run_time(cfg: dict) -> str | None:
    """Best-effort next scheduled run time, for display only."""
    try:
        from apscheduler.triggers.cron import CronTrigger
        import zoneinfo

        sched = cfg.get("schedule", {})
        minute, hour, day, month, dow = sched.get("cron", "0 7 * * *").split()
        tz_name = sched.get("timezone", "UTC")
        trigger = CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=dow,
            timezone=tz_name,
        )
        now = datetime.now(zoneinfo.ZoneInfo(tz_name))
        nxt = trigger.get_next_fire_time(None, now)
        return nxt.strftime("%a %b %d, %I:%M %p %Z") if nxt else None
    except Exception:
        return None


def fb_session_status() -> dict:
    if not FB_SESSION_PATH.exists():
        return {"present": False}
    try:
        mtime = datetime.fromtimestamp(FB_SESSION_PATH.stat().st_mtime, tz=timezone.utc)
        age_days = (datetime.now(timezone.utc) - mtime).days
        return {"present": True, "saved_at": mtime.isoformat(), "age_days": age_days}
    except OSError:
        return {"present": False}


def _load_live_search_status() -> dict:
    if LIVE_SEARCH_STATUS_PATH.exists():
        try:
            return json.loads(LIVE_SEARCH_STATUS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "idle"}


def _write_live_search_status(status: dict):
    try:
        LIVE_SEARCH_STATUS_PATH.write_text(json.dumps(status))
    except OSError:
        pass


def _run_live_search_job(query: str):
    from scrapers.live_search import run_live_search

    def on_progress(done, total, current_source):
        _write_live_search_status({
            "state": "running",
            "query": query,
            "done": done,
            "total": total,
            "current_source": current_source,
        })

    try:
        cfg = load_config_raw()
        results = run_live_search(query, cfg, on_progress=on_progress)
        _write_live_search_status({
            "state": "done",
            "query": query,
            "matched_count": sum(len(r.listings) for r in results),
            "sources_ok": sum(1 for r in results if r.success),
            "sources_total": len(results),
        })
    except Exception as e:
        logging.exception("Live search failed")
        _write_live_search_status({"state": "failed", "query": query, "reason": str(e)})


def _load_manual_scrape_status() -> dict:
    if MANUAL_SCRAPE_STATUS_PATH.exists():
        try:
            return json.loads(MANUAL_SCRAPE_STATUS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "idle"}


def _write_manual_scrape_status(status: dict):
    try:
        MANUAL_SCRAPE_STATUS_PATH.write_text(json.dumps(status))
    except OSError:
        pass


def _run_manual_scrape_job():
    from scrapers.manual_scrape import run_manual_scrape

    def on_progress(done, total, current_source):
        _write_manual_scrape_status({
            "state": "running",
            "done": done,
            "total": total,
            "current_source": current_source,
        })

    try:
        cfg = load_config_raw()
        results, new_listings = run_manual_scrape(cfg, on_progress=on_progress)
        _write_manual_scrape_status({
            "state": "done",
            "new_count": len(new_listings),
            "sources_ok": sum(1 for r in results if r.success or r.blocked),
            "sources_total": len(results),
        })
    except Exception as e:
        logging.exception("Manual scrape failed")
        _write_manual_scrape_status({"state": "failed", "reason": str(e)})


@app.before_request
def require_login():
    # Disabled at the user's explicit request (after being told this leaves
    # the dashboard's config editor and Facebook-login browser trigger open
    # to anyone with the URL, including the public ngrok tunnel). The /login
    # route and template are left intact — delete this early return to
    # re-enable the gate.
    return None
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        cfg = load_config_raw()
        email_cfg = cfg.get("email", {})
        real_email = str(email_cfg.get("from", ""))
        real_password = str(email_cfg.get("password", ""))
        given_email = request.form.get("email", "")
        given_password = request.form.get("password", "")
        email_ok = bool(real_email) and secrets.compare_digest(given_email, real_email)
        password_ok = bool(real_password) and secrets.compare_digest(given_password, real_password)
        if email_ok and password_ok:
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Wrong email or password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def _group_by_source(listings: list[dict]) -> list[tuple]:
    """Group already-newest-first listings by source, preserving recency
    order within each group, and order the groups themselves by whichever
    source has the single most recent listing."""
    groups: dict[str, list[dict]] = {}
    for listing in listings:
        groups.setdefault(listing["source_name"], []).append(listing)
    # `listings` arrives newest-first, so the first listing seen for a given
    # source is already that source's most recent one.
    return sorted(groups.items(), key=lambda kv: kv[1][0]["first_seen"], reverse=True)


@app.route("/")
def index():
    cfg = load_config_raw()
    status = load_run_status()
    listings = recent_listings(limit=300)
    grouped_listings = _group_by_source(listings)
    fbm_regions = [{"name": name, "location_id": location_id} for name, location_id in FACEBOOK_MARKETPLACE_REGIONS]
    return render_template(
        "index.html",
        status=status,
        grouped_listings=grouped_listings,
        db_stats=db_stats(),
        next_run=next_run_time(cfg),
        fb_session=fb_session_status(),
        source_count=len(cfg.get("sources", [])),
        fbm_regions=fbm_regions,
    )


@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    global _manual_scrape_thread
    if _manual_scrape_thread is None or not _manual_scrape_thread.is_alive():
        _write_manual_scrape_status({"state": "running", "done": 0, "total": 0, "current_source": None})
        _manual_scrape_thread = threading.Thread(target=_run_manual_scrape_job, daemon=True)
        _manual_scrape_thread.start()
    return redirect(url_for("index"))


@app.route("/scrape/status")
def scrape_status():
    return jsonify(_load_manual_scrape_status())


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    listings = search_listings(q, limit=300) if q else []
    grouped_listings = _group_by_source(listings)
    ebay_api = load_config_raw().get("ebay_api") or {}
    return render_template(
        "search.html",
        q=q,
        grouped_listings=grouped_listings,
        result_count=len(listings),
        ebay_api_configured=bool(ebay_api.get("client_id") and ebay_api.get("client_secret")),
    )


@app.route("/search/live/start", methods=["POST"])
def search_live_start():
    global _live_search_thread
    query = request.form.get("q", "").strip()
    if not query:
        return redirect(url_for("search"))
    if _live_search_thread is None or not _live_search_thread.is_alive():
        _write_live_search_status({"state": "running", "query": query, "done": 0, "total": 0, "current_source": None})
        _live_search_thread = threading.Thread(target=_run_live_search_job, args=(query,), daemon=True)
        _live_search_thread.start()
    return redirect(url_for("search", q=query))


@app.route("/search/live/status")
def search_live_status():
    return jsonify(_load_live_search_status())


@app.route("/favorites")
def favorites():
    listings = favorite_listings(limit=300)
    grouped_listings = _group_by_source(listings)
    return render_template(
        "favorites.html",
        grouped_listings=grouped_listings,
        result_count=len(listings),
    )


@app.route("/listing/favorite", methods=["POST"])
def listing_favorite():
    global_id = request.form.get("global_id", "")
    favorite = request.form.get("favorite") == "1"
    if global_id:
        set_favorite(global_id, favorite)
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/listing/tags", methods=["POST"])
def listing_tags():
    global_id = request.form.get("global_id", "")
    tags_raw = request.form.get("tags", "")
    if global_id:
        set_tags(global_id, tags_raw.split(","))
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/sources")
def sources():
    cfg = load_config_raw()
    return render_template(
        "sources.html",
        sources=cfg.get("sources", []),
        source_types=SOURCE_TYPES,
        fb_session=fb_session_status(),
    )


@app.route("/settings")
def settings():
    cfg = load_config_raw()
    email_cfg = cfg.get("email", {})
    schedule_cfg = cfg.get("schedule", {})
    keywords = cfg.get("keywords", []) or []
    return render_template(
        "settings.html",
        email_cfg=email_cfg,
        schedule_cfg=schedule_cfg,
        keywords_text="\n".join(keywords),
        keyword_count=len(keywords),
        db_stats=db_stats(),
        mismatched_count=count_mismatched(keywords),
        ebay_client_id=(cfg.get("ebay_api") or {}).get("client_id", ""),
        ebay_has_secret=bool((cfg.get("ebay_api") or {}).get("client_secret")),
        saved=request.args.get("saved"),
    )


@app.route("/settings/ebay", methods=["POST"])
def settings_ebay():
    cfg = load_config_raw()
    ebay_cfg = cfg.setdefault("ebay_api", {})
    ebay_cfg["client_id"] = request.form.get("client_id", "").strip()
    # Same as the email app password: the field ships blank, so only a newly
    # typed secret replaces the saved one.
    new_secret = request.form.get("client_secret", "").strip()
    if new_secret:
        ebay_cfg["client_secret"] = new_secret
    if request.form.get("clear") == "1":
        ebay_cfg["client_id"] = ""
        ebay_cfg["client_secret"] = ""
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="ebay"))


@app.route("/settings/cleanup", methods=["POST"])
def settings_cleanup():
    cfg = load_config_raw()
    deleted = purge_mismatched(cfg.get("keywords", []) or [])
    return redirect(url_for("settings", saved=f"cleanup:{deleted}"))


@app.route("/settings/reset-data", methods=["POST"])
def settings_reset_data():
    deleted = purge_all()
    return redirect(url_for("settings", saved=f"reset:{deleted}"))


@app.route("/settings/email", methods=["POST"])
def settings_email():
    cfg = load_config_raw()
    email_cfg = cfg.setdefault("email", {})
    email_cfg["from"] = request.form.get("from", "").strip()
    email_cfg["to"] = request.form.get("to", "").strip()
    email_cfg["smtp_host"] = request.form.get("smtp_host", "").strip()
    try:
        email_cfg["smtp_port"] = int(request.form.get("smtp_port", "").strip())
    except ValueError:
        pass
    email_cfg["subject"] = request.form.get("subject", "").strip()
    # Only overwrite the saved app password if a new one was actually typed
    # — the field ships blank (not pre-filled) so this never round-trips
    # the real secret back into the page source.
    new_password = request.form.get("password", "").strip()
    if new_password:
        email_cfg["password"] = new_password

    try:
        email_cfg["max_listings_per_source"] = max(1, int(request.form.get("max_listings_per_source", "").strip()))
    except ValueError:
        pass
    try:
        email_cfg["max_total_listings"] = max(1, int(request.form.get("max_total_listings", "").strip()))
    except ValueError:
        pass
    email_cfg["dashboard_url"] = request.form.get("dashboard_url", "").strip() or "http://localhost:8420"

    schedule_cfg = cfg.setdefault("schedule", {})
    schedule_cfg["timezone"] = request.form.get("timezone", "").strip()
    cron = request.form.get("cron", "").strip()
    if cron:
        schedule_cfg["cron"] = cron

    save_config_raw(cfg)
    return redirect(url_for("settings", saved="email"))


@app.route("/settings/keywords", methods=["POST"])
def settings_keywords():
    cfg = load_config_raw()
    raw = request.form.get("keywords", "")
    new_keywords = [line.strip() for line in raw.splitlines() if line.strip()]
    cfg["keywords"] = new_keywords
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="keywords"))


@app.route("/sources/add", methods=["POST"])
def add_source():
    cfg = load_config_raw()

    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    stype = request.form.get("type", "html").strip()
    rss_url = request.form.get("rss_url", "").strip()

    if not name or not url:
        return redirect(url_for("sources"))

    new_source = {"name": name, "url": url, "type": stype, "enabled": True}
    if stype in ("rss", "ebay_rss") and rss_url:
        new_source["rss_url"] = rss_url

    cfg.setdefault("sources", []).append(new_source)
    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/toggle", methods=["POST"])
def toggle_source():
    name = request.form.get("name", "")
    cfg = load_config_raw()
    for s in cfg.get("sources", []):
        if s.get("name") == name:
            s["enabled"] = not s.get("enabled", True)
            break
    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/delete", methods=["POST"])
def delete_source():
    name = request.form.get("name", "")
    cfg = load_config_raw()
    cfg["sources"] = [s for s in cfg.get("sources", []) if s.get("name") != name]
    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/edit", methods=["POST"])
def edit_source():
    cfg = load_config_raw()
    original_name = request.form.get("original_name", "")
    new_name = request.form.get("name", "").strip()
    new_url = request.form.get("url", "").strip()
    new_type = request.form.get("type", "html").strip()
    new_rss_url = request.form.get("rss_url", "").strip()

    if not new_name or not new_url:
        return redirect(url_for("sources"))

    for s in cfg.get("sources", []):
        if s.get("name") == original_name:
            s["name"] = new_name
            s["url"] = new_url
            s["type"] = new_type
            if new_type in ("rss", "ebay_rss") and new_rss_url:
                s["rss_url"] = new_rss_url
            elif "rss_url" in s and new_type not in ("rss", "ebay_rss"):
                del s["rss_url"]
            break

    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/test", methods=["POST"])
def test_source():
    name = request.form.get("name", "")
    cfg = load_config_raw()
    source = next((s for s in cfg.get("sources", []) if s.get("name") == name), None)
    if not source:
        return jsonify({"success": False, "error": "Source not found — it may have just been renamed or removed."})

    start = time.time()
    try:
        result = dispatch_scrape(dict(source), cfg.get("keywords", []) or [], cfg)
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "duration": round(time.time() - start, 1),
        })

    if result is None:
        return jsonify({"success": False, "error": f"Unknown source type '{source.get('type')}'."})

    return jsonify({
        "success": result.success,
        "blocked": result.blocked,
        "count": len(result.listings),
        "error": result.error,
        "fix_hint": result.fix_hint,
        "duration": round(result.duration_seconds, 1),
    })


def _load_fb_login_status() -> dict:
    if FB_LOGIN_STATUS_PATH.exists():
        try:
            return json.loads(FB_LOGIN_STATUS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"state": "idle"}


@app.route("/facebook/login")
def facebook_login_page():
    return render_template("facebook_login.html", status=_load_fb_login_status())


@app.route("/vnc/<path:filename>")
def vnc_static(filename):
    # noVNC's own web client (HTML/JS/CSS), served under our own origin/port
    # instead of a separate one — a second port isn't reachable through a
    # single-port tunnel like ngrok, which is exactly what left this blank
    # when accessed over the public URL.
    return send_from_directory(NOVNC_STATIC_DIR, filename)


@sock.route("/vnc/vnc-ws")
def vnc_ws(ws):
    """Proxies the noVNC client's WebSocket to the VNC server's raw TCP
    socket — a minimal websockify, running inside Flask itself so it shares
    the dashboard's own port rather than needing one of its own."""
    try:
        vnc_socket = socket.create_connection(("localhost", VNC_TCP_PORT), timeout=5)
    except OSError:
        return  # no active login session to connect to

    stop = threading.Event()

    def pump_vnc_to_ws():
        try:
            while not stop.is_set():
                data = vnc_socket.recv(4096)
                if not data:
                    break
                ws.send(data)
        except Exception:
            pass
        finally:
            stop.set()

    reader = threading.Thread(target=pump_vnc_to_ws, daemon=True)
    reader.start()

    try:
        while not stop.is_set():
            data = ws.receive(timeout=1)
            if data is None:
                continue
            if isinstance(data, str):
                data = data.encode("latin-1")
            vnc_socket.sendall(data)
    except Exception:
        pass
    finally:
        stop.set()
        vnc_socket.close()


@app.route("/facebook/login/start", methods=["POST"])
def facebook_login_start():
    global _fb_login_process
    if _fb_login_process is None or _fb_login_process.poll() is not None:
        FB_LOGIN_STATUS_PATH.unlink(missing_ok=True)
        # New session/process group (setsid) so Cancel below can kill the
        # whole tree (Xvfb, x11vnc, websockify, Chromium) at once — a plain
        # terminate() only signals this one Python process, and a raw SIGTERM
        # skips its finally-block cleanup, orphaning every child process.
        _fb_login_process = subprocess.Popen(
            ["python3", str(Path(__file__).parent / "facebook_login_service.py")],
            preexec_fn=os.setsid,
        )
    return redirect(url_for("facebook_login_page"))


@app.route("/facebook/login/status")
def facebook_login_status():
    return jsonify(_load_fb_login_status())


@app.route("/facebook/login/done", methods=["POST"])
def facebook_login_done():
    FB_LOGIN_SIGNAL_PATH.touch()
    return ("", 204)


@app.route("/facebook/login/cancel", methods=["POST"])
def facebook_login_cancel():
    global _fb_login_process
    if _fb_login_process is not None and _fb_login_process.poll() is None:
        try:
            os.killpg(os.getpgid(_fb_login_process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    _fb_login_process = None
    FB_LOGIN_STATUS_PATH.write_text(json.dumps({"state": "idle"}))
    FB_LOGIN_SIGNAL_PATH.unlink(missing_ok=True)
    return redirect(url_for("sources"))


if __name__ == "__main__":
    # A live search's progress lives only in this process's background
    # thread — if the container restarts mid-search (a rebuild, a crash),
    # the thread is gone but the status file was last written mid-run, so
    # it'd otherwise say "running" forever with no thread left to finish it
    # and no way for the polling JS to ever see it complete. Reset that on
    # startup so a restart always leaves the UI in a recoverable state.
    stale = _load_live_search_status()
    if stale.get("state") == "running":
        _write_live_search_status({
            "state": "failed",
            "query": stale.get("query"),
            "reason": "Dashboard restarted before this search finished — try again.",
        })

    stale_scrape = _load_manual_scrape_status()
    if stale_scrape.get("state") == "running":
        _write_manual_scrape_status({
            "state": "failed",
            "reason": "Dashboard restarted before this scrape finished — try again.",
        })

    # threaded=True: the /vnc-ws route holds a long-lived connection open for
    # the whole login session, which would otherwise block every other
    # request (including the status-polling JS) on Flask's single-threaded
    # dev server.
    app.run(host="0.0.0.0", port=8420, threaded=True)
