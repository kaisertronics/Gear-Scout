"""
Typical used prices ("market value") from Reverb and, when eBay API keys are
set, eBay — for every listing Gear Scout hasn't seen often enough locally
(fewer than 4 priced listings of that model).

Items with a recognized model number are looked up by brand + model. Items
without one (e.g. "Soundcraft 12 channel analog mixer") are looked up by the
meaningful words of their title; if too few listings match all of them, the
last word is dropped and it tries again. Those title-word values are rough
gauges: shown on the listing, never used to call something a deal.

Both sites give current ASKING prices, which run a little higher than what
gear actually sells for; the deal threshold is stricter for them.

Lookups are cached in the listings database for CACHE_DAYS, including
"not enough matches" results, so each item is fetched at most once per
cache period.
"""
import logging
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from scrapers.enrich import is_lot, is_partial, model_key, parse_price, value_key, value_query
from scrapers.store import _conn

logger = logging.getLogger(__name__)

API_URL = "https://api.reverb.com/api/listings/all"
HEADERS = {
    "Accept-Version": "3.0",
    "Accept": "application/hal+json",
    "Content-Type": "application/hal+json",
    "User-Agent": "GearScout/1.0 (self-hosted gear alerts)",
}
CACHE_DAYS = 14
MIN_SAMPLES = 3
_SKIP_CONDITIONS = {"brand new", "non functioning"}


