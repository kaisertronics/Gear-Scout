#!/usr/bin/env python3
"""
Gear Scout dashboard — a small status page + source manager that runs
alongside the scraper. Shares the same /data volume (SQLite DB, last-run
status, Facebook session) and /config volume (config.yaml) as the scout
service, so it always reflects the real current state.

Run with: python3 dashboard.py  (inside the container — see docker-compose.yml)
"""
import json
from typing import Optional

from scrapers.comps import believable, best_comp, similar_index
import logging
import os
import re
from collections import OrderedDict
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_sock import Sock
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parent))
from scrapers.dispatch import dispatch_scrape
from scrapers.facebook_marketplace_regions import FACEBOOK_MARKETPLACE_REGIONS
from scrapers.store import (
    all_priced_rows,
    count_hidden,
    set_hidden,
    unhide_all,
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


app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400  # static files cached a day (links carry a version)

# Listing cards for collapsed sections ("Show all", "No comp yet"…) aren't
# sent with the page; they're kept here briefly and fetched when opened.
_lazy_lists: "OrderedDict[str, list]" = OrderedDict()


LAZY_DIR = Path("/data/lazy")


def lazy_token(items: list) -> str:
    """A collapsed "Show more" list, fetched when you open it. The token is
    named after what's in the list, so re-rendering the same list (the
    background warm-up does it every few minutes) reuses it instead of
    pushing older ones out; lists are also saved to disk so they survive a
    restart. Kept 2 days."""
    import hashlib
    import pickle
    sig = "|".join(f"{l.get('global_id') or l.get('url')}:{(l.get('comp') or {}).get('pct')}" for l in items)
    token = hashlib.sha1(sig.encode()).hexdigest()[:20]
    if token in _lazy_lists:
        _lazy_lists.move_to_end(token)
        return token
    _lazy_lists[token] = items
    while len(_lazy_lists) > 3000:
        _lazy_lists.popitem(last=False)
    try:
        LAZY_DIR.mkdir(exist_ok=True)
        path = LAZY_DIR / f"{token}.pkl"
        if not path.exists():
            path.write_bytes(pickle.dumps(items))
            if secrets.randbelow(50) == 0:  # now and then, clear out old lists
                cutoff = time.time() - 2 * 86400
                for old in LAZY_DIR.glob("*.pkl"):
                    if old.stat().st_mtime < cutoff:
                        old.unlink(missing_ok=True)
    except Exception:
        logging.exception("Couldn't save a Show-more list")
    return token


@app.route("/lazy/<token>")
def lazy_cards(token):
    items = _lazy_lists.get(token)
    if items is None and re.fullmatch(r"[0-9a-f]{20}", token):
        import pickle
        try:
            items = pickle.loads((LAZY_DIR / f"{token}.pkl").read_bytes())
            _lazy_lists[token] = items
        except Exception:
            items = None
    if items is None:
        return '<p class="muted">This list has changed since the page loaded — reload the page to see the latest.</p>'
    return render_template("_lazy_cards.html", items=items)


def _ago(when) -> str:
    """"just now", "12 min ago", "3 h ago", "2 days ago"."""
    if not when:
        return ""
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, timezone.utc)
    elif isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    mins = int((datetime.now(timezone.utc) - when).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


def _last_searches() -> dict:
    """When each kind of scrape/search last finished and what it found —
    shown under every Scrape / Search button."""
    out = {}

    def read(path):
        try:
            return json.loads(path.read_text()), path.stat().st_mtime
        except Exception:
            return None, None
    st, at = read(MANUAL_SCRAPE_STATUS_PATH)
    if st and st.get("state") in ("done", "failed"):
        out["manual"] = {"ago": _ago(at), "new": st.get("new_count"), "failed": st.get("state") == "failed"}
    st, at = read(Path("/data/last_refresh.json"))
    if st:
        out["auto"] = {"ago": _ago(st.get("finished") or at), "new": st.get("new_count")}
    st, at = read(TELEX_STATUS_PATH)
    if st and st.get("state") in ("done", "failed"):
        out["telex"] = {"ago": _ago(at), "new": st.get("new"), "found": st.get("found"),
                        "all": st.get("fast_only"), "terms": st.get("total")}
    for kind, path, count in (("live", LIVE_SEARCH_STATUS_PATH, "matched_count"),
                              ("lowest", LOWEST_STATUS_PATH, "found"), ("fbposts", FBPOSTS_STATUS_PATH, "found")):
        st, at = read(path)
        if st and st.get("state") in ("done", "failed"):
            out[kind] = {"ago": _ago(at), "query": st.get("query"), "found": st.get(count),
                         "failed": st.get("state") == "failed"}
    return out


@app.context_processor
def _lazy_context():
    try:
        css_v = int(Path(app.static_folder, "style.css").stat().st_mtime)
    except OSError:
        css_v = 0
    try:
        ai_on = bool(((load_config_raw().get("ai") or {}).get("gemini_api_key") or "").strip())
    except Exception:
        ai_on = False
    from scrapers.telex import search_links
    return {"lazy_token": lazy_token, "css_version": css_v, "ai_enabled": ai_on, "search_links": search_links,
            "last_searches": _last_searches}


_last_use = {"at": 0.0}


@app.after_request
def _note_owner_activity(resp):
    """Remembers when you last opened a page (not background checks or
    polling), so updates wait instead of restarting Gear Scout while you're
    using it (scripts/safe_deploy.sh reads /data/last_user_request)."""
    if (resp.mimetype == "text/html" and not request.headers.get("X-GearScout-Test")
            and not request.headers.get("X-Warm") and time.time() - _last_use["at"] > 20):
        _last_use["at"] = time.time()
        try:
            Path("/data/last_user_request").touch()
        except OSError:
            pass
    return resp


@app.after_request
def _no_stale_pages(resp):
    """Pages always come fresh (phones, especially iPhones, otherwise
    show a saved copy and new changes seem missing). Static files and
    images keep their own caching."""
    if resp.mimetype == "text/html" and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.after_request
def _compress(resp):
    """Gzip pages, JSON, CSS and JS — listing pages shrink about 10x, which
    is most of the load time over the internet / on a phone."""
    import gzip
    if (resp.status_code != 200 or resp.direct_passthrough or "gzip" not in request.headers.get("Accept-Encoding", "")
            or resp.headers.get("Content-Encoding")):
        return resp
    ctype = resp.headers.get("Content-Type", "")
    if not ctype.startswith(("text/", "application/json", "application/javascript", "image/svg")):
        return resp
    data = resp.get_data()
    if len(data) < 1024:
        return resp
    resp.set_data(gzip.compress(data, compresslevel=5))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(resp.get_data()))
    resp.headers["Vary"] = "Accept-Encoding"
    return resp
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

SOURCE_TYPES = ["html", "rss", "craigslist", "craigslist_region", "facebook", "facebook_marketplace_region",
                "shopgoodwill", "hibid", "kijiji", "offerup", "shopify", "long_mcquade", "facebook_posts", "reddit"]


_config_cache: dict = {"stamp": None, "data": None}
_config_cache_lock = threading.Lock()


def load_config_raw():
    """Parsed config.yaml, shared and READ-ONLY. Parsing the (now 1,000+
    line) file takes up to ~0.7s and a page load needed it several times, so
    it's parsed once and reused until the file changes on disk. Copying it
    per caller isn't an option (deepcopy of these YAML objects took ~5s) —
    anything that edits and saves must use load_config_for_edit()."""
    st = os.stat(CONFIG_PATH)
    stamp = (st.st_mtime_ns, st.st_size)
    with _config_cache_lock:
        if _config_cache["stamp"] != stamp:
            with open(CONFIG_PATH) as f:
                data = yaml_rt.load(f)
            if not isinstance(data, dict) and _config_cache["data"] is not None:
                return _config_cache["data"]  # caught mid-save: keep the last good copy, re-read next time
            _config_cache["data"] = data
            _config_cache["stamp"] = stamp
        return _config_cache["data"]


def load_config_for_edit():
    """A fresh, private parse of config.yaml, safe to modify and save."""
    with open(CONFIG_PATH) as f:
        return yaml_rt.load(f)


def save_config_raw(data):
    # Written to a temporary file and swapped in at once: writing in place
    # left config.yaml empty or half-written for a moment, and a page or the
    # scraper reading it then got nothing (a Telex removal crashed the
    # deals-for-you box on Oct 22).
    tmp = f"{CONFIG_PATH}.saving"
    with open(tmp, "w") as f:
        yaml_rt.dump(data, f)
    try:
        shutil.copymode(CONFIG_PATH, tmp)
    except OSError:
        pass
    os.replace(tmp, CONFIG_PATH)


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
    started = time.time()

    def on_progress(done, total, current_source):
        _write_live_search_status({
            "state": "running", "started": started,
            "query": query,
            "done": done,
            "total": total,
            "current_source": current_source,
        })

    try:
        from scrapers.learning import record_search
        record_search(query)
    except Exception:
        logging.exception("Couldn't record search")
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
    """"Scrape now" runs as its own lower-priority process (nice 10), not
    inside the web server: it uses several CPUs for minutes, and in the same
    process it starved the pages (the site hung). This thread just waits for
    it; the process writes the progress file itself."""
    import subprocess
    try:
        proc = subprocess.run(["nice", "-n", "10", sys.executable, "-m", "scrapers.manual_scrape"],
                              cwd=str(Path(__file__).parent), timeout=3600)
        if proc.returncode != 0 and _load_manual_scrape_status().get("state") == "running":
            _write_manual_scrape_status({"state": "failed", "reason": f"scrape process exited ({proc.returncode})"})
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
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return None


def _ensure_login_password():
    """Gear Scout's sign-in used to share the email app password, so making
    a new app password for email changed your sign-in (and the sign-in page,
    reachable through the phone link, guarded your Gmail credential). Now
    the sign-in has its own password, kept only as a hash: the first time,
    it's set to the password you sign in with today, so nothing changes."""
    try:
        cfg = load_config_raw()
        if (cfg.get("login") or {}).get("password_hash"):
            return
        current = str((cfg.get("email") or {}).get("password") or "")
        if not current:
            return
        from werkzeug.security import generate_password_hash
        cfg["login"] = {"password_hash": generate_password_hash(current)}
        save_config_raw(cfg)
        logging.info("Gear Scout sign-in now has its own password (kept as a hash)")
    except Exception:
        logging.exception("Couldn't set up the separate sign-in password")


_ensure_login_password()


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
        email_ok = bool(real_email) and secrets.compare_digest(given_email.strip().lower(), real_email.strip().lower())
        login_hash = (cfg.get("login") or {}).get("password_hash")
        if login_hash:
            from werkzeug.security import check_password_hash
            password_ok = check_password_hash(login_hash, given_password)
        else:  # before Gear Scout had its own sign-in password
            password_ok = bool(real_password) and secrets.compare_digest(given_password, real_password)
        if email_ok and password_ok:
            session["authenticated"] = True
            return redirect(request.args.get("next") or url_for("feed"))
        error = "Wrong email or password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/sw.js")
def service_worker():
    # Served from the site root (not /static) so its scope covers every page.
    js = "self.addEventListener('fetch', () => {});"
    return app.response_class(js, mimetype="application/javascript",
                              headers={"Cache-Control": "no-cache"})


@app.context_processor
def _inject_mic_focus():
    try:
        return {"mic_focus_on": bool((load_config_raw().get("mic_focus") or {}).get("enabled"))}
    except Exception:
        return {"mic_focus_on": False}


@app.context_processor
def _distance_filter_context():
    try:
        home_zip = str(load_config_raw().get("home_zip") or "")
    except Exception:
        home_zip = ""
    return {"home_zip_set": bool(home_zip), "within_value": request.args.get("within", ""),
            "deals_only": request.args.get("deals") == "1",
            "worth_only": request.args.get("worth") == "1"}


_price_index_cache: dict = {"at": 0.0, "index": None}


def _price_index() -> dict:
    """Typical prices from local history — rebuilt at most every 5 minutes
    (it reads every priced listing and takes a couple of seconds)."""
    from scrapers.comps import price_index
    forced = _price_index_cache["at"] == 0  # the cache warmer asks for a fresh one
    _price_index_cache["at"] = time.time()
    return price_index(force=forced)


def auction_countdown(description, now=None) -> Optional[dict]:
    """Countdown for an auction description ("... ends YYYY-MM-DD HH:MM", Pacific)."""
    m = re.search(r"ends (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", description or "")
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        end = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    except ValueError:
        return None
    ts = int(end.timestamp())
    secs = ts - int(now if now is not None else time.time())
    if secs <= 0:
        return {"label": "ended", "urgent": False, "ts": ts}
    mins = secs // 60
    if mins < 60:
        label = f"{max(mins, 1)} min left"
    elif mins < 24 * 60:
        label = f"{mins // 60}h {mins % 60:02d}m left"
    else:
        d = mins // 1440
        label = f"{d} day{'s' if d != 1 else ''} left"
    return {"label": label, "urgent": mins < 60, "ts": ts}


def _collision_ok(title, description=None) -> bool:
    """Display-time twin of the ingest check: hides records/non-gear that were
    stored before the guard existed. Never deletes rows; fails open."""
    try:
        from scrapers.collision_guard import collision_ok
        return collision_ok(title, description)
    except Exception:
        return True


def _cable_wanted(l: dict) -> bool:
    """Matches a cable / connector term on the Telex List: a cable is what was asked for, not an accessory."""
    from scrapers.wanted import is_cable_term
    return any(is_cable_term(t) for t in l.get("wanted") or [])


