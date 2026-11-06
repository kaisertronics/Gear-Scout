"""The ONLY place Gear Flip touches Gear Scout. Everything Gear Flip needs from
the host app goes through these functions: listing rows, market values,
repair wording, filters and scam checks, sell-through speed, storing
scoured listings, phone pushes and email.

To spin Gear Flip off into its own product, re-implement this file against
the new product's database and notifier; nothing else has to change."""
import logging
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/config/config.yaml")
SEEN_DAYS = 10        # still on its site within this many days
NEW_DAYS = 45         # first found within this many days
ROW_LIMIT = 12000
SPEED_DAYS = 240      # sell-through history window
SPEED_MIN = 3         # sold ads of a model needed before its speed counts
SPEED_TTL = 6 * 3600

_speed = {"at": 0.0, "by_model": {}, "lock": threading.Lock()}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def dashboard_url(cfg: dict) -> str:
    return str((cfg.get("email") or {}).get("dashboard_url") or "http://localhost:8420").rstrip("/")


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- listings
def listing_rows(now: Optional[datetime] = None) -> list[dict]:
    """Live, priced, US, non-hidden, non-duplicate fixed-price ads seen lately."""
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    seen_cut = (now - timedelta(days=SEEN_DAYS)).isoformat()
    new_cut = (now - timedelta(days=NEW_DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT global_id, source_name, title, url, price, image_url, description, posted_at, first_seen,"
            " last_seen, shipping, location, seller, offers_ok FROM seen"
            " WHERE first_seen >= ? AND url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL AND price != ''"
            " AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND url NOT LIKE '%search/posts%' AND COALESCE(description, '') NOT LIKE 'Auction%'"
            " AND COALESCE(last_seen, first_seen) >= ? ORDER BY first_seen DESC LIMIT ?",
            (new_cut, seen_cut, ROW_LIMIT))]


def is_local(source_name: Optional[str]) -> bool:
    from scrapers.finder_rows import is_local as _local
    return _local(source_name)


def prepare(r: dict, cfg: dict, include_repairs: bool = True, now: Optional[datetime] = None) -> Optional[dict]:
    """The inputs the profit math needs for one listing, or None when Gear
    Scout's own checks say it isn't a single, real, priced piece of gear
    with a known used value."""
    from scrapers import deal_checks, fixer_upper, market_value as mv
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form, needs_repair,
                                 parse_price, price_basis, title_says_sold)
    title = (r.get("title") or "").strip()
    src = (r.get("source_name") or "").lower()
    if not title or "kijiji" in src or "mcquade" in src:
        return None
    if title_says_sold(title) or exclude_match(title, cfg.get("exclude_words") or []) or is_not_audio(title) \
            or is_accessory_only(title) or item_form(title) == "plugin":
        return None
    rep = fixer_upper.repair_cost(title, r.get("description"))
    repair = rep[0] if rep else ("general repair" if needs_repair(title, r.get("description")) else None)
    look = fixer_upper.strip_repair(title) if repair else title
    if repair and (not include_repairs or fixer_upper._SHELL.search(title)):
        return None
    if not look or mv.skip_listing(look) or is_partial(look):
        return None
    from gearflip.engine import NEW_RE
    if NEW_RE.search(title):  # new-in-box ads aren't comparable to used values
        return None
    ask = parse_price(r.get("price"))
    if not ask or ask < 40:
        return None
    _, qty, unit = price_basis(title, None, ask)
    if qty and int(qty) > 1:
        return None  # several at one price: not one flip
    value, n = mv.value(look, None if repair else r.get("description"))
    if not value:
        return None
    k = mv._key(look) or ""
    sold_n = ((mv._cache.get("sold") or {}).get(k) or (None, 0))[1] if k else 0
    flags = deal_checks.scam_flags(r, None if repair else value, unit or ask)
    if len(flags) >= 2:
        return None
    now = now or datetime.now(timezone.utc)
    posted = _parse_ts(r.get("posted_at")) or _parse_ts(r.get("first_seen"))
    return {"title": title, "ask": float(unit or ask), "value": float(value), "n": int(n or 0),
            "sold_basis": sold_n >= mv.MIN_N, "ship_in": r.get("shipping"), "local": is_local(r.get("source_name")),
            "repair": repair, "age_days": (now - posted).total_seconds() / 86400 if posted else None,
            "flags": len(flags), "flag_text": flags, "model": k}


def drop_stolen_photos(items: list[dict]) -> list[dict]:
    """Ads whose photo also sits on another seller's ad (a classic scam) are dropped."""
    from scrapers import deal_checks
    from scrapers.store import _conn
    try:
        with _conn() as conn:
            stolen = deal_checks.stolen_photos(conn, {deal_checks._img_key(i.get("image_url")) for i in items})
        return [i for i in items if deal_checks._img_key(i.get("image_url")) not in stolen]
    except Exception:
        logger.exception("Gear Flip photo check")
        return items


