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
import re
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
            "deals_only": request.args.get("deals") == "1"}


_price_index_cache: dict = {"at": 0.0, "index": None}


def _price_index() -> dict:
    """Typical prices from local history — rebuilt at most every 5 minutes
    (it reads every priced listing and takes a couple of seconds)."""
    from scrapers.enrich import build_price_index
    if _price_index_cache["index"] is None or time.time() - _price_index_cache["at"] > 300:
        _price_index_cache.update(index=build_price_index(all_priced_rows()), at=time.time())
    return _price_index_cache["index"]


def _decorate(listings: list[dict]) -> list[dict]:
    """Drops listings matching the user's exclude words and adds display
    flags: needs_repair, typical price / deal, and 'was' price after a drop."""
    from scrapers.enrich import (build_price_index, exclude_match, is_accessory_only, is_not_audio,
                                 needs_repair, price_context)

    from scrapers.geo import distance_miles

    cfg = load_config_raw()
    exclude_words = cfg.get("exclude_words") or []
    hide_acc = cfg.get("hide_accessories", True)
    home_zip = str(cfg.get("home_zip") or "")
    try:
        within = int(request.args.get("within") or 0)
    except ValueError:
        within = 0
    deals_only = request.args.get("deals") == "1"
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
        if (exclude_match(l.get("title"), exclude_words) or is_not_audio(l.get("title"))
                or (hide_acc and is_accessory_only(l.get("title")))):
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
        l["needs_repair"] = needs_repair(l.get("title"), l.get("description"))
        l["price_ctx"] = price_context(l.get("title"), l.get("price"), index, market,
                                       description=l.get("description"))
        # A current auction bid isn't a sale price, so it's never a "deal".
        l["is_auction"] = (l.get("description") or "").startswith("Auction")
        if (l.get("sold") or l.get("pending") or l["is_auction"]) and l["price_ctx"]:
            l["price_ctx"]["deal"] = False
        l["distance"] = distance_miles(home_zip, l.get("location")) if home_zip else None
        # Listings with no known distance (shipped-item sites, older rows)
        # stay visible — the radius only filters what can be measured.
        if within and l["distance"] is not None and l["distance"] > within:
            continue
        l["is_deal"] = bool(l["price_ctx"] and l["price_ctx"].get("deal"))
        if deals_only and not l["is_deal"]:
            continue
        out.append(l)
    return out


def _deals_from(grouped: list[tuple]) -> list[dict]:
    """Every deal across all sources, biggest discount first — shown in its
    own section above the per-source groups."""
    deals = [l for _, items in grouped for l in items if l.get("is_deal")]
    return sorted(deals, key=lambda l: l["price_ctx"].get("pct_under", 0), reverse=True)


def _group_by_source(listings: list[dict]) -> list[tuple]:
    """Group already-newest-first listings by source, preserving recency
    order within each group, and order the groups themselves by whichever
    source has the single most recent listing."""
    groups: dict[str, list[dict]] = {}
    for listing in _decorate(listings):
        # One section per site: every Craigslist town / Facebook region /
        # Vintage King list goes under its site, with the town or region
        # shown on the card instead.
        family, _, detail = (listing.get("source_name") or "").partition(" — ")
        family = {"FB Marketplace": "Facebook Marketplace", "FB": "Facebook groups"}.get(family, family)
        listing["source_detail"] = detail or None
        groups.setdefault(family, []).append(listing)
    # `listings` arrives newest-first, so the first listing seen for a given
    # source is already that source's most recent one.
    return sorted(groups.items(), key=lambda kv: kv[1][0]["first_seen"], reverse=True)


def _for_you(grouped: list[tuple], n: int = 12) -> list[dict]:
    """Listings that best match what you favorite (learned taste)."""
    try:
        from scrapers.learning import taste
        t = taste()
        if not t.ready:
            return []
        scored, seen_urls = [], set()
        for _, items in grouped:
            for l in items:
                if l.get("favorite") or l.get("sold") or l.get("url") in seen_urls:
                    continue
                seen_urls.add(l.get("url"))
                s = t.score(l.get("title") or "")
                if s >= 1.5:
                    scored.append((s, l))
        return [l for _, l in sorted(scored, key=lambda x: -x[0])[:n]]
    except Exception:
        logging.exception("Couldn't score listings for you")
        return []


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