def _decorate(listings: list[dict]) -> list[dict]:
    """Drops listings matching the user's exclude words and adds display
    flags: needs_repair, typical price / deal, and 'was' price after a drop."""
    from scrapers.enrich import title_says_sold
    from scrapers.enrich import (build_price_index, exclude_match, is_accessory_only, is_not_audio,
                                 needs_repair, price_context)

    from scrapers.geo import distance_miles

    cfg = load_config_raw()
    exclude_words = cfg.get("exclude_words") or []
    hide_acc = cfg.get("hide_accessories", True)
    from scrapers.enrich import item_form, wants_pedals
    # Search shows what you asked for; everywhere else pedals are left out
    # unless one of your terms asks for them.
    hide_pedals = request.endpoint not in ("search",) and not wants_pedals(cfg)
    home_zip = str(cfg.get("home_zip") or "")
    try:
        within = int(request.args.get("within") or 0)
    except ValueError:
        within = 0
    deals_only = request.args.get("deals") == "1"
    worth_only = request.args.get("worth") == "1"
    index = _price_index()
    from scrapers.market import load_market
    market = load_market()
    try:
        from scrapers.learning import FAST_SALE_DAYS, sale_stats
        sales = sale_stats()
    except Exception:
        logging.exception("Couldn't load sale stats")
        sales, FAST_SALE_DAYS = {}, 0
    from scrapers.enrich import model_key, parse_price
    now = datetime.now(timezone.utc)
    from scrapers import price_band as _pb
    _band = _pb.band() if request.endpoint in ("index", "mics_page") else None  # Home and Mics only
    # How many ads each seller has (per site) in the last 90 days: one query.
    seller_counts = {}
    try:
        from scrapers.store import _conn
        _c = _conn()
        try:
            _cut = (now - timedelta(days=90)).isoformat()
            for _src, _sel, _n in _c.execute(
                    "SELECT source_name, seller, COUNT(*) FROM seen WHERE seller IS NOT NULL "
                    "AND hidden = 0 AND duplicate = 0 AND first_seen >= ? GROUP BY source_name, seller", (_cut,)):
                seller_counts[(_src, _sel)] = _n
        finally:
            _c.close()
    except Exception:
        logging.exception("Couldn't load seller counts")
    out = []
    for l in listings:
        if (exclude_match(l.get("title"), exclude_words) or is_not_audio(l.get("title")) or title_says_sold(l.get("title"))
                or (hide_acc and is_accessory_only(l.get("title")) and not _cable_wanted(l))
                or (hide_pedals and item_form(l.get("title")) == "pedal")
                or not _collision_ok(l.get("title"), l.get("description"))):
            continue
        # Learned from listings that sold: how fast this model goes, and at what price.
        sale = sales.get(model_key(l.get("title")) or "")
        l["sale"] = ({"fast": sale["days"] <= FAST_SALE_DAYS, "days": round(sale["days"], 1),
                      "price": f"${sale['price']:,.0f}", "n": sale["n"]} if sale else None)
        # Days on market: a listing that has sat for weeks usually has a
        # seller open to an offer.
        try:
            from scrapers.base import usable_seller
            l["seller_other"] = (max(0, seller_counts.get((l.get("source_name"), l.get("seller")), 0) - 1)
                                 if usable_seller(l.get("seller")) else 0)
        except Exception:
            l["seller_other"] = 0
        l["stale"] = None
        try:
            since = datetime.fromisoformat(l.get("posted_at") or l.get("first_seen"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            days = (now - since).days
            value = parse_price(l.get("price"))
            if days >= 21 and value and not l.get("sold"):
                l["stale"] = {"days": days, "offer": f"${max(5, round(value * 0.85 / 5) * 5):,}"}
        except (TypeError, ValueError):
            pass
        try:
            from scrapers.motivated import reason as motivated_reason
            l["motivated"] = motivated_reason(l)
        except Exception:
            l["motivated"] = None
        l["age_days"] = _age_days(l)
        l["age_known"] = bool(l.get("posted_at"))
        l["needs_repair"] = needs_repair(l.get("title"), l.get("description"))
        l["price_ctx"] = price_context(l.get("title"), l.get("price"), index, market,
                                       description=l.get("description"))
        # A current auction bid isn't a sale price, so it's never a "deal".
        l["is_auction"] = (l.get("description") or "").startswith("Auction")
        l["auction_left"] = auction_countdown(l.get("description")) if l["is_auction"] else None
        if (l.get("sold") or l.get("pending") or l["is_auction"]) and l["price_ctx"]:
            l["price_ctx"]["deal"] = False
            l["price_ctx"]["worth"] = False  # a current bid isn't a price
        l["bundle"] = None
        if not (l.get("sold") or l["is_auction"]):
            from scrapers.bundle_breakdown import breakdown
            l["bundle"] = breakdown(l.get("title"), l.get("description"), parse_price(l.get("price")))
        l["resale"] = None
        if l["price_ctx"] and l["price_ctx"].get("deal"):
            from scrapers.resale_profit import estimate as resale_estimate
            l["resale"] = resale_estimate(l.get("title"), l.get("description"), parse_price(l.get("price")))
        l["ship_warn"] = None
        try:
            if not (l.get("sold") or l["is_auction"]):
                from scrapers.shipping_check import check as ship_check, label as ship_label
                _sp, _ss = parse_price(l.get("price")), l.get("shipping")
                if _sp and isinstance(_ss, (int, float)) and _ss > 0:
                    from scrapers import market_value as _mv
                    _uv = _mv.value(l.get("title"), l.get("description"))[0]
                    l["ship_warn"] = ship_label(ship_check(_sp, _ss, _uv))
        except Exception:
            l["ship_warn"] = None
        l["no_acc"] = None
        if not l.get("sold"):
            from scrapers.accessory_check import check as accessory_check
            l["no_acc"] = accessory_check(l.get("title"), l.get("description"))
        l["serial_note"] = None
        try:
            if not l.get("sold"):
                from scrapers.serial_check import check as serial_check
                l["serial_note"] = serial_check(l.get("title"), l.get("description"))
        except Exception:
            l["serial_note"] = None
        from scrapers.condition_grade import grade as condition_grade
        l["cond_grade"] = condition_grade(l.get("title"), l.get("description"))
        l["distance"] = distance_miles(home_zip, l.get("location")) if home_zip else None
        # Listings with no known distance (shipped-item sites, older rows)
        # stay visible — the radius only filters what can be measured.
        if within and l["distance"] is not None and l["distance"] > within:
            continue
        l["is_deal"] = bool(l["price_ctx"] and l["price_ctx"].get("deal"))
        if deals_only and not l["is_deal"]:
            continue
        # Display only: dim (never hide) cards priced far outside what you star.
        # Starred ads, deals, Best deals (50%+ under used) and Telex deals stay bright.
        _m = l.get("mic") or {}
        l["price_dim"] = bool(
            _band and not l.get("favorite") and not l["is_deal"] and not l.get("deal")
            and not ((_m.get("pct") or 0) >= 50) and not ((l.get("comp") or {}).get("pct", 0) >= 50)
            and _pb.is_outside(parse_price(l.get("price")), _band))
        # "Worth it only": 10%+ under B-stock value (or a solid used value for
        # vintage gear); listings without a comp yet are left out.
        if worth_only and not (l["price_ctx"] or {}).get("worth"):
            continue
        out.append(l)
    _attach_sparklines(out)
    return out


def _attach_sparklines(listings: list[dict]) -> None:
    """Adds l["spark"] (tiny SVG price-history line) to listings with 2+
    recorded prices. One database query for the whole page."""
    try:
        from scrapers.store import price_history_for
        history = price_history_for([l.get("global_id") for l in listings])
    except Exception:
        logging.exception("Couldn't load price history")
        return
    w, h, pad = 80, 20, 3
    for l in listings:
        pts = history.get(l.get("global_id")) or []
        if len(pts) < 2:
            continue
        # Stubborn seller: price cut 2+ times (consecutive drops > $0.50).
        cuts = sum(1 for (_, a), (_, b) in zip(pts, pts[1:]) if a - b > 0.5)
        if cuts >= 2:
            l["cuts"] = cuts
        pts = pts[-12:]
        vals = [v for _, v in pts]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1
        step = (w - 2 * pad) / (len(vals) - 1)
        xy = [(pad + i * step, pad + (h - 2 * pad) * (1 - (v - lo) / span) if hi != lo else h / 2)
              for i, v in enumerate(vals)]
        l["spark"] = {
            "w": w, "h": h,
            "points": " ".join(f"{x:.1f},{y:.1f}" for x, y in xy),
            "cx": round(xy[-1][0], 1), "cy": round(xy[-1][1], 1),
            "color": "var(--green-text)" if vals[-1] < vals[0] else ("var(--red)" if vals[-1] > vals[0] else "var(--muted)"),
            "title": " → ".join(f"${v:,.0f}" for v in vals),
        }


def _last_scraped_by_site() -> dict[str, str]:
    """{site section name: time of its most recent successful scrape}."""
    from scrapers.store import _conn
    out: dict[str, str] = {}
    try:
        with _conn() as conn:
            rows = conn.execute("SELECT source_name, MAX(ran_at) FROM source_runs WHERE success = 1"
                                " AND kind != 'live' GROUP BY source_name").fetchall()
    except Exception:
        return out
    for name, ran_at in rows:
        family = (name or "").partition(" — ")[0]
        family = {"FB Marketplace": "Facebook Marketplace", "FB": "Facebook groups"}.get(family, family)
        if ran_at and ran_at > out.get(family, ""):
            out[family] = ran_at
    return out


def _apply_comp_rule(listings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Splits decorated listings into ones at least 10% under their comp
    (B-stock value when the model has one, else the best comp from
    scrapers/comps.py) and ones with no comp yet. Listings above the threshold are
    dropped. Favorites always stay; auctions (a current bid isn't a price)
    go with the no-comp group."""
    from scrapers.enrich import parse_price
    from scrapers.market import load_market
    market, index, similar = load_market(), _price_index(), similar_index()
    worth, no_comp = [], []
    _apply_comp_rule.auctions = []
    for l in listings:
        if l.get("favorite"):
            worth.append(l)
            continue
        ctx = l.get("price_ctx") or {}
        value = ctx.get("unit_value") or parse_price(l.get("price"))
        if l.get("is_auction") and value and not l.get("sold"):
            # Auctions go in their own section: worth watching while the
            # current bid is still 10%+ under the comp.
            comp = (parse_price(ctx["bstock"]), "B-stock", False) if ctx.get("bstock") and not ctx.get("rough") \
                else best_comp(l, market, index, similar, None, None)
            if comp and value <= comp[0] * 0.9:
                l["comp"] = {"pct": round((1 - value / comp[0]) * 100), "ref": f"${comp[0]:,.0f}",
                             "label": comp[1], "est": comp[2]}
                m = re.search(r"ends (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", l.get("description") or "")
                l["auction_ends"] = m.group(1) if m else ""
                _apply_comp_rule.auctions.append(l)
            elif not comp:
                no_comp.append(l)
            continue
        if l.get("is_auction") or l.get("sold") or l.get("pending") or not value:
            no_comp.append(l)
            continue
        if ctx.get("bstock") and not ctx.get("rough"):
            ref, label, est = parse_price(ctx["bstock"]), "B-stock", False
        else:
            comp = best_comp(l, market, index, similar, None, None)
            if not comp:
                no_comp.append(l)
                continue
            ref, label, est = comp
        if ref * 0.2 <= value <= ref * 0.9 and believable(value, ref, est):
            l["comp"] = {"pct": round((1 - value / ref) * 100), "ref": f"${ref:,.0f}", "label": label, "est": est}
            worth.append(l)
    return worth, no_comp


def _deals_from(grouped: list[tuple]) -> list[dict]:
    """Every deal across all sources, biggest discount first — shown in its
    own section above the per-source groups."""
    # Around your budget ($500–$2,000, with some room): no $25 interfaces.
    lo, hi = BUDGET[0] * 0.6, BUDGET[1] * 1.25
    deals = [l for _, items in grouped for l in items if l.get("is_deal")
             and lo <= ((l.get("price_ctx") or {}).get("unit_value") or 0) <= hi]
    return sorted(deals, key=lambda l: l["price_ctx"].get("pct_under", 0), reverse=True)


def _group_by_source(listings: list[dict], decorated: bool = False) -> list[tuple]:
    """Group already-newest-first listings by source, preserving recency
    order within each group, and order the groups themselves by whichever
    source has the single most recent listing."""
    groups: dict[str, list[dict]] = {}
    for listing in (listings if decorated else _decorate(listings)):
        # One section per site: every Craigslist town / Facebook region /
        # Vintage King list goes under its site, with the town or region
        # shown on the card instead.
        family, _, detail = (listing.get("source_name") or "").partition(" — ")
        family = {"FB Marketplace": "Facebook Marketplace", "FB": "Facebook groups"}.get(family, family)
        listing["source_detail"] = detail or None
        groups.setdefault(family, []).append(listing)
    # `listings` arrives newest-first, so the first listing seen for a given
    # source is already that source's most recent one.
    # Facebook Marketplace and Craigslist first (the owner's priority), then
    # the rest by whichever site has the most recent listing.
    order = sorted(groups.items(), key=lambda kv: kv[1][0]["first_seen"], reverse=True)
    lead = ("Facebook Marketplace", "Craigslist")
    return [kv for name in lead for kv in order if kv[0] == name] + [kv for kv in order if kv[0] not in lead]


_for_you_cache: dict = {"at": 0.0, "items": None, "ceiling": None}


def _favorite_price_ceiling() -> Optional[float]:
    """Your price range, learned from favorites: 1.2x the price that three
    quarters of your favorites are under (at least $150)."""
    from scrapers.enrich import parse_price
    from scrapers.store import _conn
    with _conn() as conn:
        prices = sorted(v for v in (parse_price(p) for (p,) in conn.execute(
            "SELECT price FROM seen WHERE favorite = 1")) if v)
    if len(prices) < 5:
        return None
    return max(150.0, prices[int(len(prices) * 0.75) - 1] * 1.2)


def _for_you(grouped: list[tuple], n: int = 12) -> list[dict]:
    """Really good deals on the kind of gear you favorite: like your
    favorites (learned taste), priced well under market value (15%+, or
    25%+ when the value is a rough estimate), and within your price range.
    Ranked by discount, boosted by how well it matches your taste. Drawn
    from the last 7 days, not just the newest listings."""
    if _for_you_cache["items"] is not None and time.time() - _for_you_cache["at"] < 300:
        return _for_you_cache["items"]
    try:
        import sqlite3
        from scrapers.enrich import is_bundle, is_partial, is_relevant
        from scrapers.learning import keywords_with_learned, taste
        from scrapers.store import _conn
        t = taste()
        if not t.ready:
            return []
        # Your stated budget ($500–$2,000), with a little room either side.
        floor, ceiling = BUDGET[0] * 0.8, BUDGET[1] * 1.1
        cfg = load_config_raw()
        terms = tuple(keywords_with_learned(cfg))
        with _conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                """SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND hidden = 0 AND duplicate = 0
                   AND sold = 0 AND COALESCE(pending, 0) = 0 AND favorite = 0
                   AND first_seen >= ? ORDER BY first_seen DESC LIMIT 4000""",
                ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))]
        # Taste first (cheap), then full price context only for those.
        rows = [r for r in rows if t.score(r.get("title") or "") >= 1.0
                and is_relevant(r.get("title"), r.get("price"), terms)]
        scored, seen_urls = [], set()
        for l in _decorate(rows):
            ctx = l.get("price_ctx") or {}
            unit = ctx.get("unit_value")
            pct = ctx.get("pct_under")
            if (l.get("url") in seen_urls or l.get("is_auction") or not unit or pct is None
                    or not ctx.get("typical") or ctx.get("rough") or not (floor <= unit <= ceiling)
                    or (l.get("age_days") or 0) > FRESH_DAYS
                    or is_partial(l.get("title")) or is_bundle(l.get("title"))):
                continue
            # 20%+ under a solid comp; more than 75% under is almost always a
            # mismatch (a part, a card, a different version), not a steal.
            if pct < 20 or pct > 75:
                continue
            same_ad = (l.get("title") or "").lower(), l.get("price")
            if same_ad in seen_urls:  # the same ad cross-listed / re-posted
                continue
            seen_urls.add(l.get("url"))
            seen_urls.add(same_ad)
            match = min(t.score(l.get("title") or ""), 6.0)
            # Newer is better: a 2-day-old ad counts a bit less than today's.
            fresh = 1 - min(l.get("age_days") or 0, FRESH_DAYS) / (FRESH_DAYS * 2)
            local = 1.15 if (l.get("source_name") or "").startswith(_LOCAL_FIRST) else 1.0
            scored.append((pct * (1 + match / 3) * fresh * local, l))
        items = [l for _, l in sorted(scored, key=lambda x: -x[0])[:n]]
        _for_you_cache.update(at=time.time(), items=items, ceiling=ceiling)
        return items
    except Exception:
        logging.exception("Couldn't pick deals for you")
        return []


_SOURCE_TAGS = [
    ("FB Posts", "FB post", "fb"), ("FB Marketplace", "Facebook", "fb"), ("FB —", "FB group", "fb"),
    ("eBay", "eBay", "ebay"), ("Reverb", "Reverb", "reverb"), ("Craigslist", "Craigslist", "cl"),
    ("OfferUp", "OfferUp", "offerup"), ("Kijiji", "Kijiji", "kijiji"), ("ShopGoodwill", "ShopGoodwill", "sgw"),
    ("HiBid", "HiBid", "sgw"), ("Vintage King", "Vintage King", "store"), ("Long & McQuade", "L&M", "store"), ("Alto Music", "Alto", "store"),
    ("Rudy", "Rudy's", "store"), ("GroupDIY", "GroupDIY", "forum"), ("The Gear Page", "Gear Page", "forum"),
    ("AudioKarma", "AudioKarma", "forum"), ("Gearspace", "Gearspace", "forum"),
]


@app.route("/img/<name>")
def cached_image(name):
    """Locally saved copies of pictures whose links expire (Facebook)."""
    if not re.fullmatch(r"[0-9a-f]{20}\.jpg", name):
        return "", 404
    from scrapers.image_cache import IMAGE_DIR
    resp = send_from_directory(IMAGE_DIR, name, mimetype="image/jpeg", max_age=86400 * 30)
    return resp


@app.template_filter("delivered")
def _delivered(l) -> Optional[str]:
    """Price + listed US shipping for the whole ad ("$1,706"), or None when
    the site doesn't list shipping (or it's free / local pickup)."""
    from scrapers.enrich import parse_price
    ship = l.get("shipping") if hasattr(l, "get") else None
    value = parse_price(l.get("price")) if hasattr(l, "get") else None
    if not isinstance(ship, (int, float)) or ship <= 0 or not value:
        return None
    total = value + ship
    return f"${total:,.0f}" if total >= 100 else f"${total:,.2f}"


@app.template_filter("per_channel")
def _per_channel(l):
    """'$X per channel' info for multi-channel gear, or None (see scrapers/price_per_channel.py)."""
    from scrapers import price_per_channel
    return price_per_channel.per_channel(l.get("price"), l.get("title")) if hasattr(l, "get") else None


@app.template_filter("rare_find")
def _rare_find(l):
    """'Rare find' label for rare/vintage models, or None (see scrapers/rare_finds.py)."""
    from scrapers import rare_finds
    return rare_finds.rare_find(l.get("title"), l.get("brand")) if hasattr(l, "get") else None


@app.template_test("local_pickup")
def _is_local_pickup(source_name: str) -> bool:
    """Sites where you pick the item up yourself (no shipping)."""
    return bool(re.match(r"(?:FB |Facebook|Craigslist|OfferUp|Kijiji|seattle group)", source_name or ""))


@app.template_filter("img")
def _img_filter(url):
    """The saved copy of a picture when there is one, else the original link."""
    from scrapers.image_cache import cache_name, cached_path
    if cached_path(url):
        return url_for("cached_image", name=cache_name(url))
    return url


@app.template_filter("source_tag")
def _source_tag_filter(source_name):
    """'Craigslist — Seattle' -> ('Craigslist', 'cl'): short site name + color group."""
    name = source_name or ""
    for prefix, label, css in _SOURCE_TAGS:
        if name.startswith(prefix):
            return (label, css)
    return (name.split(" — ")[0][:14] or "Other", "other")


@app.template_filter("usd")
def _usd_filter(price):
    from scrapers.enrich import display_price
    return display_price(price)[0]


@app.template_filter("orig_price")
def _orig_price_filter(price):
    from scrapers.enrich import display_price
    return display_price(price)[1]


@app.template_filter("local")
def _local_filter(value):
    """ISO/UTC time -> '12-hour, your time zone' for templates."""
    from scrapers.timefmt import local_time
    return local_time(value, (load_config_raw().get("schedule") or {}).get("timezone"))


def _telex_terms(cfg) -> list[str]:
    return [str(t).strip() for t in (cfg.get("telex_list") or []) if str(t).strip()]


def _telex_matches(terms: list[str], per_term: int = 1000, strict: bool = False,
                   note: bool = True) -> list[tuple[str, list[dict]]]:
    """Every stored listing still for sale (from scrapes, live searches and
    Telex searches) matching each Telex term — every word, any order, like
    the live search. All of them, cheapest first."""
    import sqlite3
    from scrapers.store import _conn
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND hidden = 0 AND duplicate = 0
               AND COALESCE(sold, 0) = 0 ORDER BY first_seen DESC""").fetchall()]
    from scrapers.base import _keyword_pattern, _word_matches, fix_brand_spelling
    texts = [fix_brand_spelling((r["title"] or "").lower()) for r in rows]

    from scrapers.telex import model_matcher, term_matcher
    matcher = model_matcher if strict else term_matcher

    from scrapers.enrich import is_accessory_only, is_partial, item_form
    out = []
    for term in terms:
        hits, seen_urls, seen_ads = [], set(), set()
        match = matcher(term)
        # "LA-2A" means the studio unit: pedal and plugin versions only show
        # when the term asks for them ("LA-2A pedal").
        term_form = item_form(term)
        for r, text in zip(rows, texts):
            if r["url"] in seen_urls or not match(text):
                continue
            ad = ((r["title"] or "").lower().strip(), r["price"])
            if ad in seen_ads:  # the same ad stored twice (another link to it)
                continue
            form = item_form(r["title"])
            if form in ("pedal", "plugin") and form != term_form:
                continue
            # Parts and add-ons (knobs, lamps, rack ears, panels, manuals,
            # anything "for" the model) aren't the gear you're tracking.
            if is_partial(r["title"]) or is_accessory_only(r["title"]):
                continue
            seen_urls.add(r["url"])
            seen_ads.add(ad)
            hits.append(r)
            if len(hits) >= 3000:
                break
        out.append((term, hits))
    # Price context etc. for every hit in one pass (a listing can sit under
    # several terms), then hand each group its decorated copies.
    unique = list({id(r): r for _, hits in out for r in hits}.values())
    kept = {id(r) for r in _decorate(unique)}  # adds display fields in place; drops excluded

    def too_cheap(r):
        # Under 15% of what the model really goes for: a pedal, plugin or
        # part with a vague title ("Universal Audio Teletronix LA-2A — $110").
        ctx = r.get("price_ctx") or {}
        typical = parse_price(ctx.get("typical")) if ctx.get("typical") and not ctx.get("rough") else None
        value = ctx.get("unit_value") or parse_price(r.get("price"))
        return bool(typical and value and value < typical * 0.15)
    from scrapers.enrich import parse_price
    by_id = {id(r): r for r in unique}
    kept = {k for k in kept if not too_cheap(by_id[k])}
    from scrapers.enrich import parse_price

    def price_order(r):
        # Cheapest first by the price shown (US dollars for Canadian ads).
        # No price, or a placeholder like "$1 — make an offer", goes last.
        value = parse_price(r.get("price"))
        placeholder = value is None or value < 5
        return (placeholder, value or 0)

    # Your rule: a listing shows only when it's 10%+ under its comp. Every
    # listing gets the most trustworthy comp available (see scrapers/comps.py);
    # only the few with nothing at all to go on are set aside.
    from scrapers.enrich import _ALIAS_TO_BRAND, canonical_brand, is_generic_term, model_key
    from scrapers.market import load_market
    market = load_market()
    index = _price_index()
    similar = similar_index()

    out_groups = []
    for term, hits in out:
        good, unpriced = [], []
        tkey = "telex:" + term.strip().lower()
        unreliable = market.rough | market.mixed | market.distrusted
        term_ref = market.get(tkey) if tkey in market and tkey not in unreliable else None
        # The term's own price only works as a yardstick when the term names
        # one specific product: a brand plus a model ("Avalon 737") or a
        # product line ("Distressor", "Apollo X8P") — not "LA-2A" (clones of
        # every price) or a bare brand ("audioscape").
        term_brand = canonical_brand(term)
        lowered = term.lower()
        names_line = any(alias in lowered and alias not in (term_brand or "").replace("-", " ")
                         for alias, canon in _ALIAS_TO_BRAND.items() if canon == term_brand and len(alias) > 3)
        brand_words = set((term_brand or "").replace("-", " ").split())
        distinctive = [w for w in re.findall(r"[a-z0-9-]+", lowered)
                       if len(w) >= 3 and w not in brand_words and not is_generic_term(w)
                       and w not in ("clone", "style", "copy", "replica", "type", "series")]
        if not (term_brand and (model_key(term) or names_line or distinctive)):
            term_ref = None
        term_comp = (term_ref, f"{market.sources.get(tkey) or 'Reverb'} “{term}”") if term_ref else None
        for r in sorted((r for r in hits if id(r) in kept), key=price_order):
            comp = best_comp(r, market, index, similar, term_comp, term_brand)
            value = (r.get("price_ctx") or {}).get("unit_value") or parse_price(r.get("price"))
            if comp and value:
                ref, label, est = comp
                if ref * 0.2 <= value <= ref * 0.9 and believable(value, ref, est):
                    r["comp"] = {"pct": round((1 - value / ref) * 100), "ref": f"${ref:,.0f}",
                                 "label": label, "est": est}
                    good.append(r)
            else:
                unpriced.append(r)
        # Cheapest first, but ads posted 30+ days ago go after the fresh ones.
        for r in good:
            r["is_old"] = (r.get("age_days") or 0) > FRESH_DAYS
        good.sort(key=lambda r: r["is_old"])  # stable: keeps cheapest-first within each
        out_groups.append((term, good[:per_term], unpriced[:per_term]))
    if not note:
        return out_groups
    try:
        Path("/data/shown_telex.txt").write_text(
            "\n".join(dict.fromkeys(r["url"] for _, good, _ in out_groups for r in good if r.get("url"))))
    except OSError:
        pass
    # Tell the hourly refresh which Telex listings still have no comp, so it
    # looks those up first (Reverb, and eBay when its daily allowance allows).
    try:
        Path("/data/nocomp_titles.txt").write_text(
            "\n".join(dict.fromkeys(r["title"] for _, _, un in out_groups for r in un if r.get("title"))))
    except OSError:
        pass
    return out_groups


_telex_cache: dict = {"key": None, "at": 0.0, "groups": None}
_telex_state = {"running": False}
_data_version = [0]  # bumped whenever you hide/favorite/change terms or a search finishes


def _data_changed():
    _data_version[0] += 1


_verify_state = {"running": False, "last": 0.0}


def _verify_top_listings():
    """Checks the top listings on the Dashboard and Telex right away (not
    just hourly): first 6 per Telex term, first 12 per Dashboard section,
    plus New and Auctions. Sold/ended ones are marked and taken off the
    cached pages. Facebook listings are opened in a browser (every 4 hours).
    Runs in the background; listings checked in the last hour are skipped."""
    if _verify_state["running"]:
        # One check at a time; come back for anything new once it's done.
        _verify_state["again"] = True
        return
    urls = []
    d = _index_cache.get("data") or {}
    for _, items in d.get("grouped") or []:
        urls += [l["url"] for l in items[:12]]
    urls += [l["url"] for l in (d.get("auctions") or [])[:24]]
    for _, good, _ in (_telex_cache.get("groups") or []):
        urls += [r["url"] for r in good[:6]]
    st = _deals_cache.get("data") or {}
    # Steals sell within hours: every one is checked, Facebook ones hourly;
    # so are this week's newest deals.
    steal_urls = ({l["url"] for l in (st.get("trusted") or []) if l["comp"]["pct"] >= 60}
                  | {l["url"] for l in (st.get("rough") or []) if l["comp"]["pct"] >= 60}
                  | {l["url"] for l in sorted((l for l in (st.get("trusted") or []) if l.get("age_days") is not None),
                                              key=lambda l: l["age_days"])[:40]})
    urls = list(steal_urls) + urls
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return

    def run():
        from concurrent.futures import ThreadPoolExecutor
        from scrapers.sold_check import _record, check_fb, check_url
        from scrapers.store import _conn
        _verify_state["running"] = True
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            with _conn() as conn:
                recent = {u for (u,) in conn.execute(
                    f"SELECT url FROM seen WHERE url IN ({','.join('?' * len(urls))}) AND sold_checked_at >= ?",
                    (*urls, cutoff))}
            todo = [u for u in urls if u not in recent and "facebook.com" not in u]
            # Facebook needs a (slower) browser: re-check those every 4 hours, up to 25 at a time.
            fb_cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
            fb = [u for u in urls if "facebook.com/marketplace/item" in u]
            if fb:
                with _conn() as conn:
                    checked = dict(conn.execute(
                        f"SELECT url, MAX(sold_checked_at) FROM seen WHERE url IN ({','.join('?' * len(fb))}) GROUP BY url",
                        fb).fetchall())
                fb = [u for u in fb if (checked.get(u) or "") < (cutoff if u in steal_urls else fb_cutoff)][:40]

            def one(u):
                try:
                    gone = check_url(u)
                except Exception:
                    gone = None
                _record(u, gone)
                return u, gone
            with ThreadPoolExecutor(max_workers=4) as pool:
                gone = [u for u, g in pool.map(one, todo) if g]
            # Facebook ads need a real browser — heavy on this Mac — so they're
            # left to the scraper's hourly sold check (ads on your pages first).
            if gone:
                with _conn() as conn:
                    gids = [g for (g,) in conn.execute(
                        f"SELECT global_id FROM seen WHERE url IN ({','.join('?' * len(gone))})", gone)]
                for gid in gids:
                    _patch_cached(gid, hidden=True)
                logging.info("Took %d sold/ended listings off the Dashboard/Telex", len(gone))
        finally:
            _verify_state.update(running=False, last=time.time())
            if _verify_state.pop("again", False):
                _verify_top_listings()
    threading.Thread(target=run, name="verify-top", daemon=True).start()


def _patch_cached(global_id: str, favorite: Optional[bool] = None, hidden: bool = False):
    """Applies a star / hide to the cached Dashboard and Telex lists in
    place, so those pages stay instant instead of rebuilding."""
    def lists():
        if _feed_cache.get("data") is not None:
            yield _feed_cache["data"]
        d = _index_cache.get("data")
        if d:
            yield d["no_comp"]
            yield d.get("old", [])
            yield d.get("usually_hidden", [])
            for g in d.get("shop_folds", []):
                yield g["items"]
            yield d["for_you"]
            yield d["deals"]
            for _, items in d["grouped"]:
                yield items
        for _, good, unpriced in (_telex_cache.get("groups") or []):
            yield good
            yield unpriced
        for items in (_deals_cache.get("data") or {}).values():
            yield items
        for g in _group_cache.get("data") or []:
            yield g["good"]
            yield g["unpriced"]
    for items in lists():
        for i in range(len(items) - 1, -1, -1):
            if items[i].get("global_id") == global_id:
                if hidden:
                    del items[i]
                elif favorite is not None:
                    items[i]["favorite"] = 1 if favorite else 0


def _snapshot_save(name: str, data) -> None:
    """Keeps the last result of a slow page build on disk, so right after a
    restart (every update) the page shows it instantly instead of making you
    wait 30-80s while everything is recalculated."""
    import pickle
    try:
        tmp = Path(f"/data/.snap_{name}.{threading.get_ident()}.tmp")
        tmp.write_bytes(pickle.dumps(data))
        tmp.replace(Path(f"/data/.snap_{name}.pkl"))
    except Exception:
        logging.exception("Couldn't save the %s snapshot", name)


def _snapshot_load(name: str, max_age: float = 3 * 3600):
    import pickle
    p = Path(f"/data/.snap_{name}.pkl")
    try:
        if time.time() - p.stat().st_mtime < max_age:
            return pickle.loads(p.read_bytes())
    except Exception:
        pass
    return None


_deals_cache: dict = {"key": None, "at": 0.0, "data": None}
_deals_state = {"running": False}
STEAL_LEVELS = (50, 60, 70, 80)
DEAL_AGES = (("1", "Today"), ("3", "3 days"), ("7", "This week"), ("30", "This month"), ("all", "Any age"),
             ("old", "Older than a week"), ("dropped", "📉 Price just cut"))
DEAL_PRICES = (("500-2000", "$500–$2,000"), ("0-500", "Under $500"), ("2000-", "$2,000+"), ("any", "Any price"))
# The owner wants fresh ads (last few days): anything posted more than a
# week ago goes to a collapsed "older" section on every page.
FRESH_DAYS = 7
BUDGET = (500, 2000)  # what the owner usually spends
# Pages are recalculated when something changes (a scrape finishes, you hide /
# star / change terms) — otherwise at most every 15 minutes.
PAGE_REFRESH = 900
# Facebook Marketplace and Craigslist come first: local sellers price
# lowest, and many ship these days.
_LOCAL_FIRST = ("FB Marketplace", "Craigslist", "OfferUp", "Kijiji", "FB —", "seattle group")


def _dropped_recently(l: dict, days: int = 7) -> bool:
    """The seller cut the price in the last week (usually a motivated seller)."""
    when = l.get("price_dropped_at")
    if not when or not l.get("previous_price"):
        return False
    return when >= (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fresh_order(l: dict):
    """Strictly newest post first (the owner asked for this on Deals and
    Steals); Facebook / Craigslist only win a tie."""
    a = l.get("age_days")
    a = 999 if a is None else a
    local = (l.get("source_name") or "").startswith(_LOCAL_FIRST)
    return (round(a, 3), not local)


def _age_days(l: dict) -> Optional[float]:
    """How long the ad has been up: the seller's posting date when the site
    gives one, else when Gear Scout first saw it."""
    for field in ("posted_at", "first_seen"):
        try:
            d = datetime.fromisoformat(l.get(field) or "")
        except (TypeError, ValueError):
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)
    return None


def _all_deals(background: bool = False) -> dict:
    """Every stored listing still for sale that's 10%+ under its comp,
    double-checked against what other listings of the same gear ask (see
    scrapers/steals.py) — shared by the Deals and Steals pages. Takes ~20s
    over everything stored, so it's reused for 10 minutes unless something
    changed, rebuilt in the background, and kept warm."""
    key = (_data_version[0], _last_change_marker())
    if _deals_cache["key"] == key and time.time() - _deals_cache["at"] < PAGE_REFRESH:
        return _deals_cache["data"]
    if _deals_cache["data"] is None and not background:
        snap = _snapshot_load("deals")
        if snap is not None:
            _deals_cache.update(key=None, at=0.0, data=snap)
    # Something changed (a scrape running, a hide): show what we have right
    # away and rebuild in the background, instead of a blank page.
    if _deals_cache["data"] is not None and not background:
        if not _deals_state["running"]:
            def rebuild():
                _deals_state["running"] = True
                try:
                    with app.test_request_context("/deals"):
                        _all_deals(background=True)
                except Exception:
                    logging.exception("Deals rebuild failed")
                finally:
                    _deals_state["running"] = False
            threading.Thread(target=rebuild, name="deals-rebuild", daemon=True).start()
        return _deals_cache["data"]
    from scrapers.steals import find
    found = find(load_config_raw(), min_pct=10)
    data = {}
    for name in ("trusted", "rough"):
        items = _decorate(found[name])  # display fields; drops your exclude words
        for l in items:
            st = l["steal"]
            under_peers = st.get("peer") and st["peer"] < st["comp"]
            l["comp"] = {"pct": st["pct"], "ref": f"${st['ref']:,.0f}", "est": st["est"],
                         "label": "other listings of this gear" if under_peers else st["label"]}
            l["age_days"] = _age_days(l)
        data[name] = items
    try:
        Path("/data/shown_steals.txt").write_text("\n".join(dict.fromkeys(
            [l["url"] for l in data["trusted"] if l["comp"]["pct"] >= 60]
            + [l["url"] for l in data["rough"] if l["comp"]["pct"] >= 60][:60]
            + [l["url"] for l in data["trusted"] if (l["age_days"] or 99) <= 7][:150])))
    except OSError:
        pass
    _deals_cache.update(key=key, at=time.time(), data=data)
    _snapshot_save("deals", data)
    return data


def _steals(min_pct: int, background: bool = False) -> dict:
    """Listings 50%+ (or the chosen level) under used prices."""
    d = _all_deals(background)
    return {name: [l for l in d[name] if l["comp"]["pct"] >= min_pct] for name in ("trusted", "rough")}


@app.route("/deals")
def deals():
    # Opens on every deal, newest post first (the filters narrow it down).
    # It used to open on "last 3 days, $500–$2,000", which was often empty.
    age = request.args.get("age") or "all"
    age = age if age in dict(DEAL_AGES) else "all"
    price = request.args.get("price") or "any"
    price = price if price in dict(DEAL_PRICES) else "any"
    sort = request.args.get("sort") or "new"
    d = _all_deals()
    lo, _, hi = price.partition("-")
    lo = 0 if price == "any" else float(lo or 0)
    hi = float(hi) if hi and price != "any" else 1e12

    def in_price(l):
        v = l["steal"].get("unit") or 0
        return lo <= v <= hi

    def keep(l):
        a = l.get("age_days")
        a = 0 if a is None else a
        if age == "dropped":
            return in_price(l) and _dropped_recently(l)
        return in_price(l) and (a > FRESH_DAYS if age == "old" else (age == "all" or a <= float(age)))
    trusted = [l for l in d["trusted"] if keep(l)]
    rough = [l for l in d["rough"] if keep(l)]
    order = {"new": _fresh_order,
             "discount": lambda l: -l["comp"]["pct"],
             "savings": lambda l: -l["steal"]["saved"],
             # cheapest delivered: price + listed shipping (per piece)
             "cheap": lambda l: (l["steal"].get("unit") or 0)
             + ((l.get("shipping") or 0) / max(1, (l.get("price_ctx") or {}).get("qty") or 1))}
    trusted.sort(key=order.get(sort, order["new"]))
    rough.sort(key=order.get(sort, order["new"]))
    counts = {k: sum(1 for l in d["trusted"] if in_price(l) and (
        _dropped_recently(l) if k == "dropped" else
        (lambda a: a > FRESH_DAYS if k == "old" else (k == "all" or a <= float(k)))(l.get("age_days") or 0)))
        for k, _ in DEAL_AGES}
    _verify_top_listings()
    return render_template("deals.html", trusted=trusted, rough=rough, age=age, sort=sort, ages=DEAL_AGES,
                           price=price, prices=DEAL_PRICES, counts=counts, rebuilding=_deals_state["running"])


@app.route("/steals/status")
def steals_status():
    """Whether the Steals list is up to date (kicks off a rebuild if not)."""
    try:
        min_pct = int(request.args.get("pct") or 50)
    except ValueError:
        min_pct = 50
    if _deals_cache["data"] is not None:
        _all_deals()
    return {"rebuilding": _deals_state["running"]}


@app.route("/steals")
def steals():
    try:
        min_pct = int(request.args.get("pct") or 50)
    except ValueError:
        min_pct = 50
    min_pct = min_pct if min_pct in STEAL_LEVELS else 50
    data = _steals(min_pct)
    _verify_top_listings()
    # Newest posts first; ads up 30+ days get their own section at the bottom.
    age = lambda l: l.get("age_days") if l.get("age_days") is not None else 999
    fresh = sorted((l for l in data["trusted"] if age(l) <= FRESH_DAYS), key=_fresh_order)
    old = sorted((l for l in data["trusted"] if age(l) > FRESH_DAYS), key=lambda l: -l["comp"]["pct"])
    rough = sorted(data["rough"], key=age)
    return render_template("steals.html", trusted=fresh, old=old, rough=rough,
                           min_pct=min_pct, levels=STEAL_LEVELS)


@app.route("/outboard")
def outboard_page():
    """Outboard: classic comps / pres / EQs from the owner's buy list asking under used value."""
    from scrapers import outboard
    rows = []
    try:
        found = {r["url"]: r for r in _cached_find("outboard", outboard.find, load_config_raw())}
        for l in _decorate(list(found.values())):
            src = found.get(l.get("url")) or {}
            l["outboard"], l["deal"] = src.get("outboard"), src.get("deal")
            l["_when"] = src.get("_when", "")
            rows.append(l)
        rows.sort(key=lambda r: (str(r["_when"])[:10], (r.get("deal") or {}).get("pct") or 0), reverse=True)
    except Exception:
        app.logger.exception("outboard page")
    return render_template("outboard.html", rows=rows, min_pct=outboard.MIN_PCT, days=outboard.DAYS)


_group_cache: dict = {"key": None, "at": 0.0, "data": None, "running": False}


def _telex_group_lists(cfg) -> Optional[list]:
    """Imported model groups on the Telex List, each as one combined list
    (cheapest first, 10%+ under comp). Hundreds of models take ~30s to
    match, so this is rebuilt in the background every 15 minutes (or when
    the group changes) and the page shows the last result meanwhile.
    None = not ready yet."""
    from scrapers.telex import groups as telex_groups
    gs = telex_groups(cfg)
    key = tuple((n, tuple(ts)) for n, ts in gs)
    fresh = _group_cache["key"] == key and time.time() - _group_cache["at"] < 900
    if not fresh and not _group_cache["running"]:
        def build():
            _group_cache["running"] = True
            try:
                out = []
                with app.test_request_context("/telex"):
                    for name, ts in gs:
                        good, unpriced, seen_g, seen_u = [], [], set(), set()
                        for term, g, u in _telex_matches(ts, strict=True, note=False):
                            for r in g:
                                if r["url"] not in seen_g:
                                    seen_g.add(r["url"])
                                    r["telex_term"] = term
                                    good.append(r)
                            for r in u:
                                if r["url"] not in seen_u:
                                    seen_u.add(r["url"])
                                    unpriced.append(r)
                        from scrapers.enrich import parse_price
                        good.sort(key=lambda r: parse_price(r.get("price")) or 0)
                        hit_terms = {r["telex_term"] for r in good}
                        out.append({"name": name, "terms": ts, "good": good,
                                    "unpriced": [r for r in unpriced if r["url"] not in seen_g],
                                    "hit_terms": len(hit_terms)})
                _group_cache.update(key=key, at=time.time(), data=out)
                try:
                    Path("/data/shown_telex_groups.txt").write_text(
                        "\n".join(dict.fromkeys(r["url"] for g in out for r in g["good"][:60])))
                except OSError:
                    pass
            except Exception:
                logging.exception("Telex group matching failed")
            finally:
                _group_cache["running"] = False
        threading.Thread(target=build, name="telex-groups", daemon=True).start()
    if _group_cache["data"] is None:
        return None
    # Groups removed since the last build drop off right away.
    names = {n for n, _ in gs}
    return [g for g in _group_cache["data"] if g["name"] in names]


@app.route("/telex/group/remove", methods=["POST"])
def telex_group_remove():
    name = request.form.get("name", "")
    cfg = load_config_for_edit()
    cfg["telex_groups"] = [g for g in (cfg.get("telex_groups") or []) if g.get("name") != name]
    save_config_raw(cfg)
    _data_changed()
    return redirect(url_for("telex"))


# ---------------------------------------------------------------------------
# Sweetwater stepper: Sweetwater's used gear (Gear Exchange) blocks automated
# browsers with a "press & hold" human check, so Gear Scout can't search it
# itself. This page walks your Telex terms one at a time and opens each
# search in a Sweetwater tab in YOUR browser (on a timer or with Next), and
# remembers which terms you've checked and what you found.
# ---------------------------------------------------------------------------

SWEETWATER_FILE = Path("/data/sweetwater_stepper.json")
SWEETWATER_URL = "https://www.sweetwater.com/used/listings?query={q}"


def _sw_state() -> dict:
    try:
        return json.loads(SWEETWATER_FILE.read_text())
    except Exception:
        return {}


# Sweetwater allows 25 saved searches with email alerts. These 25 cover 57 of
# the Telex terms (one brand search catches several terms); "covers" is
# matched against your current Telex List so the page stays accurate.
SWEETWATER_SAVED = [
    ("Golden Age", ["Golden age", "Ga-47", "Golden age premier", "Golden Age Project R1", "Golden Age Project R1 ST", "golden age r1 tube"]),
    ("Neutrik", ["Neutrik NC3FXX", "Neutrik NC3MXX", "Neutrik NC3FD", "Neutrik NC3MD", "Neutrik NA3FDM", "Neutrik NP3X", "Neutrik NE8FDP", "Neutrik NYS-SPP-L1"]),
    ("Audioscape", ["audioscape", "Audioscape SSL", "audioscape opto", "audioscape as-2a"]),
    ("Hosa", ["Hosa DTP-805", "Hosa CSS-805", "Hosa STX-807F", "Hosa XLR-807"]),
    ("snake", ["DB25 snake", "TRS snake", "XLR snake", "XLR to TRS snake"]),
    ("United Studio Technologies", ["united audio mic", "United twin 48", "united fet 47"]),
    ("Stam", ["stam", "stam sa-47"]),
    ("Peluso", ["peluso", "peluso p24"]),
    ("CAPI", ["CAPI BT-50", "CAPI VM28"]),
    ("Voodoo VR2", ["se voodoo vr2", "Vr2 voodoo pair"]),
    ("WA-19", ["Wa-19", "warm wa-19"]),
    ("BeesNeez", ["beesneez", "beezneez"]),
    ("Flickinger", ["flickinger", "flick 500"]),
    ("ribbon microphone", ["Ribbon mic", "stereo ribbon mic"]),
    ("Distressor", ["Distressor"]),
    ("Avalon 737", ["Avalon 737"]),
    ("HEDD Type 20", ["HEDD 20 MKII"]),
    ("Neumann KH 310", ["KH 310 II"]),
    ("Telefunken TF17", ["telefunken tf17"]),
    ("Tonelux", ["tonelux jc37"]),
    ("AKG D12", ["d12 akg"]),
    ("Vanguard V13", ["Vanguard V13"]),
    ("Seventh Circle Audio", ["seventh circle audio"]),
    ("Sebatron", ["sebatron"]),
    ("Cloudlifter", ["Cloudlifter cl-4"]),
]


@app.route("/sweetwater/setup", methods=["POST"])
def sweetwater_setup():
    """Ticks a saved search off the one-time Sweetwater alert setup list."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("search") or "")[:100]
    if name:
        st = _sw_state()
        done = st.setdefault("setup", {})
        if data.get("done"):
            done[name] = datetime.now(timezone.utc).isoformat()
        else:
            done.pop(name, None)
        try:
            SWEETWATER_FILE.write_text(json.dumps(st))
        except OSError:
            pass
    return jsonify(ok=True)


@app.route("/sweetwater")
def sweetwater():
    from urllib.parse import quote_plus
    from scrapers import wanted
    st = _sw_state()
    marks = st.get("terms") or {}
    rows = []
    for t in wanted.terms(load_config_raw()):
        m = marks.get(t) or {}
        rows.append({"term": t, "url": SWEETWATER_URL.format(q=quote_plus(t)), "checked": m.get("at"),
                     "ago": _ago(m.get("at")), "result": m.get("result"), "note": m.get("note", "")})
    # Least recently checked first, so each pass picks up where you left off.
    rows.sort(key=lambda r: r["checked"] or "")
    checked = sum(1 for r in rows if r["checked"])
    have = {t.lower(): t for t in (r["term"] for r in rows)}
    done = st.get("setup") or {}
    saved = [{"search": s, "url": SWEETWATER_URL.format(q=quote_plus(s)), "done": s in done,
              "covers": [have[c.lower()] for c in cov if c.lower() in have]} for s, cov in SWEETWATER_SAVED]
    covered = {c.lower() for s in saved for c in s["covers"]}
    uncovered = [r["term"] for r in rows if r["term"].lower() not in covered]
    return render_template("sweetwater.html", rows=rows, checked=checked,
                           found=sum(1 for r in rows if r["result"] == "found"),
                           saved=saved, n_covered=len(covered), uncovered=sorted(uncovered, key=str.lower),
                           n_setup=sum(1 for s in saved if s["done"]))


@app.route("/sweetwater/mark", methods=["POST"])
def sweetwater_mark():
    data = request.get_json(silent=True) or request.form
    term = str(data.get("term") or "").strip()[:200]
    result = data.get("result") if data.get("result") in ("found", "none", "opened") else "opened"
    if term:
        st = _sw_state()
        terms = st.setdefault("terms", {})
        prev = terms.get(term) or {}
        terms[term] = {"at": datetime.now(timezone.utc).isoformat(),
                       # Just opening it doesn't undo an earlier "found".
                       "result": prev.get("result") if result == "opened" and prev.get("result") == "found" else result,
                       "note": str(data.get("note") if data.get("note") is not None else prev.get("note", ""))[:300]}
        try:
            SWEETWATER_FILE.write_text(json.dumps(st))
        except OSError:
            pass
    return jsonify(ok=True)


@app.route("/telex")
def telex():
    """Your Telex List (= Wanted list) term by term. Built from the same
    listings as the Feed — every match at any price, newest post first (or
    cheapest first), deals marked — so the two pages always agree."""
    cfg = load_config_raw()
    terms = _telex_terms(cfg)  # entries as you typed them (a few hold several terms)
    from scrapers.enrich import parse_price
    sort = "cheap" if request.args.get("sort") == "cheap" else "new"
    age = lambda r: r.get("age_days") if r.get("age_days") is not None else 999
    unit = lambda r: (r.get("price_ctx") or {}).get("unit_value") or parse_price(r.get("price")) or 1e12
    from scrapers.fb_twins import hide_twins
    items = hide_twins(_feed_items(cfg))  # Facebook ad-twins: newest only (display only)
    _mark_lowest(items)
    view, new_all, feed_counts = [], {}, {}
    for entry in terms:
        parts = {p.strip() for p in entry.split(",") if p.strip()}
        its = [l for l in items if parts & set(l["wanted"])]  # already newest first
        if sort == "cheap":
            its = sorted(its, key=unit)
        # The cheapest working one of each model (the overall cheapest pulses
        # gold) goes first, cheapest first, however old.
        lowest, rest = _pinned_first(its, parts)
        if sort == "cheap":
            # Cheapest first means every ad still for sale, however old.
            fresh, old = lowest + sorted(rest, key=lambda r: r.get("total") or r.get("unit") or 1e12), []
        else:
            fresh = lowest + [r for r in rest if age(r) <= FRESH_DAYS]
            old = [r for r in rest if age(r) > FRESH_DAYS]
        new = [r for r in fresh if age(r) <= 2]
        for r in new:
            new_all.setdefault(r["url"], (r, entry))
        feed_counts[entry] = len(its)
        view.append({"term": entry, "all_items": its, "fresh": fresh, "old": old, "unpriced": [],
                     "new_count": len(new)})
    view.sort(key=lambda v: v["new_count"] == 0)  # stable: keeps your order otherwise
    new_items = sorted(new_all.values(), key=lambda x: age(x[0]))
    from scrapers.telex import state as telex_state
    from scrapers.telex import groups as telex_groups
    try:
        from scrapers.telex_suggest import suggest_terms
        sharper = suggest_terms(terms, [f.get("title") for f in favorite_listings(limit=300)],
                                term_for=_telex_term_for)
    except Exception:
        logging.exception("Sharper terms failed")
        sharper = []
    return render_template("telex.html", sharper=sharper, feed_counts=feed_counts, terms=terms, view=view, new_items=new_items,
                           sort=sort, total=sum(feed_counts.values()), sweep=telex_state(),
                           model_groups=_telex_group_lists(cfg), has_groups=bool(telex_groups(cfg)))


TELEX_STATUS_PATH = Path("/data/telex_status.json")
_telex_thread = None


def _run_telex_job(terms: list[str], fast_only: bool):
    from scrapers.telex import search_term
    started = time.time()
    cfg = load_config_raw()
    found = new = 0

    def write(state, **extra):
        TELEX_STATUS_PATH.write_text(json.dumps({"state": state, "started": started, "found": found,
                                                 "new": new, "fast_only": fast_only, **extra}))
    try:
        try:
            from scrapers.market import refresh_telex_term_values
            refresh_telex_term_values(terms)
        except Exception:
            logging.exception("Telex term price lookup failed")
        # Fast sites answer in a second or two, so several terms run at once;
        # a Facebook search (one term, "Search everywhere") runs on its own.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        done = 0
        write("running", done=0, total=len(terms), current_source=terms[0])
        with ThreadPoolExecutor(max_workers=3 if fast_only else 1, thread_name_prefix="telex") as pool:
            futures = {pool.submit(search_term, term, cfg, None, fast_only): term for term in terms}
            for fut in as_completed(futures):
                res = fut.result()
                found += res["found"]
                new += res["new"]
                done += 1
                write("running", done=done, total=len(terms), current_source=futures[fut])
        write("done", done=len(terms), total=len(terms))
        _data_changed()
        _terms_changed(terms)  # their Price Board rows include what the search just found
    except Exception as e:
        logging.exception("Telex search failed")
        write("failed", reason=str(e))


@app.route("/telex/scrape", methods=["POST"])
def telex_scrape():
    """Search all Telex terms on the fast sites, or one term everywhere."""
    global _telex_thread
    from scrapers.telex import terms as telex_terms
    one = request.form.get("term", "").strip()
    terms = [one] if one else telex_terms(load_config_raw())
    if terms and (_telex_thread is None or not _telex_thread.is_alive()):
        TELEX_STATUS_PATH.write_text(json.dumps({"state": "running", "started": time.time(),
                                                 "done": 0, "total": len(terms)}))
        _telex_thread = threading.Thread(target=_run_telex_job, args=(terms, not one), daemon=True)
        _telex_thread.start()
    return redirect(url_for("telex"))


@app.route("/telex/status")
def telex_status():
    try:
        st = json.loads(TELEX_STATUS_PATH.read_text())
    except Exception:
        return {"state": "idle"}
    # "Running" with no search actually running here (the dashboard was
    # restarted mid-search): say so instead of spinning forever.
    if st.get("state") == "running" and (_telex_thread is None or not _telex_thread.is_alive()):
        st = {"state": "failed", "reason": "That search was interrupted (Gear Scout restarted) — run it again."}
        TELEX_STATUS_PATH.write_text(json.dumps(st))
    return st


def _terms_changed(added: list[str] | None = None) -> None:
    """After a Telex term is added or removed: update only what that term
    touches — its Price Board row (in the background, a few seconds) — instead
    of recalculating everything. The Feed, the Telex page and the deals
    (which follow the Wanted list) refresh on their next load."""
    _data_changed()
    # Several adds/removes in a row (one after the other) used to start one
    # minute-long Price Board rebuild EACH, all at once — the site hung.
    # Now: one rebuild, a little after the last change.
    with _board_lock:
        _board_state["due"] = time.time() + 20
        _board_state["terms"] |= set(added or [])
        if _board_state["running"]:
            return
        _board_state["running"] = True

    def run():
        try:
            while True:
                wait = _board_state["due"] - time.time()
                if wait > 0:
                    time.sleep(min(wait, 5))
                    continue
                with _board_lock:
                    terms = list(_board_state["terms"])
                    _board_state["terms"] = set()
                    due = _board_state["due"]
                try:
                    from scrapers import board
                    board.build(load_config_raw(), only=terms)
                except Exception:
                    logging.exception("Price Board update for changed terms failed")
                with _board_lock:
                    if _board_state["due"] == due and not _board_state["terms"]:
                        _board_state["running"] = False
                        return
        except Exception:
            _board_state["running"] = False
    threading.Thread(target=run, name="board-terms", daemon=True).start()


_board_lock = threading.Lock()
_board_state: dict = {"due": 0.0, "terms": set(), "running": False}


@app.route("/telex/add", methods=["POST"])
def telex_add():
    # No full recalculation: the Telex page works out only the new term(s);
    # Dashboard and Deals pick the term up on their regular refresh.
    new = [l.strip() for l in request.form.get("terms", "").splitlines() if l.strip()]
    if new:
        cfg = load_config_for_edit()
        terms = list(cfg.get("telex_list") or [])
        lower = {str(t).lower() for t in terms}
        for t in new:
            if t.lower() not in lower:
                terms.append(t)
                lower.add(t.lower())
        cfg["telex_list"] = terms
        save_config_raw(cfg)
        _terms_changed(new)
        # Search the new term(s) right away on the fast sites and eBay
        # (Facebook follows in the all-day rotation), so they show up on the
        # Feed within a minute instead of at the next 30-minute cycle.
        global _telex_thread
        if _telex_thread is None or not _telex_thread.is_alive():
            TELEX_STATUS_PATH.write_text(json.dumps({"state": "running", "started": time.time(),
                                                     "done": 0, "total": len(new)}))
            _telex_thread = threading.Thread(target=_run_telex_job, args=(new, True), daemon=True)
            _telex_thread.start()
    return redirect(url_for("telex"))


def _telex_term_for(title: str) -> Optional[str]:
    """The Telex term for a listing: brand + model ("neumann U87 ai"), or
    its first few meaningful words when there's no model number."""
    from scrapers.enrich import model_query, title_query
    q = model_query(title)
    if q:
        return q.strip()
    words = (title_query(title) or "").split()
    return " ".join(words[:4]) or None


@app.route("/telex/add-from-listing", methods=["POST"])
def telex_add_from_listing():
    """One-click "add to Telex" from any listing card."""
    global _telex_thread
    term = _telex_term_for(request.form.get("title", ""))
    if not term:
        return jsonify({"ok": False, "message": "Couldn't work out a search term for this one."}), 400
    cfg = load_config_for_edit()
    terms = list(cfg.get("telex_list") or [])
    already = term.lower() in (str(t).lower() for t in terms)
    if not already:
        cfg["telex_list"] = terms + [term]
        save_config_raw(cfg)
        _terms_changed([term])
        # Search it right away on the fast sites (the hourly rotation adds
        # Facebook later), if no other Telex search is running.
        if _telex_thread is None or not _telex_thread.is_alive():
            TELEX_STATUS_PATH.write_text(json.dumps({"state": "running", "started": time.time(),
                                                     "done": 0, "total": 1}))
            _telex_thread = threading.Thread(target=_run_telex_job, args=([term], True), daemon=True)
            _telex_thread.start()
    return jsonify({"ok": True, "term": term, "already": already,
                    "message": (f"“{term}” is already on your Telex List" if already
                                else f"Added “{term}” to your Telex List — searching now")})


@app.route("/telex/remove", methods=["POST"])
def telex_remove():
    term = request.form.get("term", "").strip()
    cfg = load_config_for_edit()
    cfg["telex_list"] = [t for t in (cfg.get("telex_list") or []) if str(t).strip() != term]
    save_config_raw(cfg)
    _terms_changed([])
    return redirect(url_for("telex"))


@app.route("/whats-new")
def whats_new():
    try:
        entries = json.loads((Path(app.static_folder) / "changelog.json").read_text())
    except Exception:
        entries = []
    return render_template("whats_new.html", entries=entries)


@app.route("/ask")
def ask_page():
    cfg = load_config_raw()
    from scrapers.assistant import list_alerts
    return render_template("ask.html", has_key=bool(((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()),
                           alerts=list_alerts())


def _answer_html(text: str, n: int) -> str:
    """Gemini's answer as simple HTML: paragraphs, **bold**, bullet lines,
    and [#n] citations as links to the listing cards below."""
    from html import escape
    t = escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    turn = request.form.get("turn", "0")
    t = re.sub(r"\[#(\d+)\]", lambda m: f'<a href="#ask-{turn}-{m.group(1)}">[#{m.group(1)}]</a>'
               if 1 <= int(m.group(1)) <= n else m.group(0), t)
    lines, html, in_list = t.split("\n"), [], False
    for line in lines:
        bullet = re.match(r"\s*[-*•]\s+(.*)", line)
        if bullet:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{bullet.group(1)}</li>")
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            if line.strip():
                html.append(f"<p>{line}</p>")
    if in_list:
        html.append("</ul>")
    return "".join(html)


def _assistant_add_telex(term: str):
    cfg = load_config_for_edit()
    terms = list(cfg.get("telex_list") or [])
    if term.lower() not in (str(t).lower() for t in terms):
        cfg["telex_list"] = terms + [term]
        save_config_raw(cfg)
        _terms_changed([term])


def _assistant_live_search(term: str):
    global _live_search_thread
    if _live_search_thread is None or not _live_search_thread.is_alive():
        _write_live_search_status({"state": "running", "query": term, "done": 0, "total": 0, "current_source": None})
        _live_search_thread = threading.Thread(target=_run_live_search_job, args=(term,), daemon=True)
        _live_search_thread.start()


@app.route("/ai/verdict", methods=["POST"])
def ai_verdict():
    from scrapers.assistant import verdict
    try:
        return jsonify({"ok": True, **verdict(request.form.get("global_id", ""), load_config_raw())})
    except (PermissionError, RuntimeError) as e:
        return jsonify({"ok": False, "message": str(e)})
    except Exception:
        logging.exception("AI verdict failed")
        return jsonify({"ok": False, "message": "Something went wrong talking to Gemini — try again."})


@app.route("/ask/alerts/remove", methods=["POST"])
def ask_alert_remove():
    from scrapers.assistant import remove_alert
    remove_alert(request.form.get("term", ""))
    return redirect(url_for("ask_page"))


@app.route("/ask/query", methods=["POST"])
def ask_query():
    import scrapers.assistant as A
    A.add_telex_term, A.start_live_search = _assistant_add_telex, _assistant_live_search
    try:
        history = json.loads(request.form.get("history") or "[]")
        prev_ids = json.loads(request.form.get("prev_ids") or "[]")
    except ValueError:
        history, prev_ids = [], []
    try:
        res = A.ask(request.form.get("q", ""), load_config_raw(), history=history, previous_ids=prev_ids)
    except Exception as e:
        logging.exception("Ask AI failed")
        return render_template("_ask_answer.html", error=str(e) if isinstance(e, (PermissionError, RuntimeError))
                               else "Something went wrong talking to Gemini — try again.")
    _decorate(res["listings"])  # adds display fields in place
    return render_template("_ask_answer.html", answer_html=_answer_html(res["answer"], len(res["listings"])),
                           listings=res["listings"], plan=res.get("plan") or {}, error=None,
                           question=request.form.get("q", ""), answer_text=res["answer"],
                           ids=[l.get("global_id") for l in res["listings"]],
                           turn=request.form.get("turn", "0"))


@app.route("/settings/ai", methods=["POST"])
def settings_ai():
    key = request.form.get("gemini_api_key", "").strip()
    cfg = load_config_for_edit()
    ai = cfg.setdefault("ai", {})
    if request.form.get("remove") == "1":
        ai["gemini_api_key"] = ""
    elif key:
        ai["gemini_api_key"] = key
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="ai"))


@app.route("/learning")
def learning_page():
    from scrapers import learning as L
    cfg = load_config_raw()
    t = L.taste(max_age_seconds=0)
    enabled = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    _, skipped = L.skip_on_light_runs(enabled)
    return render_template(
        "learning.html",
        taste=t, min_favs=L.MIN_FAVORITES_FOR_TASTE,
        likes=t.top(24, positive=True), dislikes=t.top(16, positive=False),
        terms=L.learned_terms(),
        groups=L.group_suggestions(cfg),
        excludes=L.exclude_suggestions(cfg),
        board=L.source_scoreboard(), skipped=set(skipped),
        fast=L.fast_sellers(), sales_known=len(L.sale_stats()),
        fb_terms=_fb_term_learning(),
    )


def _fb_term_learning() -> dict:
    from scrapers import fb_posts_background as fbbg
    from scrapers.store import _conn
    with _conn() as conn:
        stats = fbbg._term_stats(conn)
    productive = sorted(((t, s) for t, s in stats.items() if s["hits"]),
                        key=lambda x: -x[1]["hits"])[:15]
    quiet = sum(1 for s in stats.values() if fbbg.term_weight(s) < 1)
    return {"productive": productive, "quiet": quiet, "searched": len(stats)}


@app.route("/learning/term/remove", methods=["POST"])
def learning_term_remove():
    from scrapers.learning import block_term
    term = request.form.get("term", "").strip()
    if term:
        block_term(term)
    return redirect(url_for("learning_page"))


@app.route("/learning/group/add", methods=["POST"])
def learning_group_add():
    url = request.form.get("url", "").strip()
    if re.match(r"^https://www\.facebook\.com/groups/[^/?#]+$", url):
        cfg = load_config_for_edit()
        if not any((s.get("url") or "").rstrip("/") == url for s in cfg.get("sources", [])):
            cfg.setdefault("sources", []).append({
                "name": f"FB group — {url.rsplit('/', 1)[-1]}", "url": url, "type": "facebook", "enabled": True,
            })
            save_config_raw(cfg)
    return redirect(url_for("learning_page"))


@app.route("/learning/exclude/add", methods=["POST"])
def learning_exclude_add():
    word = request.form.get("word", "").strip()
    if word:
        cfg = load_config_for_edit()
        words = list(cfg.get("exclude_words") or [])
        if word.lower() not in (str(w).lower() for w in words):
            cfg["exclude_words"] = words + [word]
            save_config_raw(cfg)
    return redirect(url_for("learning_page"))


@app.route("/learning/dismiss", methods=["POST"])
def learning_dismiss():
    from scrapers.learning import dismiss
    kind, value = request.form.get("kind", ""), request.form.get("value", "").strip()
    if kind in ("group", "exclude") and value:
        dismiss(kind, value)
    return redirect(url_for("learning_page"))


# ---------------------------------------------------------------------------
# Feed — the home page: everything for sale that matches your Wanted list
# (Telex List), newest post first, with what's new since you last looked on
# top. One list, one set of rules (scrapers/wanted.py); deals and steals are
# badges and filters on it rather than separate pages.
# ---------------------------------------------------------------------------

FEED_VISIT_FILE = Path("/data/feed_visit.json")
FEED_RECENT_DAYS = 14   # posted within this → main list; older → collapsed section
FEED_STEAL_PCT = 50  # same as steal alerts (scrapers/steals.py MIN_PCT)
_feed_cache: dict = {"key": None, "at": 0.0, "data": None}


def _feed_visit() -> Optional[str]:
    """Your previous Feed visit (UTC ISO). Coming back after 30+ minutes
    starts a new visit; reloading in between keeps the same "new" line."""
    now = datetime.now(timezone.utc)
    try:
        v = json.loads(FEED_VISIT_FILE.read_text())
    except Exception:
        v = {}
    if request.headers.get("X-GearScout-Test") or request.headers.get("X-Warm"):
        return v.get("previous")
    current = v.get("current")
    if not current or now - datetime.fromisoformat(current) > timedelta(minutes=30):
        v["previous"] = current
    v["current"] = now.isoformat()
    try:
        FEED_VISIT_FILE.write_text(json.dumps(v))
    except OSError:
        pass
    return v.get("previous")


def _ago(iso: Optional[str]) -> Optional[str]:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    mins = int((datetime.now(timezone.utc) - d).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}m ago"
    if mins < 48 * 60:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def _mark_lowest(items: list[dict]) -> None:
    """(Kept for callers; the marks are set when the Feed is built.)"""
    return None


def _mark_picks(items: list[dict], cfg) -> None:
    """For every term: the cheapest working one of each model among exactly
    these listings (scrapers/models.py) — the term's overall cheapest pulses
    gold, each model's cheapest gets a gold "Lowest <model>" tag. Worked out
    from the list itself, so a new cheaper ad is marked as soon as it's
    listed; the scraper re-checks the marked ones are still for sale."""
    from scrapers import models, wanted
    from scrapers.enrich import parse_price, quantity
    by_term: dict[str, list] = {}
    for l in items:
        l["lowest_for"], l["model_lowest"], l["model_lowest_label"] = None, None, None
        ctx = l.get("price_ctx") or {}
        l["unit"] = ctx.get("unit_value") or (
            (parse_price(l.get("price")) or 0) / max(1, quantity(l.get("title"), l.get("description"))))
        l["qty"] = ctx.get("qty") or 1
        l["total"] = models.landed(l)  # price + known shipping, per piece
        for t in l.get("wanted") or []:
            by_term.setdefault(t, []).append(l)
    by_url = {l["url"]: l for l in items}
    marked = set()
    exclude = cfg.get("exclude_words") or []
    for term in wanted.terms(cfg):
        ps = models.picks(term, by_term.get(term, []), exclude)
        for i, p in enumerate(ps):
            l = by_url.get(p["url"])
            if not l:
                continue
            marked.add(p["url"])
            if i == 0:
                l["lowest_for"] = (l["lowest_for"] or []) + [term]
            l["model_lowest"] = {**(l["model_lowest"] or {}), term: p["model"]}
    for l in items:
        names = list(dict.fromkeys((l.get("model_lowest") or {}).values()))
        l["model_lowest_label"] = ", ".join(names[:2]) if names else None
    try:
        Path("/data/lowest_picks.txt").write_text("\n".join(sorted(marked)))
    except OSError:
        pass


def _pinned_first(its: list[dict], parts: set) -> tuple[list, list]:
    """(the cheapest of each model for these terms — cheapest first — , the
    rest). The overall cheapest is simply the first of them."""
    unit = lambda r: r.get("total") or r.get("unit") or 1e12  # price + shipping per piece, as the picks use
    pinned = [r for r in its if parts & (set(r.get("lowest_for") or []) | set((r.get("model_lowest") or {}).keys()))]
    pinned.sort(key=unit)
    return pinned, [r for r in its if r not in pinned]


def _feed_items(cfg) -> list[dict]:
    """Every Wanted match still for sale, decorated for display, newest post
    first. Rebuilt when a scrape or sweep finishes, or every 3 minutes."""
    from scrapers import wanted
    from scrapers.enrich import parse_price
    marker = _last_change_marker()
    try:
        marker += str(int(Path("/data/last_sweep.json").stat().st_mtime))
    except OSError:
        pass
    key = (_data_version[0], marker, tuple(wanted.terms(cfg)))
    c = _feed_cache
    if c["data"] is None:
        snap = _snapshot_load("feed")  # right after a restart: the saved copy, instantly
        if snap is not None:
            c.update(key=None, at=0.0, data=snap)
    if c["data"] is not None and c["key"] == key and time.time() - c["at"] < 180:
        return c["data"]
    if c["data"] is not None and not _feed_state.get("rebuilding"):
        # Something changed: show the current list right away and rebuild in
        # the background — rebuilding inside the request made the page hang
        # for a minute while a scrape was using the CPU.
        same_terms = c["key"] is None or (len(c["key"]) > 2 and c["key"][2] == key[2])
        if same_terms:
            def rebuild():
                _feed_state["rebuilding"] = True
                try:
                    _build_feed(cfg, key)
                except Exception:
                    logging.exception("Feed rebuild failed")
                finally:
                    _feed_state["rebuilding"] = False
            threading.Thread(target=rebuild, name="feed-rebuild", daemon=True).start()
            return c["data"]
    elif c["data"] is not None and _feed_state.get("rebuilding") and c["key"] is not None \
            and len(c["key"]) > 2 and c["key"][2] == key[2]:
        return c["data"]
    if c["data"] is not None:
        # Your terms changed (added / removed): show the current list right
        # away — minus removed terms — and rebuild in the background, instead
        # of making the page wait (several deletes in a row stalled it).
        current = set(key[2])
        stale = [l for l in c["data"] if set(l.get("wanted") or []) & current]
        if not _feed_state.get("rebuilding"):
            def rebuild_terms():
                _feed_state["rebuilding"] = True
                try:
                    _build_feed(load_config_raw(), (_data_version[0], key[1], tuple(wanted.terms(load_config_raw()))))
                except Exception:
                    logging.exception("Feed rebuild failed")
                finally:
                    _feed_state["rebuilding"] = False
            threading.Thread(target=rebuild_terms, name="feed-rebuild-terms", daemon=True).start()
        return stale
    return _build_feed(cfg, key)


_feed_state: dict = {"rebuilding": False}


def _build_feed(cfg, key) -> list[dict]:
    from scrapers import wanted
    from scrapers.enrich import parse_price
    c = _feed_cache
    ms = wanted.matchers(cfg)
    items, seen_ads = [], set()
    for r in wanted.recent_rows():
        found = wanted.match(r.get("title"), r.get("price"), ms, r.get("source_name"))
        if not found:
            continue
        ad = ((r.get("title") or "").lower().strip(), r.get("price"))
        if ad in seen_ads:  # the same ad stored twice (another link to it)
            continue
        seen_ads.add(ad)
        r["wanted"] = found
        items.append(r)
    # Display fields and the shared junk filters, independent of this
    # request's own filters (those are applied afterwards).
    with app.test_request_context("/", headers={"X-Warm": "1"}):
        items = _decorate(items)
    # Deals and steals: the same calculation the Deals and Steals pages, the
    # digest and the steal alerts use (scrapers/steals.py: 10%+ under the most
    # trustworthy comp, double-checked against other listings of the same
    # gear) — so a listing shows the same "% under" everywhere.
    with app.test_request_context("/deals"):
        deals = _all_deals() or {}
    comp_by_url = {d["url"]: d["comp"] for d in deals.get("trusted") or []}
    # Steals with only one price source: shown marked "double-check".
    check_by_url = {d["url"]: d["comp"] for d in deals.get("rough") or []
                    if FEED_STEAL_PCT <= d["comp"]["pct"] <= 90}  # 80–90% "too good": shown, to double-check
    out = []
    for l in items:
        ctx = l.get("price_ctx") or {}
        typical = parse_price(ctx.get("typical")) if ctx.get("typical") and not ctx.get("rough") else None
        value = ctx.get("unit_value") or parse_price(l.get("price"))
        if typical and value and value < typical * 0.15:
            continue  # a part or plugin with a vague title
        comp = comp_by_url.get(l["url"])
        l["comp"] = comp
        l["is_deal"] = bool(comp)
        l["deal_pct"] = comp["pct"] if comp else None
        l["is_steal"] = bool(comp and comp["pct"] >= FEED_STEAL_PCT and not comp["est"])
        check = None if comp else check_by_url.get(l["url"])
        if check:
            l["comp"], l["is_steal"], l["steal_check"] = check, True, True
            l["deal_pct"] = check["pct"]
        if ctx:
            ctx["deal"] = False  # the card's "% under" comes from l.comp, as everywhere else
        l["checked_label"] = _ago(l.get("confirmed_live_at"))
        out.append(l)
    _mark_picks(out, cfg)
    # Newest post first; ties (same posting time, or no posting time) go to
    # the one Gear Scout found most recently.
    out.sort(key=lambda l: l.get("first_seen") or "", reverse=True)
    out.sort(key=lambda l: l["age_days"] if l.get("age_days") is not None else 999)
    try:
        Path("/data/shown_feed.txt").write_text("\n".join(l["url"] for l in out[:400] if l.get("url")))
        # Tell the hourly refresh which of your listings still have no usual
        # price, so it looks those up first (Reverb, eBay when allowed).
        Path("/data/nocomp_titles.txt").write_text("\n".join(dict.fromkeys(
            l["title"] for l in out if l.get("title") and not (l.get("price_ctx") or {}).get("typical")
            and not l.get("comp"))))
    except OSError:
        pass
    c.update(key=key, at=time.time(), data=out)
    _snapshot_save("feed", out)
    return out


INBOX_DONE_FILE = Path("/data/inbox_done.json")
INBOX_DROP_DAYS = 3      # a price drop stays in the inbox this long
INBOX_DROP_PCT = 10      # ...and only cuts this big count
INBOX_AUCTION_HOURS = 48  # auctions ending within this show up


def _inbox_done() -> dict:
    try:
        return json.loads(INBOX_DONE_FILE.read_text())
    except Exception:
        return {}


@app.route("/inbox/done", methods=["POST"])
def inbox_done():
    """✓ Done on a front-page card (or a whole section): it leaves the inbox
    for good. The listing itself stays everywhere else."""
    keys = request.form.getlist("key") or (request.get_json(silent=True) or {}).get("keys") or []
    done = _inbox_done()
    now = datetime.now(timezone.utc)
    for k in keys:
        if k:
            done[str(k)[:600]] = now.isoformat()
    # Forget entries older than 120 days so the file stays small.
    cutoff = (now - timedelta(days=120)).isoformat()
    done = {k: v for k, v in done.items() if v >= cutoff}
    try:
        INBOX_DONE_FILE.write_text(json.dumps(done))
    except OSError:
        pass
    if request.is_json or request.headers.get("X-Requested-With"):
        return jsonify(ok=True)
    return redirect(url_for("feed"))


def _inbox(items: list[dict], since: Optional[str]) -> list[dict]:
    """The front page: only what needs your attention, most urgent first.
    Each listing shows in one section; ✓ Done removes it from the inbox."""
    done = _inbox_done()
    now = datetime.now(timezone.utc)
    age = lambda l: l["age_days"] if l.get("age_days") is not None else 999
    used, sections = set(), []
    # Listings marked done in any section ("steal:<url>", "drop:<url>:<price>",
    # "fav:<url>:<price>:<pending>"): they don't come back as a steal or as
    # new — only a fresh event (an auction ending, another price cut) does.
    done_urls = set()
    for k in done:
        kind, _, rest = k.partition(":")
        extra = {"drop": 1, "fav": 2}.get(kind)
        done_urls.add(rest.rsplit(":", extra)[0] if extra else rest)

    def add(key, title, note, rows, keyf, once=False):
        out = []
        for l in rows:
            k = keyf(l)
            if l.get("url") in used or k in done or l.get("sold") or l.get("hidden") \
                    or (once and l.get("url") in done_urls):
                continue
            used.add(l.get("url"))
            out.append((l, k))
        sections.append({"id": key, "title": title, "note": note, "rows": out})

    def ends(l):
        m = re.search(r"ends (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", l.get("description") or "")
        if not m:
            return None
        try:  # auction end times are written in Pacific time
            from zoneinfo import ZoneInfo
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        except ValueError:
            return None

    soon = sorted((l for l in items if l.get("is_auction") and ends(l)
                   and now < ends(l) <= now + timedelta(hours=INBOX_AUCTION_HOURS)), key=ends)
    for l in soon:
        l["inbox_note"] = "ends " + ends(l).strftime("%a %-I:%M %p").replace(":00 ", " ") + " PT"
    add("auctions", "⏰ Auctions ending soon", f"Ending in the next {INBOX_AUCTION_HOURS} hours.", soon,
        lambda l: f"auction:{l['url']}")
    steals = sorted((l for l in items if l.get("is_steal")), key=lambda l: -(l.get("deal_pct") or 0))
    add("steals", "💥 Steals", f"{FEED_STEAL_PCT}%+ under what this gear usually sells for.", steals,
        lambda l: f"steal:{l['url']}", once=True)
    drop_cut = (now - timedelta(days=INBOX_DROP_DAYS)).isoformat()
    def cut(l):
        from scrapers.enrich import parse_price
        a, b = parse_price(l.get("previous_price")), parse_price(l.get("price"))
        return round(100 * (a - b) / a) if a and b and b < a else 0

    drops = sorted((l for l in items if l.get("previous_price") and (l.get("price_dropped_at") or "") >= drop_cut
                    and cut(l) >= INBOX_DROP_PCT), key=lambda l: l.get("price_dropped_at") or "", reverse=True)
    for l in drops:
        l["inbox_note"] = f"{cut(l)}% cut · was {l['previous_price']}"
    add("drops", "📉 Price drops", f"Cut {INBOX_DROP_PCT}%+ in the last {INBOX_DROP_DAYS} days.", drops,
        lambda l: f"drop:{l['url']}:{l.get('price')}")  # a further cut shows again
    favs = [l for l in items if l.get("favorite") and (l.get("pending")
            or (l.get("previous_price") and (l.get("price_dropped_at") or "") >= drop_cut))]
    for l in favs:
        l["inbox_note"] = "sale pending" if l.get("pending") else f"price cut, was {l['previous_price']}"
    add("favorites", "★ Your favorites", "Something changed on gear you starred.", favs,
        lambda l: f"fav:{l['url']}:{l.get('price')}:{bool(l.get('pending'))}")
    new = [l for l in items if since and (l.get("first_seen") or "") > since and age(l) <= FEED_RECENT_DAYS]
    add("new", "🆕 New since you last looked", "Newest post first.", new, lambda l: f"new:{l['url']}", once=True)
    return sections


@app.route("/mics")
def mics_page():
    """Mic Focus: the mics being hunted right now, best discount first."""
    from scrapers import market_pulse, price_targets
    ctx = _mics_ctx()
    ctx["pulse"] = market_pulse.pulse(load_config_raw())  # weekly trend per mic (read-only, cached 30 min)
    from scrapers import price_history
    ctx["history"] = price_history.history(load_config_raw())  # asking-price history per model (cached 30 min)
    from scrapers import wait_signal
    ctx["waits"] = wait_signal.signals(ctx["history"])  # buy-now / wait / steady badge per model, from that same history
    from scrapers import best_month
    ctx["best_months"] = best_month.best_months(load_config_raw())  # cheapest calendar month per model; empty until the data is thick enough
    from scrapers import regional_gap
    ctx["regional_gaps"] = regional_gap.regional_gaps(load_config_raw())  # same model far cheaper in another state (cached 30 min)
    try:
        ctx["price_targets"] = price_targets.status(load_config_raw())
    except Exception:
        app.logger.exception("price targets panel")
        ctx["price_targets"] = []
    try:  # compact pickup-route panel: only from deals already built (never triggers a rebuild)
        pp = _pickup_plan(_deals_cache["data"]) if _deals_cache["data"] is not None else None
        ctx["pickup"] = {"count": pp["count"], "areas": len(pp["clusters"]), "miles": pp["total_miles"]} if pp else None
    except Exception:
        app.logger.exception("pickup route panel")
        ctx["pickup"] = None
    return render_template("mics.html", **ctx)


@app.route("/offers")
def offers_page():
    """Offer log: what you offered, what sellers said, which discounts worked."""
    from scrapers import offer_log
    return render_template("offers.html", s=offer_log.summary(), labels=offer_log.LABELS)


@app.route("/offers/add", methods=["POST"])
def offers_add():
    """Records one offer + reply (only writes /data/offers.json)."""
    from scrapers import offer_log
    f = request.form
    o = offer_log.add(f.get("url"), f.get("title"), f.get("source"), f.get("seller"),
                      f.get("asking"), f.get("offered"), f.get("reply"), f.get("counter"))
    return jsonify(ok=bool(o), discount=offer_log.discount(o) if o else None), (200 if o else 400)


@app.route("/offer")
def offer_page():
    """One-tap instant offer: the page every deal push/email links to."""
    from scrapers import instant_offer as io
    url = (request.args.get("url") or "").strip()
    l = io.lookup(url) if url else None
    if not l:
        return render_template("offer.html", l=None, url=url), 404
    ask = l.get("ask")
    ch = io.channel(url, l.get("offers_ok"), _inject_ebay_connected().get("ebay_connected"))
    amount = io.suggest(ask)
    return render_template("offer.html", l=l, url=url, ask=ask, amount=amount, channel=ch,
                           floor=io.min_offer(ask) if ask else None,
                           msg=io.message(l.get("title"), amount) if amount else "", ship_note=io.SHIP_NOTE)


@app.route("/reverb/offer", methods=["POST"])
def reverb_offer():
    """Sends a real, binding Make-an-Offer on Reverb. Only when the form says
    confirm=1 (the card's second tap); dry_run=1 builds everything but sends
    nothing. (Same open access as the other POST routes; the login gate is off.)"""
    from scrapers import reverb_offer as ro
    f = request.form
    if f.get("confirm") != "1":
        return jsonify(ok=False, message="Confirmation required"), 400
    res = ro.send_offer(f.get("url"), f.get("amount"), dry_run=f.get("dry_run") == "1",
                        title=f.get("title") or "", source="Reverb", seller=f.get("seller") or "",
                        asking=f.get("asking"))
    return jsonify(res)


@app.route("/ebay/offer", methods=["POST"])
def ebay_offer():
    """Sends a real, binding Best Offer on an eBay Buy It Now listing. Same
    rules as /reverb/offer: confirm=1 required, dry_run=1 sends nothing."""
    from scrapers import ebay_offer as eo
    f = request.form
    if f.get("confirm") != "1":
        return jsonify(ok=False, message="Confirmation required"), 400
    res = eo.send_offer(f.get("url"), f.get("amount"), dry_run=f.get("dry_run") == "1",
                        title=f.get("title") or "", source="eBay", seller=f.get("seller") or "",
                        asking=f.get("asking"))
    return jsonify(res)


EBAY_STATE_PATH = "/data/ebay_oauth_state.txt"


def _ebay_save_user_token(refresh_token):
    """Targeted write of ebay_api.user_refresh_token only (None/"" clears it).
    Backs config.yaml up once, next to it, before the first write."""
    bak = CONFIG_PATH + ".bak-ebay"
    try:
        if not os.path.exists(bak):
            shutil.copy2(CONFIG_PATH, bak)
    except OSError:
        app.logger.warning("ebay: could not make a config backup (continuing)")
    cfg = load_config_for_edit()
    ebay_cfg = cfg.setdefault("ebay_api", {})
    ebay_cfg["user_refresh_token"] = refresh_token or ""
    save_config_raw(cfg)


@app.route("/ebay/connect")
def ebay_connect():
    from scrapers import ebay_offer as eo
    try:
        state = secrets.token_urlsafe(24)
        url = eo.consent_url(state)
    except Exception:
        url = None
    if not url:
        return redirect(url_for("settings", saved="ebay-needs-runame"))
    try:
        with open(EBAY_STATE_PATH, "w") as f:
            f.write("%s %d" % (state, int(time.time())))
    except OSError:
        return redirect(url_for("settings", saved="ebay-error"))
    return redirect(url)


@app.route("/ebay/callback")
def ebay_callback():
    from scrapers import ebay_offer as eo
    code = request.args.get("code")
    state = request.args.get("state") or ""
    try:
        with open(EBAY_STATE_PATH) as f:
            saved_state, ts = f.read().split()
        os.remove(EBAY_STATE_PATH)
    except (OSError, ValueError):
        saved_state, ts = "", "0"
    if (not code or not saved_state or not secrets.compare_digest(saved_state, state)
            or time.time() - int(ts) > 900):
        app.logger.warning("ebay callback: missing code or bad/expired state")
        return redirect(url_for("settings", saved="ebay-error"))
    try:
        tok = eo.exchange_code(code)
        _ebay_save_user_token(tok["refresh_token"])
    except Exception as e:
        app.logger.warning("ebay callback failed: %s", type(e).__name__ + ": " + str(e)[:200])
        return redirect(url_for("settings", saved="ebay-error"))
    return redirect(url_for("settings", saved="ebay-connected"))


@app.route("/ebay/disconnect", methods=["POST"])
def ebay_disconnect():
    from scrapers import ebay_offer as eo
    _ebay_save_user_token("")
    eo.clear_cache()
    return redirect(url_for("settings", saved="ebay"))


@app.route("/settings/ebay-runame", methods=["POST"])
def settings_ebay_runame():
    cfg = load_config_for_edit()
    cfg.setdefault("ebay_api", {})["runame"] = request.form.get("runame", "").strip()
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="ebay"))


