"""The ONLY place Gear Flip touches Gear Scout. Everything Gear Flip needs from
the host app goes through these functions: listing rows, market values,
repair wording, filters and scam checks, sell-through speed, storing
scoured listings, phone pushes and email.

To spin Gear Flip off into its own product, re-implement this file against
the new product's database and notifier; nothing else has to change."""
import logging
import re
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
MIN_COMPS = 6         # comparable ads needed before Gear Flip trusts a resale price
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
        rows = [dict(r) for r in conn.execute(
            "SELECT global_id, source_name, title, url, price, image_url, description, posted_at, first_seen,"
            " last_seen, shipping, location, seller, offers_ok FROM seen"
            " WHERE first_seen >= ? AND url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL AND price != ''"
            " AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND url NOT LIKE '%search/posts%' AND COALESCE(description, '') NOT LIKE 'Auction%'"
            " AND COALESCE(last_seen, first_seen) >= ? ORDER BY first_seen DESC LIMIT ?",
            (new_cut, seen_cut, ROW_LIMIT))]
    try:  # Gear Scout's shared freshness rule: no old posts, relists, or ads not seen live in a day
        from scrapers import freshness
        rows = freshness.keep(rows)
    except ImportError:
        pass
    return rows


def is_local(source_name: Optional[str]) -> bool:
    from scrapers.finder_rows import is_local as _local
    return _local(source_name)


def _no(why: Optional[dict], reason: str):
    """Counts why an ad was left out (shown on the tab), and returns None."""
    if why is not None:
        why[reason] = why.get(reason, 0) + 1
    return None


def prepare(r: dict, cfg: dict, include_repairs: bool = True, now: Optional[datetime] = None,
            why: Optional[dict] = None) -> Optional[dict]:
    """The inputs the profit math needs for one listing, or None when Gear
    Scout's own checks say it isn't a single, real, priced piece of gear
    with a known used value."""
    from scrapers import deal_checks, fixer_upper, market_value as mv
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form, needs_repair,
                                 parse_price, price_basis, title_says_sold)
    title = (r.get("title") or "").strip()
    src = (r.get("source_name") or "").lower()
    if not title or "kijiji" in src or "mcquade" in src:
        return _no(why, "skipped site")
    if title_says_sold(title) or exclude_match(title, cfg.get("exclude_words") or []) or is_not_audio(title) \
            or is_accessory_only(title) or item_form(title) == "plugin":
        return _no(why, "not studio gear, sold, or an accessory")
    rep = fixer_upper.repair_cost(title, r.get("description"))
    repair = rep[0] if rep else ("general repair" if needs_repair(title, r.get("description")) else None)
    look = fixer_upper.strip_repair(title) if repair else title
    if repair and (not include_repairs or fixer_upper._SHELL.search(title)):
        return _no(why, "repair ad excluded")
    if not look or mv.skip_listing(look) or is_partial(look):
        return _no(why, "a part or a lot")
    from gearflip import verify
    from gearflip.engine import NEW_RE
    if NEW_RE.search(title) or verify.packaging_only(title) or verify.lookalike(look) or verify.knockoff(title):
        return _no(why, "new, box-only, look-alike or knockoff")  # new-in-box (not comparable), just the box, a look-alike or a knockoff
    ask = parse_price(r.get("price"))
    if not ask or ask < 40:
        return _no(why, "no usable price")
    _, qty, unit = price_basis(title, None, ask)
    if qty and int(qty) > 1:
        return _no(why, "several at one price")  # several at one price: not one flip
    value, n = mv.value(look, None if repair else r.get("description"))
    basis = "model"
    if not value:
        # No model value: fall back to ads with the same specific words, as a
        # rough estimate (lower confidence; the card says so).
        sv = similar_value(look, r.get("url"))
        if not sv:
            return _no(why, "no known used value")
        value, n = sv
        basis = "similar"
    k = mv._key(look) or ""
    sold_n = ((mv._cache.get("sold") or {}).get(k) or (None, 0))[1] if k else 0
    # Prefer the last 6 months: real sales first, then recent asking prices,
    # keeping the condition adjustment (mint/rough) Gear Scout's value made.
    # Resale = a realistic quick sale, not the typical ask (bk: "nothing will
    # sell that expensive"). Last 6 months first: real sales, else the lower
    # quarter of recent asks; then the lower quarter of the year's asks.
    # Never above Gear Scout's own value; condition (mint/rough) kept.
    rv = recent_values().get(k) if k and basis == "model" else None
    overall, _ = mv._overall(k, *mv.index().get(k, (None, 0))) if k else (None, 0)
    tier_x = (value / overall) if overall else 1.0
    cap = value
    year_asks = (((mv._cache.get("samples") or {}).get("asks") or {}).get(k) or []) if basis == "model" else []
    if rv and rv["sold_n"] >= mv.MIN_N:
        value, n, sold_n = rv["sold"] * tier_x, rv["sold_n"], rv["sold_n"]
    elif rv and rv["ask_n"] >= mv.MIN_N and rv.get("ask_low"):
        value, n, sold_n = rv["ask_low"] * tier_x, rv["ask_n"], 0
    elif quick_sale(year_asks) and sold_n < mv.MIN_N:
        value = quick_sale(year_asks) * tier_x
    value = min(value, cap)
    if n < MIN_COMPS:
        return _no(why, "fewer than 6 comparable ads")  # too few comparable ads to trust a resale price
    if verify.price_problem(float(unit or ask), float(value), repair):
        return _no(why, "priced too far under value")  # far under value: rarely the real, complete unit
    flags = deal_checks.scam_flags(r, None if repair else value, unit or ask)
    if len(flags) >= 2:
        return _no(why, "scam signs")
    now = now or datetime.now(timezone.utc)
    posted = _parse_ts(r.get("posted_at")) or _parse_ts(r.get("first_seen"))
    return {"title": title, "ask": float(unit or ask), "value": float(value), "n": int(n or 0),
            "sold_basis": sold_n >= mv.MIN_N, "ship_in": r.get("shipping"), "local": is_local(r.get("source_name")),
            "repair": repair, "age_days": (now - posted).total_seconds() / 86400 if posted else None,
            "flags": len(flags), "flag_text": flags + (["rough value (similar ads)"] if basis == "similar" else []),
            "model": k or ("similar:" + " ".join(_sim_words(look)[:3])), "basis": basis}


