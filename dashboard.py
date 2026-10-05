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
                "shopgoodwill", "kijiji", "offerup", "shopify", "long_mcquade", "facebook_posts", "reddit"]


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
                _config_cache["data"] = yaml_rt.load(f)
            _config_cache["stamp"] = stamp
        return _config_cache["data"]


def load_config_for_edit():
    """A fresh, private parse of config.yaml, safe to modify and save."""
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
    from scrapers.manual_scrape import run_manual_scrape
    started = time.time()

    def on_progress(done, total, current_source):
        _write_manual_scrape_status({
            "state": "running", "started": started,
            "done": done,
            "total": total,
            "current_source": current_source,
        })

    try:
        cfg = load_config_raw()
        results, new_listings = run_manual_scrape(cfg, on_progress=on_progress)
        Path("/data/manual_scrape_finished").touch()  # pages rebuild once, now
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


@app.route("/sw.js")
def service_worker():
    # Served from the site root (not /static) so its scope covers every page.
    js = "self.addEventListener('fetch', () => {});"
    return app.response_class(js, mimetype="application/javascript",
                              headers={"Cache-Control": "no-cache"})


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
    out = []
    for l in listings:
        if (exclude_match(l.get("title"), exclude_words) or is_not_audio(l.get("title")) or title_says_sold(l.get("title"))
                or (hide_acc and is_accessory_only(l.get("title")))
                or (hide_pedals and item_form(l.get("title")) == "pedal")):
            continue
        # Learned from listings that sold: how fast this model goes, and at what price.
        sale = sales.get(model_key(l.get("title")) or "")
        l["sale"] = ({"fast": sale["days"] <= FAST_SALE_DAYS, "days": round(sale["days"], 1),
                      "price": f"${sale['price']:,.0f}", "n": sale["n"]} if sale else None)
        # Days on market: a listing that has sat for weeks usually has a
        # seller open to an offer.
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
        l["age_days"] = _age_days(l)
        l["age_known"] = bool(l.get("posted_at"))
        l["needs_repair"] = needs_repair(l.get("title"), l.get("description"))
        l["price_ctx"] = price_context(l.get("title"), l.get("price"), index, market,
                                       description=l.get("description"))
        # A current auction bid isn't a sale price, so it's never a "deal".
        l["is_auction"] = (l.get("description") or "").startswith("Auction")
        if (l.get("sold") or l.get("pending") or l["is_auction"]) and l["price_ctx"]:
            l["price_ctx"]["deal"] = False
            l["price_ctx"]["worth"] = False  # a current bid isn't a price
        l["distance"] = distance_miles(home_zip, l.get("location")) if home_zip else None
        # Listings with no known distance (shipped-item sites, older rows)
        # stay visible — the radius only filters what can be measured.
        if within and l["distance"] is not None and l["distance"] > within:
            continue
        l["is_deal"] = bool(l["price_ctx"] and l["price_ctx"].get("deal"))
        if deals_only and not l["is_deal"]:
            continue
        # "Worth it only": 10%+ under B-stock value (or a solid used value for
        # vintage gear); listings without a comp yet are left out.
        if worth_only and not (l["price_ctx"] or {}).get("worth"):
            continue
        out.append(l)
    return out


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
                   AND first_seen >= datetime('now', '-7 days') ORDER BY first_seen DESC LIMIT 4000""")]
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
    ("Vintage King", "Vintage King", "store"), ("Long & McQuade", "L&M", "store"), ("Alto Music", "Alto", "store"),
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
        d = _index_cache.get("data")
        if d:
            yield d["no_comp"]
            yield d.get("old", [])
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
    """Listings 60%+ (or the chosen level) under used prices."""
    d = _all_deals(background)
    return {name: [l for l in d[name] if l["comp"]["pct"] >= min_pct] for name in ("trusted", "rough")}


@app.route("/deals")
def deals():
    age = request.args.get("age") or "3"
    age = age if age in dict(DEAL_AGES) else "3"
    price = request.args.get("price") or "500-2000"
    price = price if price in dict(DEAL_PRICES) else "500-2000"
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
             "cheap": lambda l: l["steal"].get("unit") or 0}
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
        min_pct = int(request.args.get("pct") or 60)
    except ValueError:
        min_pct = 60
    if _deals_cache["data"] is not None:
        _all_deals()
    return {"rebuilding": _deals_state["running"]}


@app.route("/steals")
def steals():
    try:
        min_pct = int(request.args.get("pct") or 60)
    except ValueError:
        min_pct = 60
    min_pct = min_pct if min_pct in STEAL_LEVELS else 60
    data = _steals(min_pct)
    _verify_top_listings()
    # Newest posts first; ads up 30+ days get their own section at the bottom.
    age = lambda l: l.get("age_days") if l.get("age_days") is not None else 999
    fresh = sorted((l for l in data["trusted"] if age(l) <= FRESH_DAYS), key=_fresh_order)
    old = sorted((l for l in data["trusted"] if age(l) > FRESH_DAYS), key=lambda l: -l["comp"]["pct"])
    rough = sorted(data["rough"], key=age)
    return render_template("steals.html", trusted=fresh, old=old, rough=rough,
                           min_pct=min_pct, levels=STEAL_LEVELS)


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


@app.route("/telex")
def telex():
    cfg = load_config_raw()
    terms = _telex_terms(cfg)
    # Matching thousands of listings against every term takes a few seconds
    # (much longer while a scrape runs); reuse it for 3 minutes unless
    # something changed.
    key = (tuple(terms), _data_version[0])  # ?sort= only changes the display
    if _telex_cache["groups"] is None:
        snap = _snapshot_load("telex")
        if snap is not None:  # terms changed since? only the differences get worked out below
            _telex_cache.update(key=None, at=0.0, groups=snap)
    fresh = _telex_cache["key"] == key and time.time() - _telex_cache["at"] < PAGE_REFRESH
    cached = _telex_cache["groups"]
    if (not fresh and cached is not None and not request.headers.get("X-Warm")
            and [t for t, _, _ in cached] != terms):
        # You added or removed a term: work out only the new one(s) — a few
        # seconds — instead of all ~45 terms (a minute), then refresh the
        # rest in the background.
        have = {t: g for g in cached for t in [g[0]]}
        missing = [t for t in terms if t not in have]
        if len(missing) <= 5:
            if missing:
                have.update({g[0]: g for g in _telex_matches(missing, note=False)})
            cached = [have[t] for t in terms]
            _telex_cache.update(key=None, at=0.0, groups=cached)
    if fresh:
        groups = _telex_cache["groups"]
    elif (_telex_cache["groups"] is not None
          and [t for t, _, _ in _telex_cache["groups"]] == terms and not request.headers.get("X-Warm")):
        # Show the last list right away and refresh it in the background.
        groups = _telex_cache["groups"]
        if not _telex_state["running"]:
            def rebuild():
                _telex_state["running"] = True
                try:
                    with app.test_request_context("/telex", headers={"X-Warm": "1"}):
                        telex()
                except Exception:
                    logging.exception("Telex rebuild failed")
                finally:
                    _telex_state["running"] = False
            threading.Thread(target=rebuild, name="telex-rebuild", daemon=True).start()
    else:
        groups = _telex_matches(terms)
        _telex_cache.update(key=key, at=time.time(), groups=groups)
        _snapshot_save("telex", groups)
        _verify_top_listings()
    # What changed since you last looked: newest posts first inside each
    # term (or cheapest first, one tap), terms with something new on top,
    # and everything posted in the last 2 days collected at the top.
    sort = "cheap" if request.args.get("sort") == "cheap" else "new"
    age = lambda r: r.get("age_days") if r.get("age_days") is not None else 999
    view, new_all = [], {}
    from scrapers.board import _is_specific
    from scrapers.enrich import parse_price
    unit = lambda r: (r.get("price_ctx") or {}).get("unit_value") or parse_price(r.get("price")) or 0
    for term, items, unpriced in groups:
        # Broad terms ("vintage microphone", "tube mic", "stam") match lots of
        # cheap odds and ends: nothing under $150 there, and only $300+ (near
        # your budget) in the "New" section. Specific models: any price.
        specific = _is_specific(term)
        if not specific:
            items = [r for r in items if unit(r) >= 150]
        fresh = [r for r in items if not r.get("is_old")]
        old = [r for r in items if r.get("is_old")]
        if sort == "new":
            fresh = sorted(fresh, key=age)
        new = [r for r in fresh if age(r) <= 2 and (specific or unit(r) >= 300)]
        for r in new:
            new_all.setdefault(r["url"], (r, term))
        view.append({"term": term, "all_items": items, "fresh": fresh, "old": old, "unpriced": unpriced,
                     "new_count": len(new)})
    view.sort(key=lambda v: v["new_count"] == 0)  # stable: keeps your order otherwise
    new_items = sorted(new_all.values(), key=lambda x: age(x[0]))
    from scrapers.telex import state as telex_state
    from scrapers.telex import groups as telex_groups
    return render_template("telex.html", groups=groups, terms=terms, view=view, new_items=new_items, sort=sort,
                           total=sum(len(g) for _, g, _ in groups), sweep=telex_state(),
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
    of recalculating everything. The Telex page itself works out just the new
    term on its next load."""
    def run():
        try:
            from scrapers import board
            board.build(load_config_raw(), only=added or [])
        except Exception:
            logging.exception("Price Board update for new terms failed")
    threading.Thread(target=run, name="board-terms", daemon=True).start()


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


@app.route("/")
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
                    with app.test_request_context("/", headers={"X-Warm": "1"}):
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
    grouped = _group_by_source(worth[:300], decorated=True)
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
            "auctions": auctions, "old": old}


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
            # The Dashboard's default view, ready before anyone asks.
            with app.test_request_context("/", headers={"X-Warm": "1"}):
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
    for path in (TELEX_STATUS_PATH, LOWEST_STATUS_PATH, FBPOSTS_STATUS_PATH):
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