@app.context_processor
def _inject_ebay_connected():
    try:
        e = load_config_raw().get("ebay_api") or {}
        return {"ebay_connected": bool(str(e.get("user_refresh_token") or "").strip()
                                       and str(e.get("client_id") or "").strip()
                                       and str(e.get("client_secret") or "").strip())}
    except Exception:
        return {"ebay_connected": False}


@app.route("/offers/remove", methods=["POST"])
def offers_remove():
    from scrapers import offer_log
    offer_log.remove(request.form.get("id") or "")
    return redirect(url_for("offers_page"))


@app.route("/flips")
def flips_page():
    """Flip engine: suggestions only (buy cheap, resell for profit) plus the
    tracker of what you bought and sold. Nothing here posts or contacts anyone."""
    from scrapers import flips
    try:
        items = flips.find(load_config_raw())
    except Exception:
        app.logger.exception("flip suggestions")
        items = []
    owned = flips.bought_urls()
    for r in items:
        r["draft"] = flips.draft(r)
        r["owned"] = r.get("url") in owned
    return render_template("flips.html", items=items, t=flips.summary(), min_profit=flips.MIN_PROFIT,
                           min_pct=flips.MIN_PROFIT_PCT, min_comps=flips.MIN_COMPS)


@app.route("/flips/bought", methods=["POST"])
def flips_bought():
    """"I bought this" (only writes /data/flips.json)."""
    from scrapers import flips
    f = request.form
    flips.bought(f.get("url"), f.get("title"), f.get("source"), f.get("paid"), f.get("expected"))
    return redirect(url_for("flips_page") + "#tracker")