RECENT_DAYS = 183
RECENT_TTL = 1800
_recent = {"at": 0.0, "by_model": {}, "lock": threading.Lock()}


def recent_values() -> dict:
    """{model key: {"sold", "sold_n", "ask", "ask_n"}}: the middle of the last 6
    months' confirmed Reverb sales and of single, used, US asking prices, with
    the same exclusions as Gear Scout's yearly value. Cached 30 minutes."""
    if time.time() - _recent["at"] < RECENT_TTL and _recent["by_model"]:
        return _recent["by_model"]
    with _recent["lock"]:
        if time.time() - _recent["at"] < RECENT_TTL and _recent["by_model"]:
            return _recent["by_model"]
        import re
        from scrapers import market_value as mv
        from scrapers.enrich import is_accessory_only, is_partial, needs_repair, parse_price
        from scrapers.store import _conn
        cut = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()
        asks: dict = {}
        sold: dict = {}
        seen_ads = set()
        try:
            with _conn() as conn:
                rows = conn.execute(
                    "SELECT title, price, description FROM seen WHERE first_seen >= ? AND price IS NOT NULL"
                    " AND duplicate = 0 AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
                    " AND COALESCE(description, '') NOT LIKE 'Auction%'", (cut,)).fetchall()
                mv._ensure_sales(conn)
                sales = conn.execute("SELECT title, price FROM confirmed_sales WHERE at >= ?", (cut,)).fetchall()
            for title, price, desc in rows:
                if not title or mv._SKIP.search(title) or mv._PART_WORDS.search(title) or is_partial(title) \
                        or is_accessory_only(title) or needs_repair(title, desc):
                    continue
                v = parse_price(price)
                if not v or v < 25:
                    continue
                ad = (re.sub(r"\s+", " ", title.lower()).strip(), round(v))
                if ad in seen_ads:
                    continue
                seen_ads.add(ad)
                k = mv._key(title)
                if k:
                    asks.setdefault(k, []).append(v)
            for title, v in sales:
                if title and v and not mv._SKIP.search(title) and not is_partial(title) and not is_accessory_only(title):
                    k = mv._key(title)
                    if k:
                        sold.setdefault(k, []).append(float(v))
        except Exception:
            logger.exception("Gear Flip recent values")
        out = {}
        for k in set(asks) | set(sold):
            a, s_ = sorted(asks.get(k, [])), sorted(sold.get(k, []))
            cut_n = len(a) // 10 if len(a) >= 10 else (1 if len(a) >= 5 else 0)
            core = a[cut_n:len(a) - cut_n] if cut_n else a
            out[k] = {"ask": statistics.median(core) if core else None, "ask_n": len(a),
                      "ask_low": quick_sale(a), "sold": statistics.median(s_) if s_ else None, "sold_n": len(s_)}
        _recent.update(at=time.time(), by_model=out)
        return out