def _telex_matches(terms: list[str], days: int = 30, per_term: int = 60) -> list[tuple[str, list[dict]]]:
    """Stored listings (last `days` days, from scrapes and live searches)
    matching each Telex term — every word, any order, like the live search."""
    import sqlite3
    from datetime import timedelta
    from scrapers.store import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND hidden = 0 AND duplicate = 0
               AND COALESCE(sold, 0) = 0 AND first_seen >= ? ORDER BY first_seen DESC LIMIT 6000""",
            (cutoff,)).fetchall()]
    from scrapers.base import _keyword_pattern, _word_matches, fix_brand_spelling
    texts = [fix_brand_spelling((r["title"] or "").lower()) for r in rows]

    def matcher(term: str):
        words = [w for w in term.lower().split() if re.search(r"[a-z0-9]", w)]
        # A long, descriptive term ("Warm audio WA-412 API 4 channel pre")
        # rarely has every word in a title: its brand + model numbers decide.
        if len(words) >= 4:
            key = [words[0]] + [w for w in words[1:] if re.search(r"\d", w) and len(w) >= 2]
            words = key if len(key) >= 2 else words
        exact = _keyword_pattern(term.lower())
        # Quick substring check on the longest plain word before the full match.
        plain = max((w for w in words if w.isalpha() and len(w) >= 3), key=len, default=None)
        return lambda t: (plain is None or plain in t) and (
            bool(exact and exact.search(t)) or all(_word_matches(w, t) for w in words))

    out = []
    for term in terms:
        hits, seen_urls = [], set()
        match = matcher(term)
        for r, text in zip(rows, texts):
            if r["url"] in seen_urls:
                continue
            if match(text):
                seen_urls.add(r["url"])
                hits.append(r)
                if len(hits) >= 400:
                    break
        out.append((term, hits))
    # Price context etc. for every hit in one pass (a listing can sit under
    # several terms), then hand each group its decorated copies.
    unique = list({id(r): r for _, hits in out for r in hits}.values())
    kept = {id(r) for r in _decorate(unique)}  # adds display fields in place; drops excluded
    from scrapers.enrich import parse_price

    def price_order(r):
        # Cheapest first by the price shown (US dollars for Canadian ads).
        # No price, or a placeholder like "$1 — make an offer", goes last.
        value = parse_price(r.get("price"))
        placeholder = value is None or value < 5
        return (placeholder, value or 0)

    return [(term, sorted((r for r in hits if id(r) in kept), key=price_order)[:per_term])
            for term, hits in out]


@app.route("/telex")
def telex():
    cfg = load_config_raw()
    terms = _telex_terms(cfg)
    groups = _telex_matches(terms)
    from scrapers.telex import state as telex_state
    return render_template("telex.html", groups=groups, terms=terms,
                           total=sum(len(g) for _, g in groups), sweep=telex_state())


@app.route("/telex/add", methods=["POST"])
def telex_add():
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
    return redirect(url_for("telex"))


@app.route("/telex/remove", methods=["POST"])
def telex_remove():
    term = request.form.get("term", "").strip()
    cfg = load_config_for_edit()
    cfg["telex_list"] = [t for t in (cfg.get("telex_list") or []) if str(t).strip() != term]
    save_config_raw(cfg)
    return redirect(url_for("telex"))


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
    status = load_run_status()
    listings = recent_listings(limit=450)
    # Same relevance check the scrapes now use, so listings stored before it
    # (found only through a broad word like "mic") don't crowd the page.
    if cfg.get("relevance_check", True):
        from scrapers.enrich import is_relevant
        from scrapers.learning import keywords_with_learned
        terms = tuple(keywords_with_learned(cfg))
        listings = [l for l in listings if l.get("favorite") or is_relevant(l.get("title"), l.get("price"), terms)][:300]
    grouped_listings = _group_by_source(listings)
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
        for_you=_for_you(grouped_listings),
        deals=_deals_from(grouped_listings),
        status=status,
        grouped_listings=grouped_listings,
        db_stats=db_stats(),
        next_run=next_run_time(cfg),
        fb_session=fb_session_status(),
        source_count=len(cfg.get("sources", [])),
        fbm_regions=fbm_regions,
        watches=_watch_rows(cfg),
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
    return render_template(
        "lowest.html",
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
    return jsonify(_load_lowest_status())


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
    groups: dict[str, list[dict]] = {}
    for r in _decorate(rows):
        groups.setdefault(r["source_name"].replace("FB Posts — ", ""), []).append(r)
    try:
        status = json.loads(FBPOSTS_STATUS_PATH.read_text())
    except Exception:
        status = {"state": "idle"}
    return render_template(
        "fb_posts.html",
        groups=sorted(groups.items()),
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
        return jsonify(json.loads(FBPOSTS_STATUS_PATH.read_text()))
    except Exception:
        return jsonify({"state": "idle"})


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
        email_cfg=email_cfg,
        schedule_cfg=schedule_cfg,
        keywords_text="\n".join(keywords),
        keyword_count=len(keywords),
        db_stats=db_stats(),
        mismatched_count=count_mismatched(keywords),
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