@app.route("/flips/sold", methods=["POST"])
def flips_sold():
    """Sold price + shipping for a flip you bought (only writes /data/flips.json)."""
    from scrapers import flips
    flips.sold(request.form.get("id") or "", request.form.get("price"), request.form.get("ship"))
    return redirect(url_for("flips_page") + "#tracker")


@app.route("/flips/remove", methods=["POST"])
def flips_remove():
    from scrapers import flips
    flips.remove(request.form.get("id") or "")
    return redirect(url_for("flips_page") + "#tracker")


def _seller_watched(source, seller) -> bool:
    from scrapers import seller_watch
    return seller_watch.is_watched(source, seller)


app.jinja_env.globals["seller_watched"] = _seller_watched  # a global so the card macro sees it


@app.route("/sellers")
def sellers_page():
    """Watched sellers and their latest listings (read from the database only)."""
    from scrapers import seller_watch
    rows = []
    for w in seller_watch.load():
        try:
            items = seller_watch.latest(w)
        except Exception:
            app.logger.exception("watched seller listings")
            items = []
        rows.append({**w, "items": items})
    return render_template("sellers.html", rows=rows)


@app.route("/sellers/watch", methods=["POST"])
def sellers_watch():
    """Follows or unfollows a seller (only writes /data/watched_sellers.json)."""
    from scrapers import seller_watch
    f = request.form
    if f.get("unwatch") == "1":
        ok = seller_watch.remove(f.get("source"), f.get("seller"))
    else:
        ok = bool(seller_watch.add(f.get("source"), f.get("seller")))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok), (200 if ok else 400)
    return redirect(url_for("sellers_page"))