def _sim_words(title: str) -> list:
    from scrapers.comps import sim_words
    from scrapers.enrich import strip_clone_reference
    return sim_words(strip_clone_reference(title or ""))


def similar_value(title: str, url: Optional[str] = None) -> Optional[tuple]:
    """(quick-sale value, how many ads) from stored ads sharing this title's 3
    (else 2) most specific words, when there's no model value. Needs MIN_COMPS+
    ads with consistent prices (top quarter within 2.5x of the bottom)."""
    try:
        from scrapers.comps import similar_index
        sim = similar_index()
    except Exception:
        return None
    ws = [w for w in _sim_words(title) if w in sim["words"] and len(sim["words"][w]) >= 2]
    if not any(re.search(r"\d", w) and re.search(r"[a-z]", w) for w in ws):
        return None  # needs a model number among the matching words
    ws.sort(key=lambda w: len(sim["words"][w]))
    for k in (3, 2):
        if len(ws) < k:
            continue
        ids = set.intersection(*(sim["words"][w] for w in ws[:k]))
        vals = sorted(sim["prices"][i] for i in ids if sim["urls"][i] != url)
        if len(vals) >= MIN_COMPS and vals[(len(vals) * 3) // 4] <= 2.5 * vals[len(vals) // 4]:
            return quick_sale(vals), len(vals)
    return None


def quick_sale(prices: list) -> Optional[float]:
    """What a used piece realistically sells for SOON: the lower quarter of
    what the same model is listed at (most ads sit unsold at the median
    or above). None with fewer than 3 prices."""
    a = sorted(p for p in prices if p)
    if len(a) < 3:
        return None
    if len(a) < 4:
        return float(a[0])
    return float(statistics.quantiles(a, n=4, method="inclusive")[0])


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


# ---------------------------------------------------------------- picture analysis
def gemini_key(cfg: dict) -> str:
    """The owner's own free Gemini key from Gear Scout's Settings → AI ('' = none)."""
    return str(((cfg.get("ai") or {}).get("gemini_api_key") or "")).strip()


def photo_bytes(image_url: Optional[str]):
    """(mime type, bytes) of a listing photo (Gear Scout's image cache first)."""
    from scrapers.assistant import _photo
    return _photo(image_url)


def gemini_paused() -> bool:
    """True while Gear Scout's shared Gemini pause is on (busy / rate-limited)."""
    try:
        from scrapers import gemini_pause
    except ImportError:
        return False
    return bool(gemini_pause.paused())


def gemini_backoff(err: Exception) -> None:
    """Starts the shared Gemini pause when the error says Gemini is busy or rate-limited."""
    try:
        from scrapers import gemini_pause
    except ImportError:
        return
    if gemini_pause.busy(err):
        gemini_pause.pause()


def ai_json(key: str, prompt: str, photo) -> str:
    """One Gemini call with one photo, answered as JSON text."""
    from scrapers.assistant import _generate
    return _generate(key, prompt, json_mode=True, images=[photo])


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