def sell_speed() -> dict:
    """{model key: median days from posted to sold} from ads Gear Scout saw
    sell (or come down), for models with SPEED_MIN+ of them. Cached."""
    if time.time() - _speed["at"] < SPEED_TTL and _speed["by_model"]:
        return _speed["by_model"]
    with _speed["lock"]:
        if time.time() - _speed["at"] < SPEED_TTL and _speed["by_model"]:
            return _speed["by_model"]
        from scrapers import market_value as mv
        from scrapers.store import _conn
        cut = (datetime.now(timezone.utc) - timedelta(days=SPEED_DAYS)).isoformat()
        by: dict = {}
        try:
            with _conn() as conn:
                rows = conn.execute("SELECT title, posted_at, first_seen, sold_at FROM seen WHERE sold = 1"
                                    " AND sold_at >= ? AND duplicate = 0", (cut,)).fetchall()
            for title, posted, first, sold in rows:
                a, b = _parse_ts(posted) or _parse_ts(first), _parse_ts(sold)
                k = mv._key(title or "") if title else None
                if not (a and b and k) or b <= a:
                    continue
                by.setdefault(k, []).append(min((b - a).total_seconds() / 86400, 120))
        except Exception:
            logger.exception("Gear Flip sell-speed read")
        _speed["by_model"] = {k: round(statistics.median(v), 1) for k, v in by.items() if len(v) >= SPEED_MIN}
        _speed["at"] = time.time()
        return _speed["by_model"]


# ---------------------------------------------------------------- scouring
def store_listings(listings: list, cfg: dict) -> int:
    """Stores scoured listings in Gear Scout's database as search-only finds
    (live_only: they feed values and Gear Flip, not Gear Scout's own feeds).
    Returns how many were new."""
    from scrapers.enrich import drop_excluded
    from scrapers.store import is_overseas, mark_seen
    new = 0
    for l in drop_excluded([l for l in listings if not is_overseas(getattr(l, "country", None))], cfg):
        try:
            if mark_seen(l, live_only=True) == "new":
                new += 1
        except Exception:
            logger.exception("Gear Flip: storing %s", getattr(l, "url", "?"))
    return new


def reverb_search(params: dict) -> list:
    """One Reverb API page of used pro-audio listings (public API, no account)."""
    from scrapers.reverb_api import API_URL, HEADERS, _to_listing, reverb_get
    q = {"product_type": "pro-audio", "sort": "published_at|desc", "per_page": 50, **params}
    resp = reverb_get(API_URL, params=q, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return [_to_listing(i, "Reverb — Gear Flip") for i in resp.json().get("listings") or []
            if (i.get("state") or {}).get("slug", "live") == "live" and i.get("title")]


def ebay_search(term: str, cfg: dict) -> list:
    """One eBay search (official API; falls back to eBay's web search when the
    day's API budget is spent). Empty without eBay keys."""
    api_cfg = cfg.get("ebay_api") or {}
    if not (api_cfg.get("client_id") and api_cfg.get("client_secret")):
        return []
    from scrapers.ebay_api import scrape_ebay_api
    res = scrape_ebay_api({"name": "eBay — Gear Flip"}, [term], api_cfg)
    return res.listings if res.success else []


def scrape_busy() -> bool:
    """True while Gear Scout is mid-search from the dashboard (step aside)."""
    import json
    for name in ("manual_scrape_status.json", "live_search_status.json", "lowest_status.json",
                 "fbposts_status.json", "telex_status.json"):
        p = Path("/data") / name
        try:
            if time.time() - p.stat().st_mtime < 900 and json.loads(p.read_text()).get("state") == "running":
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------- notify
def push(cfg: dict, topic: str, title: str, message: str, url: Optional[str] = None,
         actions: Optional[list] = None, image: Optional[str] = None) -> bool:
    from scrapers.notify import send_push
    if topic:
        cfg = {**cfg, "notifications": {**(cfg.get("notifications") or {}), "ntfy_topic": topic}}
    return send_push(cfg, title, message, url=url, tags=["moneybag"], image=image, actions=actions)


def push_ready(cfg: dict, topic: str) -> bool:
    return bool(topic or ((cfg.get("notifications") or {}).get("ntfy_topic") or "").strip())


def email(cfg: dict, html: str, subject: str) -> bool:
    ecfg = cfg.get("email") or {}
    if not (ecfg.get("from") and ecfg.get("to") and ecfg.get("password")):
        return False
    from scrapers.emailer import send_html_email
    return send_html_email(html, subject, ecfg)