@app.route("/price-targets/add", methods=["POST"])
def price_targets_add():
    """Saves a model + the most you'd pay (only writes /data/price_targets.json)."""
    from scrapers import price_targets
    price_targets.add(request.form.get("text"), request.form.get("max_price"))
    return redirect(url_for("mics_page") + "#price-targets")


@app.route("/price-targets/remove", methods=["POST"])
def price_targets_remove():
    from scrapers import price_targets
    price_targets.remove(request.form.get("id") or "")
    return redirect(url_for("mics_page") + "#price-targets")


def _mics_ctx() -> dict:
    """Everything the Mics page and Home show about mics (and outboard
    targets such as the Distressor, which are not mics)."""
    from scrapers import mic_focus
    cfg = load_config_raw()
    st = mic_focus.settings(cfg)
    data = mic_focus.load()
    # Hidden (✕), sold or pending since the list was built: gone right away.
    from scrapers.reposts import collapse_reposts
    rows = collapse_reposts(_decorate(mic_focus.still_showing(data.get("rows") or [])))
    for l in rows:
        l["age_days"] = _age_days(l)
        m = l["mic"]
        if m.get("unsure") and not m.get("used"):
            l["price_ctx"] = dict(l.get("price_ctx") or {}, note=(
                f"No solid used value yet (only {m.get('n') or 0} other listing{'' if m.get('n') == 1 else 's'} of this model)"))
            continue
        if m["pct"] is not None and m["pct"] > 0:
            label = "New" if m["ref_kind"] == "new" else "Used market"
            l["comp"] = {"pct": m["pct"], "ref": f"${m['ref']:,}", "est": False, "label": label}
        elif m["ref"]:
            l["price_ctx"] = dict(l.get("price_ctx") or {}, note=(
                f"{'New' if m['ref_kind'] == 'new' else 'Used market'} ~${m['ref']:,}: "
                + ("about the same" if m["pct"] is not None and m["pct"] > -5 else f"{-m['pct']}% over")))
    # % brands only list their deals; one with no solid used value waits in
    # "Check these" at the end. United, Dachman and the Distressor list everything.
    check = sorted((l for l in rows if l["mic"].get("unsure")), key=lambda l: (l["mic"]["name"], l["mic"]["delivered"]))
    rows = [l for l in rows if not l["mic"].get("unsure")]
    pct_keys = {t["key"] for t in st["targets"] if mic_focus.pct_mode(t)}
    by = {t["key"]: [] for t in st["targets"]}
    for l in rows:
        if l["mic"]["target"] in pct_keys and not l["mic"]["hit"]:
            continue
        by.setdefault(l["mic"]["target"], []).append(l)
    # Each section in the order of the price the card shows (listing +
    # listed shipping) — sorting by per-piece price put a $1,299 pair first.
    from scrapers.enrich import model_key, parse_price

    def shown(l):
        v = parse_price(l.get("price")) or 0
        return v + (l["shipping"] if isinstance(l.get("shipping"), (int, float)) else 0)
    models = {}
    for k, ls in by.items():
        ls.sort(key=shown)
        # Lowest price per model: the cheapest listing of each model, cheapest first.
        groups = {}
        for l in ls:
            q = l["mic"].get("model")
            if q:
                label = q
            else:
                mk = (model_key(l.get("title")) or "").split("|")[0]
                label = mk.split(":")[-1].upper() if mk else "Other"
            # "TLM 103", "TLM-103" and "TLM103" are one model
            g = groups.setdefault(re.sub(r"[\s-]", "", label.lower()), {"label": label, "n": 0, "best": l})
            g["n"] += 1
        models[k] = sorted(groups.values(), key=lambda g: shown(g["best"]))
        for g in models[k]:
            g["price"] = shown(g["best"])            # Highlight the cheapest ad of each model on its card (the "💲 Lowest"
            # tag and border the Price Board uses) — here and in "In your price range".
            if g["n"] > 1:
                g["best"]["model_lowest"] = True
                g["best"]["model_lowest_label"] = g["label"]
    # In your price range, newest first (cheapest-first buried new ones
    # mid-list, so the top never seemed to change); categories stay cheapest first.
    # "Newest" = when the seller posted it, when known: a dealer's 2017 ad that
    # a newly added brand only just turned up goes below this week's ads (Oct 28).
    def listed(l):
        found = l.get("first_seen") or ""
        try:
            p = datetime.fromisoformat(l.get("posted_at") or "")
            p = p if p.tzinfo else p.replace(tzinfo=timezone.utc)
            return min(found, p.astimezone(timezone.utc).isoformat()) if found else p.isoformat()
        except (TypeError, ValueError):
            return found
    outboard_keys = {t["key"] for t in st["targets"] if t.get("outboard")}
    all_hits = sorted((l for l in rows if l["mic"]["hit"]), key=listed, reverse=True)
    hits = [l for l in all_hits if l["mic"]["target"] not in outboard_keys]  # the Distressor etc. aren't mics
    sweep = None
    try:
        info = json.loads(Path("/data/last_sweep.json").read_text())
        sweep = {"ago": _ago(info["finished"]), "new": info.get("new", 0)}
    except Exception:
        pass
    auctions = _decorate(mic_focus.still_showing(data.get("auctions") or []))
    for l in auctions:
        m = l["mic"]
        mins = m.get("mins_left") or 0
        try:  # minutes left as of now, not as of the last sweep
            mins = int((datetime.fromisoformat(m["ends"]) - datetime.now(timezone.utc)).total_seconds() // 60)
        except Exception:
            pass
        l["auction_ends"] = (f"{mins} min" if mins < 90 else f"{mins // 60} h" if mins < 48 * 60 else f"{mins // 1440} days")
        ship = l["shipping"] if isinstance(l.get("shipping"), (int, float)) else 0
        top = max(0, int(m["max_price"] - ship))
        l["price_ctx"] = dict(l.get("price_ctx") or {}, note=(
            f"Bid + shipping ${m['delivered']:,.0f} · max bid ${top:,} to stay "
            + ("50% under used" if m["target"] in pct_keys else "within your limit")))
    auctions = [l for l in auctions if not l["auction_ends"].startswith("-")]
    # Landing: the best deals on everything you hunt (last 150 days), and
    # what's new since your last visit across all of it.
    deals = collapse_reposts(_decorate(mic_focus.still_showing(data.get("deals") or [])))
    for l in deals:
        d = l["deal"]
        l["age_days"] = _age_days(l)
        l["comp"] = {"pct": d["pct"], "ref": f"${d['used']:,}", "est": False,
                     "label": "Sold" if "sales" in (d.get("basis") or "") else "Used market"}
    deals_warn = [l for l in deals if l.get("warn")]
    deals = [l for l in deals if not l.get("warn")]
    since = _visit_marker()
    new = []
    if since:
        seen_new = set()
        for l in deals + all_hits + [l for t in ("united", "dachman") for l in by.get(t, []) if l["mic"]["hit"]]:
            if (l.get("first_seen") or "") > since and l["url"] not in seen_new:
                seen_new.add(l["url"])
                new.append(l)
        new.sort(key=listed, reverse=True)
    from urllib.parse import quote
    mercari = [(q, f"https://www.mercari.com/search/?keyword={quote(q)}&sortBy=2") for q in mic_focus.fb_terms(cfg)]
    return dict(st=st, hits=hits, all_hits=all_hits, by=by, models=models, built=_ago(data.get("built")), built_iso=data.get("built"),
                auctions=auctions, mercari=mercari, deals=deals, deals_warn=deals_warn, new=new, proposals=_proposals_view(),
                since=_ago(since) if since else None, since_iso=since, check=check, pct_keys=pct_keys, outboard_keys=outboard_keys,
                listed=listed, shown=shown, sweep=sweep, terms=mic_focus.sweep_terms(cfg), saved=request.args.get("saved"))


OUTBOARD_MAX_AGE_DAYS = 45
OUTBOARD_PICKUP_MAX_MILES = 500


def _outboard_fresh_near(l: dict) -> bool:
    """Outboard section: ads up to 45 days old; local-pickup ads only within 500 miles of home."""
    age = l.get("age_days")
    if age is None:
        age = _age_days(l)
    if age is None or age > OUTBOARD_MAX_AGE_DAYS:
        return False
    if _is_local_pickup(l.get("source_name") or ""):
        d = l.get("distance")
        if d is not None and d > OUTBOARD_PICKUP_MAX_MILES:
            return False
    return True


_finder_cache: dict = {}
_finder_lock = threading.Lock()


def _cached_find(name: str, fn, cfg, ttl: float = 300):
    """Home's finders each scan the whole listings table; share one result
    for `ttl` seconds (one build at a time, stale copy served meanwhile) so
    page loads and retries can't pile up."""
    ent = _finder_cache.get(name)
    now = time.time()
    if ent and now - ent[0] < ttl:
        return list(ent[1])
    if ent:
        if not _finder_lock.acquire(blocking=False):
            return list(ent[1])
    else:
        _finder_lock.acquire()
    try:
        ent = _finder_cache.get(name)
        if ent and time.time() - ent[0] < ttl:
            return list(ent[1])
        res = fn(cfg)
        _finder_cache[name] = (time.time(), res)
        return list(res)
    finally:
        _finder_lock.release()


def _home_page():
    """Home: new results, smokin' hot deals, outboard (Distressor), a compact
    Mics section, what's coming next, and the "Check these" list last."""
    ctx = _mics_ctx()
    cfg = load_config_raw()
    st, by, listed = ctx["st"], ctx["by"], ctx["listed"]
    outboard_keys = ctx["outboard_keys"]
    since = ctx["since_iso"]
    # Smokin' hot: the Telex List's 50%+ deals and the mic deals, best score first.
    from scrapers.reposts import collapse_reposts
    hot, seen_urls = [], set()
    for l in collapse_reposts(sorted(ctx["deals"] + ctx["hits"], key=lambda l: -(l.get("score") or 0))):
        if l.get("url") not in seen_urls:
            seen_urls.add(l.get("url"))
            hot.append(l)
    # Outboard targets (the Distressor; others can be added to mic_focus.TARGETS with outboard=True).
    outboard = []
    for t in st["targets"]:
        if not t.get("outboard"):
            continue
        ls = [l for l in ctx["all_hits"] if l["mic"]["target"] == t["key"] and l.get("url") not in seen_urls
              and _outboard_fresh_near(l)]
        outboard.append({"key": t["key"], "name": t["name"].replace("Empirical Labs ", ""), "note": t.get("note"),
                         "on": t["on"], "rows": ls})
    mic_keys = [t["key"] for t in st["targets"] if t["key"] not in outboard_keys]
    mic_targets = [t for t in st["targets"] if t["key"] in mic_keys]
    mic_count = sum(len(by.get(k) or []) for k in mic_keys)
    # New since you last looked: all gear (Telex List and mics, outboard too).
    new = list(ctx["new"])
    if since:
        have = {l.get("url") for l in new}
        try:
            for l in _feed_items(cfg):
                age = l["age_days"] if l.get("age_days") is not None else 999
                if (l.get("first_seen") or "") > since and age <= FEED_RECENT_DAYS and l.get("url") not in have \
                        and not l.get("sold") and not l.get("hidden"):
                    have.add(l.get("url"))
                    new.append(l)
        except Exception:
            logging.exception("Home: Telex List new results failed")
        for k in mic_keys + list(outboard_keys):
            for l in by.get(k) or []:
                if (l.get("first_seen") or "") > since and l.get("url") not in have:
                    have.add(l.get("url"))
                    new.append(l)
        new.sort(key=listed, reverse=True)
        new = collapse_reposts(new)
    # Vague-title ads the AI named from the photo and that are 50%+ under used.
    mislabeled = []
    try:
        from scrapers import mic_focus, mislabeled as ML
        from scrapers.enrich import parse_price
        mislabeled = _decorate(mic_focus.still_showing(_cached_find("mislabeled", ML.find, cfg)))
        for l in mislabeled:
            l["comp"] = {"pct": round((1 - (parse_price(l.get("price")) or 0) / l["used_value"]) * 100),
                         "ref": f"${l['used_value']:,}", "est": False, "label": f"AI guess: {l['guess']}"}
    except Exception:
        logging.exception("Home: mislabeled finder failed")
    quiet = []
    try:
        from scrapers import mic_focus, quiet_auctions
        quiet = _decorate(mic_focus.still_showing(_cached_find("quiet", quiet_auctions.find, cfg)))
        for l in quiet:
            q = l["quiet"]
            l["comp"] = {"pct": q["pct"], "ref": f"${q['used']:,}", "est": False, "label": "Used market"}
        quiet.sort(key=lambda l: l["quiet"]["ends"])
    except Exception:
        logging.exception("Home: quiet auctions failed")
    motivated = []
    try:
        from scrapers import mic_focus, motivated as MOT
        motivated = _decorate(mic_focus.still_showing(_cached_find("motivated", MOT.find, cfg)))
        for l in motivated:
            m = l["motivated_ad"]
            l["comp"] = {"pct": m["pct"], "ref": f"${m['used']:,}", "est": False, "label": "Used market"}
        motivated = collapse_reposts(sorted(motivated, key=lambda l: -l["motivated_ad"]["pct"]))
    except Exception:
        logging.exception("Home: motivated sellers failed")
    ctx.update(new=new, hot=hot, outboard=outboard, mic_targets=mic_targets, mic_count=mic_count, mislabeled=mislabeled,
               quiet=quiet, motivated=motivated)
    return render_template("home.html", **ctx)


def _proposals_view() -> dict:
    from scrapers import proposals
    items = proposals.load()
    return {"open": [p for p in items if p["status"] == "proposed"],
            "approved": [p for p in items if p["status"] == "approved"],
            "built": sorted((p for p in items if p["status"] == "built"), key=lambda p: p.get("built_at") or "", reverse=True)[:5]}


@app.route("/proposals/<pid>", methods=["POST"])
def proposal_decide(pid):
    """Build / Skip on a proposal (the nightly update builds approved ones)."""
    from scrapers import proposals
    action = request.form.get("action")
    if action in ("approved", "skipped"):
        proposals.set_status(pid, action)
    return redirect(url_for("mics_page") + "#proposals")


@app.route("/builder")
def builder_status_page():
    """Live status of the background builder (written to /data/builder_status.json by scripts/status_push.sh on the host)."""
    import html as _h
    try:
        s = json.load(open("/data/builder_status.json"))
        age = int(time.time() - os.path.getmtime("/data/builder_status.json"))
    except Exception:
        return "<p style='font-family:sans-serif'>Builder status is not available yet.</p>"
    mark = {"done": "&#10003;", "running": "&#9654;", "waiting": "&middot;"}
    pct = int(100 * s["done"] / max(s["total"], 1))
    rows = "".join(f'<li class="{p["state"]}"><b>{mark[p["state"]]}</b> {_h.escape(p["label"])}</li>' for p in s["plan"])
    commits = "".join(f"<li>{_h.escape(c[:110])}</li>" for c in s["commits"][:8])
    lim = ", ".join(f"{a} until {t}" for a, t in s["limited"].items())
    stale = f" &middot; <b>host not reporting for {age // 60} min</b>" if age > 180 else ""
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=15>
<title>Gear Scout builder</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--dim:#777;--bar:#2b7a4b;--track:#e6e6e6}}@media(prefers-color-scheme:dark){{:root{{--bg:#161616;--fg:#eee;--dim:#999;--track:#333;--bar:#4cc38a}}}}
body{{background:var(--bg);color:var(--fg);font:15px -apple-system,system-ui,sans-serif;margin:0;padding:16px;max-width:640px}}
.track{{background:var(--track);border-radius:8px;height:14px}}.fill{{background:var(--bar);height:14px;border-radius:8px;width:{pct}%}}
ul{{list-style:none;padding:0}}li{{padding:5px 0}}.waiting,.done{{color:var(--dim)}}.running{{font-weight:600}}h3{{margin:20px 0 4px}}small{{color:var(--dim)}}</style>
<h2>Gear Scout builder</h2><div class=track><div class=fill></div></div>
<p><b>{s['done']}/{s['total']}</b> batches done &middot; App {'healthy' if s['healthy'] else '<b>DOWN</b>'} &middot; {s['counts'].get('proposed',0)} ideas waiting &middot; {s['counts'].get('built',0)} built{(' &middot; limited: '+lim) if lim else ''}{stale}</p>
<ul>{rows}</ul><h3>Latest commits</h3><ul>{commits}</ul><small>Updated {s['at']}, refreshes itself</small>"""


@app.route("/mics/built")
def mics_built():
    """When the Mics list was last rebuilt — the open page polls this and
    reloads itself after each sweep instead of going stale."""
    from scrapers import mic_focus
    return {"built": mic_focus.load().get("built")}


@app.route("/shopping")
def shopping():
    """Studio shopping list: cheapest live listings per item, running total."""
    from scrapers import shopping_list
    return render_template("shopping.html", s=shopping_list.summary())


def _pickup_plan(deals: Optional[dict]) -> dict:
    """Weekend pickup route from the (already built) deals data."""
    from scrapers import pickup_route
    from scrapers.geo import miles_between, place_coords, zip_coords
    home_zip = str(load_config_raw().get("home_zip") or "")
    home = zip_coords(home_zip) if home_zip else None
    listings = list((deals or {}).get("trusted", [])) + list((deals or {}).get("rough", []))
    plan = pickup_route.build(listings, home, place_coords, miles_between)
    plan["home_set"] = bool(home)
    return plan


@app.route("/pickup-route")
def pickup_route_page():
    """Weekend pickup route: live local deals grouped by area with a stop order."""
    return render_template("pickup_route.html", p=_pickup_plan(_all_deals()))


@app.route("/shopping/state", methods=["POST"])
def shopping_state():
    """Saves a 'bought' count for one item, or the budget."""
    from scrapers import shopping_list
    d = request.get_json(silent=True) or {}
    st = shopping_list.load_state()
    if "budget" in d:
        try:
            st["budget"] = max(0, float(re.sub(r"[^\d.]", "", str(d["budget"])))) if str(d["budget"]).strip() else None
        except ValueError:
            return {"ok": False, "error": "Type the budget as a number, like 1500."}, 400
    if "key" in d:
        items = {i["key"]: i for i in shopping_list.build()["items"]}
        if d["key"] not in items:
            return {"ok": False, "error": "Unknown item."}, 400
        try:
            n = int(d.get("bought", 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "Type how many as a whole number."}, 400
        st["bought"][d["key"]] = min(max(n, 0), items[d["key"]]["qty"])
    shopping_list.save_state(st)
    return {"ok": True}


@app.route("/mics/settings", methods=["POST"])
def mics_settings():
    data = load_config_for_edit()
    mf = data.get("mic_focus") or {}
    if "enabled" in request.form:
        mf["enabled"] = request.form.get("enabled") == "1"
    targets = dict(mf.get("targets") or {})
    from scrapers.mic_focus import TARGETS
    for t in TARGETS:
        k = t["key"]
        if f"price_{k}" not in request.form:
            if f"on_{k}" in request.form or "min_price" in request.form:  # a 50%-rule brand: just its on/off tick
                targets[k] = dict(targets.get(k) or {}, on=request.form.get(f"on_{k}") == "1")
            continue
        try:
            price = max(0, int(float(re.sub(r"[^\d.]", "", request.form.get(f"price_{k}") or "") or 0)))
        except ValueError:
            continue
        targets[k] = dict(targets.get(k) or {}, max_price=price, on=request.form.get(f"on_{k}") == "1")
    mf["targets"] = targets
    if "min_price" in request.form:
        try:
            mf["min_price"] = max(0, int(float(re.sub(r"[^\d.]", "", request.form.get("min_price") or "") or 0)))
        except ValueError:
            pass
    data["mic_focus"] = mf
    save_config_raw(data)
    _data_changed()  # Deals / Steals follow Mic Focus (on/off, limits): rebuild them
    return redirect(url_for("mics_page", saved=1))


@app.route("/")
def feed():
    from scrapers import wanted
    cfg = load_config_raw()
    if not request.args:
        if (cfg.get("mic_focus") or {}).get("enabled"):
            return mics_page()  # Mic Focus: the mics are the front page (the inbox is at /?inbox=1)
        return _home_page()  # new results + smokin' hot deals; the old inbox is at /?inbox=1
    terms = wanted.terms(cfg)
    items = _feed_items(cfg)
    if not (request.args.get("term") or request.args.get("show") or request.args.get("sort") or request.args.get("all")):
        # The front page: an inbox of what needs your attention. Every
        # listing for a term lives on the Telex List (and ?all=1).
        since = _feed_visit()
        from scrapers.fb_twins import hide_twins
        sections = _inbox(hide_twins(items), since)
        sweep = None
        try:
            info = json.loads(Path("/data/last_sweep.json").read_text())
            sweep = {"ago": _ago(info["finished"]), "new": info.get("new", 0)}
        except Exception:
            pass
        _verify_feed([l["url"] for s in sections for l, _ in s["rows"]][:80])
        from scrapers import analytics
        stats = analytics.load(cfg)
        return render_template("feed.html", sections=sections, total=len(items), terms=terms,
                               since_label=_ago(since), first_visit=since is None, sweep=sweep,
                               waiting=sum(len(s["rows"]) for s in sections),
                               stats=stats, stats_ago=_ago(stats["built"]) if stats else None)
    since = _feed_visit()
    show = request.args.get("show") or "all"
    term = request.args.get("term") or ""
    sort = "cheap" if request.args.get("sort") == "cheap" else "new"
    try:
        within = int(request.args.get("within") or 0)
    except ValueError:
        within = 0
    counts = {t: 0 for t in terms}
    for l in items:
        for t in l["wanted"]:
            if t in counts:
                counts[t] += 1
    view = items
    _mark_lowest(items)
    pinned = []
    if term:
        parts = {p.strip() for p in term.split(",") if p.strip()}
        view = [l for l in view if parts & set(l["wanted"])]
        # Filtering by a term: the cheapest working one of each model on top.
        pinned, _ = _pinned_first(view, parts)
    if show == "deals":
        view = [l for l in view if l.get("deal_pct")]
    elif show == "steals":
        view = [l for l in view if l.get("is_steal")]
    if within:
        view = [l for l in view if l.get("distance") is None or l["distance"] <= within]
    # The pinned cheapest still have to pass the Deals / Steals / distance filters.
    in_view = {id(l) for l in view}
    pinned = [l for l in pinned if id(l) in in_view]
    for l in view:
        l["is_new"] = bool(since and (l.get("first_seen") or "") > since)
    # "New" = new to you AND recently posted; an old ad Gear Scout only just
    # dug up goes with the older ads (still tagged NEW there).
    age = lambda l: l["age_days"] if l.get("age_days") is not None else 999
    new = [l for l in view if l["is_new"] and age(l) <= FEED_RECENT_DAYS]
    recent = [l for l in view if not l["is_new"] and age(l) <= FEED_RECENT_DAYS]
    older = [l for l in view if age(l) > FEED_RECENT_DAYS]
    if pinned:
        new = [l for l in new if l not in pinned]
        older = [l for l in older if l not in pinned]
        recent = pinned + [l for l in recent if l not in pinned]
    if show in ("deals", "steals") and sort != "cheap":
        # Deals / steals: all of them in view, whatever their age (a steal
        # posted three weeks ago is still a steal) — newest first.
        new, recent, older = new, recent + older, []
    if sort == "cheap":
        # One list, every ad still for sale whatever its age, cheapest first
        # (each model's cheapest on top when a term is picked).
        rest = sorted((l for l in view if l not in pinned), key=lambda l: l.get("total") or l.get("unit") or 1e12)
        new, recent, older = [], pinned + rest, []
    sweep = None
    try:
        info = json.loads(Path("/data/last_sweep.json").read_text())
        sweep = {"ago": _ago(info["finished"]), "new": info.get("new", 0)}
    except Exception:
        pass
    _verify_feed([l["url"] for l in (new + recent)[:80]])
    return render_template("feed_all.html", new=new, recent=recent, older=older, total=len(items),
                           terms=terms, counts=counts, term=term, show=show, since_label=_ago(since),
                           sweep=sweep, recent_days=FEED_RECENT_DAYS, steal_pct=FEED_STEAL_PCT, sort=sort,
                           first_visit=since is None)


_feed_verify = {"running": False}


def _verify_feed(urls: list[str]):
    """Sold check for the top of the Feed right away (websites hourly;
    Facebook ads are left to the scraper's sold check, Feed first)."""
    if _feed_verify["running"] or not urls:
        return

    def run():
        from concurrent.futures import ThreadPoolExecutor
        from scrapers.sold_check import _record, check_url
        from scrapers.store import _conn
        _feed_verify["running"] = True
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            web = [u for u in urls if "facebook.com" not in u]
            with _conn() as conn:
                recent = {u for (u,) in conn.execute(
                    f"SELECT url FROM seen WHERE url IN ({','.join('?' * len(web))}) AND sold_checked_at >= ?",
                    (*web, cutoff))} if web else set()
            todo = [u for u in web if u not in recent]

            def one(u):
                try:
                    gone = check_url(u)
                except Exception:
                    gone = None
                _record(u, gone)
                return u, gone
            with ThreadPoolExecutor(max_workers=4) as pool:
                gone = [u for u, g in pool.map(one, todo) if g]
            if gone:
                _data_changed()
                logging.info("Feed: %d sold/ended listings taken off", len(gone))
        finally:
            _feed_verify["running"] = False
    threading.Thread(target=run, name="verify-feed", daemon=True).start()


@app.route("/health")
def health():
    """Is everything being searched? Per site: last search, how many it
    found and how many were new; per Wanted term: when each site group last
    searched it; and how fresh the Feed is."""
    from scrapers import wanted
    from scrapers.store import _conn
    from scrapers.telex import EBAY_EVERY_MIN, FAST_EVERY_MIN, _cable_part_number, sweep_state
    cfg = load_config_raw()
    try:
        run = json.loads(Path("/data/last_run.json").read_text())
    except Exception:
        run = {}
    with _conn() as conn:
        last_found = dict(conn.execute("SELECT source_name, MAX(first_seen) FROM seen GROUP BY source_name").fetchall())
        new_24h = dict(conn.execute(
            "SELECT source_name, COUNT(*) FROM seen WHERE first_seen >= ? GROUP BY source_name",
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),)).fetchall())
    sites = []
    for s in run.get("sources") or []:
        name = s.get("name")
        sites.append({"name": name, "ok": s.get("success"), "blocked": s.get("blocked"), "count": s.get("count"),
                      "error": s.get("error"), "last_new": _ago(last_found.get(name)),
                      "new_24h": new_24h.get(name, 0)})
    # Facebook Marketplace regions and Craigslist cities are many small sources: one row each group.
    st = sweep_state()
    term_rows = []
    for t in wanted.terms(cfg):
        row = {"term": t}
        for site in ("fast", "ebay", "fb"):
            s = st.get((t, site)) or {}
            row[site] = _ago(s.get("searched_at"))
            row[site + "_new"] = s.get("new") or 0
        row["fb_slow"] = _cable_part_number(t)  # searched on Facebook every 3 days (see telex.run_wanted_sweep)
        term_rows.append(row)
    items = _feed_items(cfg)
    # Freshness: for Feed listings found in the last day with a known posting
    # time, how long after the seller posted Gear Scout found them.
    lags = []
    for l in items:
        if l.get("age_known") and l.get("posted_at") and l.get("first_seen"):
            try:
                p = datetime.fromisoformat(l["posted_at"])
                f = datetime.fromisoformat(l["first_seen"])
                p = p if p.tzinfo else p.replace(tzinfo=timezone.utc)
                f = f if f.tzinfo else f.replace(tzinfo=timezone.utc)
                # Ads posted in the last 3 days and found in the last day (an
                # old ad found late says nothing about how fast new ones arrive).
                now = datetime.now(timezone.utc)
                if now - f < timedelta(days=1) and now - p < timedelta(days=3):
                    lags.append(max(0.0, (f - p).total_seconds() / 3600))
            except ValueError:
                pass
    lags.sort()
    lag = {"n": len(lags), "median": round(lags[len(lags) // 2], 1) if lags else None,
           "within_1h": sum(x <= 1 for x in lags), "within_6h": sum(x <= 6 for x in lags)}
    # The top of the Feed (what you actually scroll through) — the long tail
    # of older ads is re-checked more slowly.
    top = items[:200]
    six_h = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    checks = {"total": len(top), "fresh": sum(1 for l in top if (l.get("confirmed_live_at") or "") >= six_h)}
    sweep = None
    try:
        info = json.loads(Path("/data/last_sweep.json").read_text())
        sweep = {"ago": _ago(info["finished"]), "new": info.get("new", 0)}
    except Exception:
        pass
    try:
        refresh = json.loads(Path("/data/last_refresh.json").read_text())
        refresh = {"ago": _ago(refresh["finished"]), "new": refresh.get("new_count", 0)}
    except Exception:
        refresh = None
    email = None
    try:
        es = json.loads(Path("/data/email_status.json").read_text())
        email = {"ok_ago": _ago(es["last_ok"]) if es.get("last_ok") else None,
                 "failing_ago": _ago(es["failing_since"]) if es.get("failing_since") else None,
                 "why": es.get("last_fail_why") or ""}
    except Exception:
        pass
    return render_template("health.html", sites=sites, term_rows=term_rows, lag=lag, checks=checks,
                           sweep=sweep, refresh=refresh, email=email,
                           fast_every=FAST_EVERY_MIN, ebay_every=EBAY_EVERY_MIN)


@app.route("/dashboard")
def index():
    cfg = load_config_raw()
    data = _index_listings(cfg)
    # "New since your last visit": everything (all sites, newest first) found
    # after your previous visit; a visit within 30 minutes counts as the same.
    since = _visit_marker()
    every = [l for _, items in data["grouped"] for l in items]
    for l in every:
        l["is_new"] = bool(since and (l.get("first_seen") or "") > since)
    new_items = sorted((l for l in every if l["is_new"]), key=lambda l: l.get("first_seen") or "", reverse=True)
    fbm_regions = [{"name": name, "location_id": location_id} for name, location_id in FACEBOOK_MARKETPLACE_REGIONS]
    last_refresh = None
    try:
        info = json.loads(Path("/data/last_refresh.json").read_text())
        mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(info["finished"])).total_seconds() // 60)
        last_refresh = {"mins": mins, "new_count": info.get("new_count", 0)}
    except Exception:
        pass
    return render_template(
        "index.html",
        last_refresh=last_refresh,
        no_comp=data["no_comp"][:150], no_comp_total=len(data["no_comp"]),
        old_items=data.get("old", []),
        usually_hidden=data.get("usually_hidden", [])[:150],
        usually_hidden_total=len(data.get("usually_hidden", [])),
        shop_folds=data.get("shop_folds", []),
        usually_hidden_brands=sorted({l.get("hidden_brand") for l in data.get("usually_hidden", []) if l.get("hidden_brand")}),
        new_items=new_items[:48], new_total=len(new_items), new_since=since,
        auctions=data.get("auctions", []),
        scraped_at=_last_scraped_by_site(),
        for_you=data["for_you"],
        for_you_ceiling=_for_you_cache.get("ceiling"),
        deals=data["deals"],
        status=load_run_status(),
        grouped_listings=data["grouped"],
        db_stats=db_stats(),
        next_run=next_run_time(cfg),
        fb_session=fb_session_status(),
        source_count=len(cfg.get("sources", [])),
        fbm_regions=fbm_regions,
        watches=_watch_rows(cfg),
    )


_index_cache: dict = {"key": None, "at": 0.0, "data": None}
_index_state = {"running": False}
VISIT_FILE = Path("/data/last_visit.json")


def _visit_marker() -> Optional[str]:
    """The time of your previous Dashboard visit (UTC ISO). A new visit
    starts when you come back after 30+ minutes away; refreshing or
    clicking around in between keeps the same "new since" point."""
    now = datetime.now(timezone.utc)
    try:
        v = json.loads(VISIT_FILE.read_text())
    except Exception:
        v = {}
    if request.headers.get("X-GearScout-Test"):  # maintenance checks aren't visits
        return v.get("previous")
    current = v.get("current")
    if not current or now - datetime.fromisoformat(current) > timedelta(minutes=30):
        v["previous"] = current
    v["current"] = now.isoformat()
    try:
        VISIT_FILE.write_text(json.dumps(v))
    except OSError:
        pass
    return v.get("previous")


_mismatch_cache: dict = {"key": None, "at": 0.0, "n": 0}


def _mismatched_count(keywords) -> int:
    """Settings → Data statistic (checks every stored listing against every
    term — ~1.5s); reused for 10 minutes."""
    key = len(keywords)
    if _mismatch_cache["key"] != key or time.time() - _mismatch_cache["at"] > 600:
        _mismatch_cache.update(key=key, at=time.time(), n=count_mismatched(keywords))
    return _mismatch_cache["n"]


def _index_listings(cfg) -> dict:
    """The Dashboard's listings (the slow part: comps for ~3,000 listings),
    reused for 3 minutes unless something changed (hide, favorite, settings,
    a finished scrape). The cache warmer keeps the default view ready."""
    key = (request.query_string, _last_change_marker())
    if _index_cache["key"] == key and time.time() - _index_cache["at"] < PAGE_REFRESH:
        return _index_cache["data"]
    # Right after a restart: show the last Dashboard saved on disk at once
    # and rebuild in the background (the cache warmer does it).
    if _index_cache["data"] is None and not request.query_string:
        snap = _snapshot_load("index")
        if snap is not None:
            _index_cache.update(key=None, at=0.0, data=snap)
            return snap
    # Something changed: show the current Dashboard right away and refresh
    # it in the background, instead of making you wait 30-40s.
    if _index_cache["data"] is not None and not request.query_string and not request.headers.get("X-Warm"):
        if not _index_state["running"]:
            def rebuild():
                _index_state["running"] = True
                try:
                    with app.test_request_context("/dashboard", headers={"X-Warm": "1"}):
                        _index_listings(load_config_raw())
                except Exception:
                    logging.exception("Dashboard rebuild failed")
                finally:
                    _index_state["running"] = False
            threading.Thread(target=rebuild, name="index-rebuild", daemon=True).start()
        return _index_cache["data"]
    data = _build_index_listings(cfg)
    _index_cache.update(key=key, at=time.time(), data=data)
    if not request.query_string:
        _snapshot_save("index", data)
    _verify_top_listings()
    return data


def _last_change_marker() -> str:
    """Changes when a scrape finishes or settings are saved."""
    marker = ""
    # A manual scrape counts when it finishes — not on every progress update
    # (that made every page rebuild over and over while a scrape ran).
    for p in (Path("/data/last_run.json"), Path("/data/last_refresh.json"), Path("/data/manual_scrape_finished")):
        try:
            marker += str(int(p.stat().st_mtime))
        except OSError:
            pass
    return marker


def _build_index_listings(cfg) -> dict:
    listings = recent_listings(limit=3000)
    # Same relevance check the scrapes now use, so listings stored before it
    # (found only through a broad word like "mic") don't crowd the page.
    if cfg.get("relevance_check", True):
        from scrapers.enrich import is_relevant
        from scrapers.learning import keywords_with_learned
        terms = tuple(keywords_with_learned(cfg))
        listings = [l for l in listings if l.get("favorite") or is_relevant(l.get("title"), l.get("price"), terms)]
    # Only what's worth your time: 10%+ under its comp. The rest of what has
    # no comp yet sits in one collapsed group at the bottom.
    worth, no_comp = _apply_comp_rule(_decorate(listings))
    # Ads the seller posted 30+ days ago (Gear Scout often finds those deep in
    # Reverb/eBay) go to their own section at the bottom; favorites stay put.
    for l in worth:
        l["age_days"] = _age_days(l)
    old = sorted((l for l in worth if not l.get("favorite") and (l.get("age_days") or 0) > FRESH_DAYS),
                 key=lambda l: -(l.get("comp") or {}).get("pct", 0))
    old_ids = {id(l) for l in old}
    worth = [l for l in worth if id(l) not in old_ids]
    # Brands you keep hiding drop to a collapsed line (unless 60%+ under comp).
    usually_hidden, kept = [], []
    try:
        from scrapers.hidden_brands import is_usually_hidden, usually_hidden_brands
        _hb = usually_hidden_brands()
        for l in worth:
            b = is_usually_hidden(l, _hb)
            if b:
                l["hidden_brand"] = b
                usually_hidden.append(l)
            else:
                kept.append(l)
        worth = kept
    except Exception:
        logging.exception("Hidden-brand memory failed")
    # Reverb shops' new / B-stock Lauten and Warm Audio copies: one line per model.
    shop_folds = []
    try:
        from scrapers.shop_fold import fold_shop_stock
        worth, shop_folds = fold_shop_stock(worth)
    except Exception:
        logging.exception("Shop-stock fold failed")
    grouped =_group_by_source(worth[:300], decorated=True)
    from zoneinfo import ZoneInfo
    now_s = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M")  # ShopGoodwill times are Pacific
    auctions = sorted((a for a in getattr(_apply_comp_rule, "auctions", []) if (a.get("auction_ends") or "9") >= now_s),
                      key=lambda a: a.get("auction_ends") or "9")
    for_you = _for_you(grouped)
    # Tell the hourly sold/pending checker what's on screen, so it re-checks
    # these first (what you see shouldn't linger after it sells).
    try:
        shown = [l["url"] for l in for_you] + [l["url"] for _, items in grouped for l in items]
        Path("/data/shown_dashboard.txt").write_text("\n".join(dict.fromkeys(u for u in shown if u)))
    except OSError:
        pass
    picked = {l.get("url") for l in for_you}
    return {"grouped": grouped, "no_comp": no_comp, "for_you": for_you,
            "deals": [l for l in _deals_from(grouped) if l.get("url") not in picked],  # each ad once
            "auctions": auctions, "old": old, "usually_hidden": usually_hidden,
            "shop_folds": shop_folds}


@app.route("/scrape/start", methods=["POST"])
def scrape_start():
    global _manual_scrape_thread
    if _manual_scrape_thread is None or not _manual_scrape_thread.is_alive():
        _write_manual_scrape_status({"state": "running", "done": 0, "total": 0, "current_source": None})
        _manual_scrape_thread = threading.Thread(target=_run_manual_scrape_job, daemon=True)
        _manual_scrape_thread.start()
    nxt = request.form.get("next", "")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))


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
        deals=_deals_from(grouped_listings),
        q=q,
        grouped_listings=grouped_listings,
        result_count=len(listings),
        ebay_api_configured=bool(ebay_api.get("client_id") and ebay_api.get("client_secret")),
        is_watched=_is_watched(q),
        watch_saved=request.args.get("watch_saved"),
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