class MarketIndex(dict):
    """{value_key: typical price}, plus .sources {value_key: 'Reverb' | 'eBay' | 'Reverb + eBay'}
    and .rough (keys valued from only one or two listings)."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sources: dict[str, str] = {}
        self.rough: set[str] = set()


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_prices (
            model_key TEXT PRIMARY KEY,
            query TEXT,
            typical REAL,
            samples INTEGER,
            fetched_at TEXT
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(market_prices)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE market_prices ADD COLUMN source TEXT")
    if "loose" not in cols:
        # 1 = found only after loosening the search — shown as a rough estimate.
        conn.execute("ALTER TABLE market_prices ADD COLUMN loose INTEGER DEFAULT 0")


def _title_fits(title: str, query: str) -> bool:
    """A result counts when it has the query's model number and brand (its
    first word) and most of its other words — every word is too strict
    ("Tascam TEAC 22-2" vs Reverb's "TEAC 22-2 Reel to Reel")."""
    from scrapers.base import _word_matches, fix_brand_spelling
    text = fix_brand_spelling(title.lower())
    words = [w for w in query.lower().split() if w not in _GENERIC] or query.lower().split()
    if not words:
        return False
    models = [w for w in words if _is_model_token(w) or (w.isdigit() and len(w) >= 3)]
    must = set(models) or {words[0]}
    if not all(_word_matches(w, text) for w in must):
        return False
    rest = [w for w in words if w not in must]
    if not rest:
        return True
    hit = sum(1 for w in rest if _word_matches(w, text))
    return hit / len(rest) >= 0.6


def _usable(title: str, query: str, value: Optional[float]) -> bool:
    from scrapers.base import keyword_match
    from scrapers.enrich import is_bundle
    return bool(value and value >= 5 and not is_partial(title) and not is_lot(title) and not is_bundle(title)
                and (keyword_match(title, [query]) or _title_fits(title, query)))


def _reverb_prices(query: str) -> list[float]:
    resp = requests.get(API_URL, params={"query": query, "per_page": 50},
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    prices = []
    for item in resp.json().get("listings", []) or []:
        title = item.get("title") or ""
        price = item.get("price") or {}
        condition = ((item.get("condition") or {}).get("display_name") or "").lower()
        if price.get("currency") != "USD" or condition in _SKIP_CONDITIONS:
            continue
        value = parse_price(str(price.get("amount") or ""))
        if _usable(title, query, value):
            prices.append(value)
    return prices


def _ebay_prices(query: str, api_cfg: dict) -> list[float]:
    """Used / open-box / refurbished asking prices on eBay (Browse API)."""
    from scrapers.ebay_api import CONDITION_FILTER, SEARCH_URL, _get_token
    token = _get_token(api_cfg["client_id"].strip(), api_cfg["client_secret"].strip())
    resp = requests.get(
        SEARCH_URL,
        params={"q": query, "limit": 50, "filter": f"{CONDITION_FILTER},buyingOptions:{{FIXED_PRICE}}"},
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        timeout=20,
    )
    resp.raise_for_status()
    prices = []
    for item in resp.json().get("itemSummaries", []) or []:
        price = item.get("price") or {}
        if price.get("currency") != "USD":
            continue
        try:
            value = float(price.get("value"))
        except (TypeError, ValueError):
            continue
        if _usable(item.get("title") or "", query, value):
            prices.append(value)
    return prices


def reverb_typical(query: str) -> tuple[Optional[float], int]:
    """Median used asking price on Reverb for listings whose title matches
    every word of `query` (model-number aware, same rule as live search)."""
    prices = _reverb_prices(query)
    if len(prices) < MIN_SAMPLES:
        return None, len(prices)
    return statistics.median(prices), len(prices)


# Words that describe what kind of thing it is, not which one — dropped
# when a lookup finds too few matches ("Blue Microphones Nessie" -> "blue
# nessie"; "Takstar SGC-598 Condenser Microphone" -> "takstar sgc-598").
_GENERIC = {
    "microphone", "microphones", "mic", "mics", "audio", "interface", "usb", "usb-c", "rackmount",
    "rack", "handheld", "wireless", "wired", "condenser", "dynamic", "ribbon", "studio", "pro",
    "professional", "black", "white", "silver", "gold", "nickel", "series", "recorder", "player",
    "card", "sd", "acoustic-electric", "cutaway", "glossy", "mount", "available", "dual", "stereo",
    "powered", "active", "passive", "portable", "digital", "analog", "tube", "lavalier", "cardioid",
    "large", "small", "diaphragm", "capsule", "head", "unit", "system", "kit", "edition", "limited",
    "mixing", "console", "channel", "power", "cord", "with", "vintage", "original", "rare",
    "gear", "equipment", "music", "stuff", "various", "misc", "assorted", "bundle", "lot",
}
_MISSPELLED_BRANDS = {"sure": "shure", "nueman": "neumann", "nuemann": "neumann", "neuman": "neumann",
                      "senheiser": "sennheiser", "sennhieser": "sennheiser", "akg's": "akg"}


# Specs, not model numbers: "24-bit", "192khz", "2x2", "16x16", "1/4", "3.5".
_SPEC = re.compile(r"^(?:\d+(?:-?bit|khz|hz|v|w|mm|ft|u|ch|in|out|x\d+)|\d+/\d+|\d+\.\d+|usb-?c?\d*|mk-?i+)$")


def _is_model_token(w: str) -> bool:
    return bool(re.search(r"\d", w) and re.search(r"[a-z]", w) and not _SPEC.match(w))


def lookup_plan(query: str) -> list[str]:
    """Searches to try, most exact first. Loosening never drops so much that
    it matches unrelated gear: it keeps the brand and model number (or at
    least three descriptive words), and a model number on its own is only
    tried when it's distinctive ("urec5", "sgc-598" — not "c1la" or "24-bit")."""
    words = [_MISSPELLED_BRANDS.get(w, w) for w in query.lower().split()]
    words = [w for w in words if not _SPEC.match(w)] or words
    plan = [" ".join(words)]
    specific = [w for w in words if w not in _GENERIC]
    model = next((w for w in specific if _is_model_token(w)), None)
    if len(specific) >= 3:
        for cut in (specific[:4], specific[:3]):
            if not model or model in cut:
                plan.append(" ".join(cut))
    elif len(specific) == 2 and model:
        plan.append(" ".join(specific))
    if model and specific and specific[0] != model:
        plan.append(f"{specific[0]} {model}")  # brand + model: "shure sm11"
    if model and len(model.replace("-", "")) >= 5:
        plan.append(model)
    out = []
    for q in plan:
        if q and q not in out:
            out.append(q)
    return out


def market_typical(query: str, ebay_cfg: Optional[dict] = None,
                   loosen: bool = False) -> tuple[Optional[float], int, Optional[str], str]:
    """(median, samples, source label, query used) pooling Reverb and eBay.
    Tries the exact query first; with `loosen`, falls back through
    lookup_plan. If no search finds 3+ listings, the best one with 1–2 is
    used (the caller marks those as rough)."""
    best = (None, 0, None, query)
    for i, q in enumerate(lookup_plan(query) if loosen else [query]):
        if i:
            time.sleep(0.5)
        reverb = _reverb_prices(q)
        ebay = []
        if ebay_cfg and ebay_cfg.get("client_id") and ebay_cfg.get("client_secret"):
            try:
                ebay = _ebay_prices(q, ebay_cfg)
            except Exception as e:
                logger.warning("eBay price lookup failed for %r: %s", q, e)
        prices = reverb + ebay
        if not prices:
            continue
        label = " + ".join(n for n, p in (("Reverb", reverb), ("eBay", ebay)) if p)
        result = (statistics.median(prices), len(prices), label, q)
        if len(prices) >= MIN_SAMPLES:
            return result
        if len(prices) > best[1]:
            best = result
    return best


def load_market() -> MarketIndex:
    """{value_key: typical asking price} for fresh, usable cache entries."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT model_key, typical, source, samples, loose FROM market_prices"
            " WHERE typical IS NOT NULL AND fetched_at >= ?",
            (cutoff,),
        ).fetchall()
    market = MarketIndex()
    for key, typical, source, samples, loose in rows:
        market[key] = typical
        market.sources[key] = source or "Reverb"
        if (samples or 0) < MIN_SAMPLES or loose:
            market.rough.add(key)
    return market


def refresh_market_prices(titles: list[str], local_index: dict[str, float],
                          max_lookups: int = 60, pause: float = 1.0,
                          cfg: Optional[dict] = None, force: bool = False) -> int:
    """Looks up every item in `titles` that has no local typical price and
    no fresh cached lookup — model-number items first, then title-word ones.
    Capped per call and paced, so a run never floods Reverb or eBay.
    Returns how many items got a usable price."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    ebay_cfg = (cfg or {}).get("ebay_api") or {}
    with _conn() as conn:
        _ensure_table(conn)
        fresh = set() if force else {k for (k,) in conn.execute(
            "SELECT model_key FROM market_prices WHERE fetched_at >= ?", (cutoff,))}
    models: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for title in titles:
        from scrapers.enrich import is_bundle
        if is_bundle(title):
            continue
        mkey = model_key(title)
        if mkey and mkey in local_index and not is_partial(title):
            continue
        key = value_key(title)
        if not key or key in fresh or key in models or key in by_title:
            continue
        if key[:2] in ("t:", "p:"):
            by_title[key] = key[2:]
        else:
            models[key] = value_query(title)
    todo = list(models.items()) + list(by_title.items())
    todo = todo[:max_lookups]

    found = 0
    for key, query in todo:
        try:
            typical, n, label, used = market_typical(query, ebay_cfg, loosen=True)
            loose = used != lookup_plan(query)[0]
            if not typical and ":" in key and key[:2] not in ("t:", "p:"):
                # Brand + model found nothing — try the title's words.
                from scrapers.enrich import title_query
                tq = next((title_query(t) for t in titles if value_key(t) == key), None)
                if tq:
                    typical, n, label, used = market_typical(tq, ebay_cfg, loosen=True)
                    loose = True
        except Exception as e:
            logger.warning("Market price lookup failed for %r: %s", query, e)
            continue
        with _conn() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO market_prices"
                " (model_key, query, typical, samples, fetched_at, source, loose) VALUES (?,?,?,?,?,?,?)",
                (key, used, typical, n, datetime.now(timezone.utc).isoformat(), label, int(loose)),
            )
            conn.commit()
        if typical:
            found += 1
        time.sleep(pause)
    if todo:
        logger.info("Market prices: looked up %d items, %d usable", len(todo), found)
    return found