@app.route("/jobs/active")
def jobs_active():
    """Manual scrapes/searches running in the background (they keep going
    when you switch pages) — shown in a small bar on every page."""
    jobs = [
        ("scrape", MANUAL_SCRAPE_STATUS_PATH, lambda: _manual_scrape_thread, "Scraping every source", "index"),
        ("telex", TELEX_STATUS_PATH, lambda: _telex_thread, "Telex search", "telex"),
        ("live", LIVE_SEARCH_STATUS_PATH, lambda: _live_search_thread, "Live search", "search"),
        ("lowest", LOWEST_STATUS_PATH, lambda: _lowest_thread, "Lowest-price search", "lowest"),
        ("fbposts", FBPOSTS_STATUS_PATH, lambda: _fbposts_thread, "Facebook post search", "fb_posts_page"),
    ]
    out = []
    for kind, path, thread, label, page in jobs:
        try:
            st = json.loads(path.read_text())
        except Exception:
            continue
        t = thread()
        if st.get("state") != "running" or t is None or not t.is_alive():
            continue
        q = st.get("query") or ""
        out.append({"kind": kind, "label": label + (f" “{q}”" if q else ""), "done": st.get("done", 0),
                    "total": st.get("total", 0), "started": st.get("started"),
                    "current_source": st.get("current_source"),
                    "url": url_for(page, q=q) if kind in ("live", "lowest") and q else url_for(page)})
    return jsonify(jobs=out)


@app.route("/favorites")
def favorites():
    listings = favorite_listings(limit=300)
    grouped_listings = _group_by_source(listings)
    return render_template(
        "favorites.html",
        deals=_deals_from(grouped_listings),
        grouped_listings=grouped_listings,
        result_count=len(listings),
    )


@app.route("/listing/favorite", methods=["POST"])
def listing_favorite():
    global_id = request.form.get("global_id", "")
    favorite = request.form.get("favorite") == "1"
    if global_id:
        set_favorite(global_id, favorite)
        _patch_cached(global_id, favorite=favorite)
    return redirect(request.form.get("next") or url_for("index"))


def _watch_rows(cfg: dict) -> list[dict]:
    from scrapers.watches import get_watches, watch_status
    status = watch_status()
    rows = []
    for w in get_watches(cfg):
        st = status.get(w["query"], {})
        rows.append({**w, **st})
    return rows


def _is_watched(q: str) -> bool:
    from scrapers.watches import get_watches, normalize_query
    return bool(q) and any(w["query"] == normalize_query(q) for w in get_watches(load_config_raw()))


@app.route("/watches/add", methods=["POST"])
def watches_add():
    from scrapers.watches import normalize_query, seed_from_stored
    q = normalize_query(request.form.get("q", ""))
    if not q:
        return redirect(url_for("search"))
    max_price = request.form.get("max_price", "").replace("$", "").replace(",", "").strip()
    try:
        max_price = float(max_price) if max_price else None
    except ValueError:
        max_price = None
    cfg = load_config_for_edit()
    watches = cfg.setdefault("watches", [])
    for w in watches:
        if normalize_query(w.get("query", "")) == q:
            w["max_price"] = max_price
            break
    else:
        watches.append({"query": q, "max_price": max_price, "enabled": True})
    save_config_raw(cfg)
    # Everything already found for this search counts as seen — only listings
    # that show up from now on will alert.
    seed_from_stored(q, max_price)
    return redirect(url_for("search", q=q, watch_saved=1))


@app.route("/watches/delete", methods=["POST"])
def watches_delete():
    from scrapers.watches import normalize_query
    q = normalize_query(request.form.get("q", ""))
    cfg = load_config_for_edit()
    cfg["watches"] = [w for w in (cfg.get("watches") or []) if normalize_query(w.get("query", "")) != q]
    save_config_raw(cfg)
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/settings/notifications", methods=["POST"])
def settings_notifications():
    cfg = load_config_for_edit()
    ncfg = cfg.setdefault("notifications", {})
    ncfg["ntfy_topic"] = re.sub(r"[^A-Za-z0-9_-]", "", request.form.get("ntfy_topic", ""))[:64]
    ncfg["ntfy_server"] = request.form.get("ntfy_server", "").strip() or "https://ntfy.sh"
    ncfg["watch_email"] = request.form.get("watch_email") == "1"
    try:
        ncfg["watch_interval_minutes"] = max(15, int(request.form.get("watch_interval_minutes", "60")))
    except ValueError:
        pass
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="notifications"))


@app.route("/settings/notifications/test", methods=["POST"])
def settings_notifications_test():
    from scrapers.notify import send_push
    ok = send_push(load_config_raw(), "Gear Scout test", "Notifications are working 🎛",
                   tags=["white_check_mark"])
    return redirect(url_for("settings", saved="push_ok" if ok else "push_fail"))


LOWEST_STATUS_PATH = Path("/data/lowest_status.json")
_lowest_thread = None


def _write_lowest_status(status: dict):
    LOWEST_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOWEST_STATUS_PATH.write_text(json.dumps(status))


def _load_lowest_status() -> dict:
    try:
        return json.loads(LOWEST_STATUS_PATH.read_text())
    except Exception:
        return {"state": "idle"}


def _run_lowest_job(query: str):
    from scrapers.lowest import check_lowest

    started = time.time()

    def on_progress(done, total, current):
        _write_lowest_status({"state": "running", "query": query, "done": done,
                              "total": total, "current_source": current, "started": started})
    try:
        res = check_lowest(query, load_config_raw(), on_progress=on_progress)
        _write_lowest_status({"state": "done", "query": query, "found": len(res["top"])})
    except Exception as e:
        logging.exception("Lowest-price lookup failed")
        _write_lowest_status({"state": "failed", "query": query, "reason": str(e)})


def _start_lowest_job(query: str) -> bool:
    global _lowest_thread
    if _lowest_thread is not None and _lowest_thread.is_alive():
        return False
    _write_lowest_status({"state": "running", "query": query, "done": 0, "total": 0})
    _lowest_thread = threading.Thread(target=_run_lowest_job, args=(query,), daemon=True)
    _lowest_thread.start()
    return True


# ---------------------------------------------------------------------------
# Saved: what you paid for your gear vs. new, with social slides
# ---------------------------------------------------------------------------
_savings_cache: dict = {"key": None, "at": 0.0, "data": None}


def _savings_data(refresh: bool = False):
    from scrapers import savings
    url = (load_config_raw().get("savings_sheet_url") or "").strip()
    if not url:
        return url, None
    c = _savings_cache
    if not refresh and c["key"] == url and c["data"] is not None and time.time() - c["at"] < 600:
        return url, c["data"]
    d = savings.build(url, refresh=refresh)
    c.update(key=url, at=time.time(), data=d)
    return url, d


@app.route("/saved")
def savings_page():
    from scrapers import savings_slides
    url, d = _savings_data()
    slides = savings_slides.slides(d) if d and d.get("count") else []
    return render_template("savings.html", sheet_url=url, d=d, slides=slides,
                           version=int(_savings_cache.get("at") or 0))


@app.route("/saved/sheet", methods=["POST"])
def savings_sheet():
    cfg = load_config_for_edit()
    cfg["savings_sheet_url"] = request.form.get("sheet_url", "").strip()
    save_config_raw(cfg)
    _savings_cache.update(key=None, data=None)
    return redirect(url_for("savings_page"))


@app.route("/saved/refresh", methods=["POST"])
def savings_refresh():
    _savings_data(refresh=True)
    return redirect(url_for("savings_page"))


@app.route("/saved/slide/<int:n>.svg")
def savings_slide(n: int):
    from flask import Response
    from scrapers import savings_slides
    _, d = _savings_data()
    slides = savings_slides.slides(d) if d and d.get("count") else []
    if not 1 <= n <= len(slides):
        return ("No such slide", 404)
    return Response(slides[n - 1], mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.route("/lowest")
def lowest():
    from scrapers.ebay_api import ebay_manual_lowest_url
    from scrapers.lowest import get_state, normalize, tracked_queries
    cfg = load_config_raw()
    q = normalize(request.args.get("q", ""))
    tracked = tracked_queries(cfg)
    from scrapers import board as price_board
    b = price_board.load()
    rows = b.get("rows") or []
    with_deal = sorted((r for r in rows if r.get("best")), key=lambda r: -r["best"]["pct"])
    cheapest_only = sorted((r for r in rows if not r.get("best") and r.get("cheapest")),
                           key=lambda r: -((r.get("comp") or {}).get("pct") or -999))
    empty = [r for r in rows if not r.get("best") and not r.get("cheapest")]
    return render_template(
        "lowest.html", board_rows=with_deal + cheapest_only, board_empty=empty, board_built=b.get("built"),
        q=q,
        current=get_state(q) if q else None,
        is_tracked=q in tracked,
        tracked=[(t, get_state(t)) for t in tracked],
        status=_load_lowest_status(),
        ebay_included=bool((cfg.get("ebay_api") or {}).get("client_id")
                           and (cfg.get("ebay_api") or {}).get("client_secret")),
        ebay_url=ebay_manual_lowest_url(q) if q else "",
    )


@app.route("/lowest/run", methods=["POST"])
def lowest_run():
    from scrapers.lowest import normalize
    q = normalize(request.form.get("q", ""))
    if q:
        _start_lowest_job(q)
    return redirect(url_for("lowest", q=q) if q else url_for("lowest"))


@app.route("/lowest/status")
def lowest_status():
    st = _load_lowest_status()
    if st.get("state") == "running" and (_lowest_thread is None or not _lowest_thread.is_alive()):
        st = {"state": "failed", "query": st.get("query"),
              "reason": "That search was interrupted (Gear Scout restarted) — run it again."}
        _write_lowest_status(st)
    return jsonify(st)


@app.route("/lowest/track", methods=["POST"])
def lowest_track():
    from scrapers.lowest import get_state, normalize, tracked_queries
    q = normalize(request.form.get("q", ""))
    if not q:
        return redirect(url_for("lowest"))
    cfg = load_config_for_edit()
    if q not in tracked_queries(cfg):
        cfg.setdefault("price_trackers", []).append({"query": q, "enabled": True})
        save_config_raw(cfg)
    # First check records today's 10 lowest; alerts start from the next one.
    if not get_state(q):
        _start_lowest_job(q)
    return redirect(url_for("lowest", q=q))


@app.route("/lowest/untrack", methods=["POST"])
def lowest_untrack():
    from scrapers.lowest import forget, normalize
    q = normalize(request.form.get("q", ""))
    cfg = load_config_for_edit()
    cfg["price_trackers"] = [
        t for t in (cfg.get("price_trackers") or [])
        if normalize(t.get("query", "") if isinstance(t, dict) else str(t)) != q
    ]
    save_config_raw(cfg)
    forget(q)
    return redirect(url_for("lowest"))


FBPOSTS_STATUS_PATH = Path("/data/fbposts_status.json")
_fbposts_thread = None


def _fbposts_rows(days: int = 14) -> list[dict]:
    """Facebook post-search finds first seen in the last `days` days."""
    import sqlite3
    from datetime import timedelta
    from scrapers.store import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM seen WHERE source_name LIKE 'FB Posts%' AND hidden = 0 AND duplicate = 0
               AND posted_at >= ? ORDER BY posted_at DESC LIMIT 400""", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def _run_fbposts_job(term: str):
    from scrapers.fb_posts import scrape_facebook_posts
    from scrapers.store import mark_seen
    try:
        started = time.time()
        FBPOSTS_STATUS_PATH.write_text(json.dumps({"state": "running", "query": term, "started": started}))

        def on_progress(done, total, step):
            FBPOSTS_STATUS_PATH.write_text(json.dumps({"state": "running", "query": term, "started": started,
                                                       "done": done, "total": total, "current_source": step}))
        res = scrape_facebook_posts({"name": f"FB Posts — Manual: {term}"}, [term], load_config_raw(),
                                    on_progress=on_progress)
        for l in res.listings:
            l.source_name = f"FB Posts — Manual: {term}"
            mark_seen(l)
        FBPOSTS_STATUS_PATH.write_text(json.dumps(
            {"state": "done" if res.success else "failed", "query": term,
             "found": len(res.listings), "reason": res.error}))
    except Exception as e:
        logging.exception("FB post search failed")
        FBPOSTS_STATUS_PATH.write_text(json.dumps({"state": "failed", "query": term, "reason": str(e)}))


@app.route("/fb-posts")
def fb_posts_page():
    from scrapers.fb_posts import priority_terms
    cfg = load_config_raw()
    rows = _fbposts_rows()
    # Same rule as everywhere: posts 10%+ under their comp; the rest of the
    # posts with no comp (most posts have no clear price) are collapsed.
    worth, no_comp = _apply_comp_rule(_decorate(rows))
    groups: dict[str, list[dict]] = {}
    for r in worth:
        groups.setdefault(r["source_name"].replace("FB Posts — ", ""), []).append(r)
    try:
        status = json.loads(FBPOSTS_STATUS_PATH.read_text())
    except Exception:
        status = {"state": "idle"}
    return render_template(
        "fb_posts.html",
        groups=sorted(groups.items()),
        no_comp=no_comp,
        terms=priority_terms(cfg),
        enabled=any(s.get("type") == "facebook_posts" and s.get("enabled", True) for s in cfg.get("sources", [])),
        status=status,
        q=request.args.get("q", ""),
        bg=_fb_bg_view(cfg),
        saved=request.args.get("saved"),
    )


def _fb_bg_view(cfg) -> dict:
    from scrapers import fb_posts_background as fbbg
    st, s = fbbg.state(), fbbg.settings(cfg)
    total = st.get("total_terms") or len(fbbg.all_terms(cfg))
    cursor = st.get("cursor") or 0
    active_hours = max(1, s["end_hour"] - s["start_hour"])
    per_day = s["per_hour"] * active_hours
    paused = st.get("paused_until")
    if paused and datetime.fromisoformat(paused) <= datetime.now(timezone.utc):
        paused = None
    return {
        **s, "total": total, "cursor": cursor,
        "pct": round(100 * cursor / total) if total else 0,
        "today_count": st.get("today_count") or 0, "today_hits": st.get("today_hits") or 0,
        "last_term": st.get("last_term"), "passes_done": st.get("passes_done") or 0,
        "per_day": per_day, "days_per_pass": round(total / per_day, 1) if per_day else None,
        "paused_until": paused, "pause_reason": st.get("pause_reason") if paused else None,
    }


@app.route("/settings/fb-posts-background", methods=["POST"])
def settings_fb_posts_background():
    def num(name, lo, hi, default):
        try:
            return max(lo, min(hi, int(request.form.get(name, default))))
        except ValueError:
            return default
    cfg = load_config_for_edit()
    fps = cfg.setdefault("fb_post_search", {})
    start, end = num("start_hour", 0, 23, 6), num("end_hour", 1, 24, 22)
    fps["background"] = {
        "enabled": request.form.get("enabled") == "1",
        "per_hour": num("per_hour", 8, 200, 60),
        "start_hour": start, "end_hour": max(end, start + 1),
        "roundup_hour": num("roundup_hour", 0, 23, 15),
    }
    save_config_raw(cfg)  # picked up by the next 15-minute batch — no restart needed
    return redirect(url_for("fb_posts_page", saved="bg"))


@app.route("/fb-posts/search", methods=["POST"])
def fb_posts_search():
    global _fbposts_thread
    term = " ".join(request.form.get("q", "").split())
    if term and (_fbposts_thread is None or not _fbposts_thread.is_alive()):
        _fbposts_thread = threading.Thread(target=_run_fbposts_job, args=(term,), daemon=True)
        _fbposts_thread.start()
    return redirect(url_for("fb_posts_page", q=term))


@app.route("/fb-posts/status")
def fb_posts_status():
    try:
        st = json.loads(FBPOSTS_STATUS_PATH.read_text())
    except Exception:
        return jsonify({"state": "idle"})
    if st.get("state") == "running" and (_fbposts_thread is None or not _fbposts_thread.is_alive()):
        st = {"state": "failed", "query": st.get("query"),
              "reason": "That search was interrupted (Gear Scout restarted) — run it again."}
        FBPOSTS_STATUS_PATH.write_text(json.dumps(st))
    return jsonify(st)


@app.route("/settings/fb-posts", methods=["POST"])
def settings_fb_posts():
    from scrapers.fb_posts import parse_priority_list
    cfg = load_config_for_edit()
    cats = parse_priority_list(request.form.get("priority", ""))
    cfg.setdefault("fb_post_search", {})["categories"] = cats
    # Make sure the source exists when there's something to search.
    if cats and not any(s.get("type") == "facebook_posts" for s in cfg.get("sources", [])):
        cfg["sources"].append({"name": "FB Posts", "url": "https://www.facebook.com/search/posts/",
                               "type": "facebook_posts", "enabled": True})
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="fbposts"))


@app.route("/listing/hide", methods=["POST"])
def listing_hide():
    _patch_cached(request.form.get("global_id", ""), hidden=request.form.get("hidden", "1") == "1")
    # Hiding something Gear Scout called a deal / worth it = its comp was
    # probably wrong; Gear Scout re-checks comps you dispute.
    try:
        gid = request.form.get("global_id", "")
        from scrapers.store import _conn
        with _conn() as conn:
            row = conn.execute("SELECT title, price, description FROM seen WHERE global_id = ?", (gid,)).fetchone()
        if row and request.form.get("hidden", "1") == "1":
            from scrapers.enrich import model_key, price_context, value_key
            from scrapers.learning import record_comp_hide
            from scrapers.market import load_market
            ctx = price_context(row[0], row[1], _price_index(), load_market(), description=row[2])
            if ctx.get("deal") or ctx.get("worth"):
                key = model_key(row[0]) if ctx.get("source") in ("local", "sold") else value_key(row[0])
                record_comp_hide(key)
    except Exception:
        logging.exception("Couldn't record disputed comp")
    global_id = request.form.get("global_id", "")
    if global_id:
        set_hidden(global_id, request.form.get("hidden", "1") == "1")
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/settings/accessories", methods=["POST"])
def settings_accessories():
    cfg = load_config_for_edit()
    cfg["hide_accessories"] = request.form.get("hide_accessories") == "1"
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="exclude"))


@app.route("/settings/exclude", methods=["POST"])
def settings_exclude():
    cfg = load_config_for_edit()
    words = [w.strip() for w in request.form.get("exclude_words", "").splitlines() if w.strip()]
    cfg["exclude_words"] = words
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="exclude"))


@app.route("/settings/location", methods=["POST"])
def settings_location():
    cfg = load_config_for_edit()
    cfg["home_zip"] = re.sub(r"\D", "", request.form.get("home_zip", ""))[:5]
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="location"))


@app.route("/settings/unhide", methods=["POST"])
def settings_unhide():
    n = unhide_all()
    return redirect(url_for("settings", saved=f"unhide:{n}"))


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
    from scrapers import catch_speed
    speed = [dict(r, median_text=catch_speed.fmt(r["median"]), p90_text=catch_speed.fmt(r["p90"]))
             for r in catch_speed.scoreboard()]  # read-only, cached 10 min
    return render_template(
        "sources.html",
        catch_speed=speed,
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
        hide_accessories=load_config_raw().get("hide_accessories", True),
        ai_key_set=bool(((load_config_raw().get("ai") or {}).get("gemini_api_key") or "").strip()),
        email_cfg=email_cfg,
        schedule_cfg=schedule_cfg,
        keywords_text="\n".join(keywords),
        keyword_count=len(keywords),
        db_stats=db_stats(),
        mismatched_count=_mismatched_count(keywords),
        notif_cfg=cfg.get("notifications") or {},
        home_zip=cfg.get("home_zip") or "",
        suggested_topic="gearscout-" + secrets.token_hex(8),
        fb_priority_text="\n".join(
            f"[{c}]\n" + "\n".join(str(t) for t in (terms or []))
            for c, terms in (((cfg.get("fb_post_search") or {}).get("categories")) or {}).items()
        ),
        exclude_text="\n".join(str(w) for w in (cfg.get("exclude_words") or [])),
        hidden_count=count_hidden(),
        ebay_client_id=(cfg.get("ebay_api") or {}).get("client_id", ""),
        ebay_has_secret=bool((cfg.get("ebay_api") or {}).get("client_secret")),
        ebay_runame_set=bool(str((cfg.get("ebay_api") or {}).get("runame") or "").strip()),
        saved=request.args.get("saved"),
    )


@app.route("/settings/ebay", methods=["POST"])
def settings_ebay():
    cfg = load_config_for_edit()
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
    cfg = load_config_for_edit()
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
    # Google shows app passwords as "abcd efgh ijkl mnop": the spaces go.
    new_password = re.sub(r"\s+", "", request.form.get("password", ""))
    pw_warning = False
    if new_password:
        email_cfg["password"] = new_password
        gmail = "gmail" in (email_cfg.get("smtp_host") or "smtp.gmail.com").lower()
        pw_warning = gmail and not re.fullmatch(r"[a-zA-Z]{16}", new_password)
    # Gear Scout's own sign-in password (separate from the email one).
    new_login = request.form.get("login_password", "")
    login_short = False
    if new_login:
        if len(new_login) >= 8:
            from werkzeug.security import generate_password_hash
            cfg["login"] = {**(cfg.get("login") or {}), "password_hash": generate_password_hash(new_login)}
        else:
            login_short = True

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
    return redirect(url_for("settings", saved="email_pw_warn" if pw_warning else "login_short" if login_short else "email"))


@app.route("/settings/keywords", methods=["POST"])
def settings_keywords():
    cfg = load_config_for_edit()
    raw = request.form.get("keywords", "")
    new_keywords = [line.strip() for line in raw.splitlines() if line.strip()]
    cfg["keywords"] = new_keywords
    save_config_raw(cfg)
    return redirect(url_for("settings", saved="keywords"))


@app.route("/sources/add", methods=["POST"])
def add_source():
    cfg = load_config_for_edit()

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
    cfg = load_config_for_edit()
    for s in cfg.get("sources", []):
        if s.get("name") == name:
            s["enabled"] = not s.get("enabled", True)
            break
    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/delete", methods=["POST"])
def delete_source():
    name = request.form.get("name", "")
    cfg = load_config_for_edit()
    cfg["sources"] = [s for s in cfg.get("sources", []) if s.get("name") != name]
    save_config_raw(cfg)
    return redirect(url_for("sources"))


@app.route("/sources/edit", methods=["POST"])
def edit_source():
    cfg = load_config_for_edit()
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


def _keep_caches_warm():
    """Pre-computes the slow parts of page loads (price history, taste,
    sale speeds, market values) at start-up and every 4 minutes, so pages
    open fast even right after a restart or a big scrape."""
    while True:
        try:
            # Only what's out of date gets redone (it used to force every
            # rebuild every 3 minutes — a constant load on the old Mac).
            _price_index()
            from scrapers.learning import sale_stats, taste
            taste()
            sale_stats()
            from scrapers.market import load_market
            load_market()
            from scrapers import market_value as _mvw
            _mvw.index(max_age=1200)  # refreshed here, before page loads find it stale
            try:
                from scrapers import market_pulse as _MP
                _MP.pulse(load_config_raw())
            except Exception:
                logging.exception('Cache warm-up: market pulse failed')
            from scrapers import mislabeled as _ML, motivated as _MOT, quiet_auctions as _QA
            _cfg = load_config_raw()
            for _n, _f in (("mislabeled", _ML.find), ("quiet", _QA.find), ("motivated", _MOT.find)):
                try:
                    _cached_find(_n, _f, _cfg, ttl=240)
                except Exception:
                    logging.exception("Cache warm-up: %s failed", _n)
            _feed_items(load_config_raw())  # the Feed (home page) first
            # The Dashboard's default view, ready before anyone asks.
            with app.test_request_context("/dashboard", headers={"X-Warm": "1"}):
                if _index_cache["key"] is None and _index_cache["data"] is not None:
                    # Showing the saved copy from before the restart: rebuild for real now.
                    data = _build_index_listings(load_config_raw())
                    _index_cache.update(key=(b"", _last_change_marker()), at=time.time(), data=data)
                    _snapshot_save("index", data)
                else:
                    _index_listings(load_config_raw())
            _mismatched_count(load_config_raw().get("keywords") or [])
            with app.test_request_context("/telex", headers={"X-Warm": "1"}):
                telex()  # fills the Telex cache
            with app.test_request_context("/deals"):
                _all_deals(background=True)
            _verify_top_listings()
        except Exception:
            logging.exception("Cache warm-up failed")
        time.sleep(300)


def _singleflight_builders():
    """Page builders keep a 30-minute cache but not a lock, so a cold cache
    (every restart) let every open page rebuild at once and stall the site.
    Wrap them so concurrent callers queue behind one build, then hit its cache."""
    import functools
    import importlib
    targets = (("best_month", "best_months"), ("price_history", "history"), ("regional_gap", "regional_gaps"),
               ("market_pulse", "pulse"), ("flips", "find"), ("price_targets", "status"), ("price_targets", "hits"),
               ("wait_signal", "signals"), ("seller_watch", "watched"), ("offer_log", "summary"))
    for mod_name, fn_name in targets:
        try:
            mod = importlib.import_module(f"scrapers.{mod_name}")
            fn = getattr(mod, fn_name, None)
            if fn is None or getattr(fn, "_singleflight", False):
                continue
            lock = threading.RLock()

            def make(fn=fn, lock=lock):
                @functools.wraps(fn)
                def wrapper(*a, **k):
                    with lock:
                        return fn(*a, **k)
                wrapper._singleflight = True
                return wrapper
            setattr(mod, fn_name, make())
        except Exception:
            logging.exception("single-flight wrap failed for %s.%s", mod_name, fn_name)


_singleflight_builders()


if __name__ == "__main__":
    threading.Thread(target=_keep_caches_warm, name="cache-warm", daemon=True).start()
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

    # Every other background search: a "running" left over from before the
    # restart can't still be running.
    for path in (TELEX_STATUS_PATH, LOWEST_STATUS_PATH, FBPOSTS_STATUS_PATH, MANUAL_SCRAPE_STATUS_PATH):
        try:
            st = json.loads(path.read_text())
            if st.get("state") == "running":
                path.write_text(json.dumps({"state": "failed", "query": st.get("query"),
                                            "reason": "Gear Scout restarted before this finished — run it again."}))
        except Exception:
            pass

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
