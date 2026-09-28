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
    """{value_key: typical price}, plus .sources {value_key: 'Reverb' | 'eBay' | 'Reverb + eBay'}."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sources: dict[str, str] = {}


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


def _usable(title: str, query: str, value: Optional[float]) -> bool:
    from scrapers.base import keyword_match
    return bool(value and value >= 20 and not is_partial(title) and not is_lot(title)
                and keyword_match(title, [query]))


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


def market_typical(query: str, ebay_cfg: Optional[dict] = None,
                   loosen: bool = False) -> tuple[Optional[float], int, Optional[str], str]:
    """(median, samples, source label, query used) pooling Reverb and eBay.
    With `loosen`, drops trailing words (down to 2) until enough match."""
    words = query.split()
    while True:
        q = " ".join(words)
        reverb = _reverb_prices(q)
        ebay = []
        if ebay_cfg and ebay_cfg.get("client_id") and ebay_cfg.get("client_secret"):
            try:
                ebay = _ebay_prices(q, ebay_cfg)
            except Exception as e:
                logger.warning("eBay price lookup failed for %r: %s", q, e)
        prices = reverb + ebay
        if len(prices) >= MIN_SAMPLES:
            label = " + ".join(n for n, p in (("Reverb", reverb), ("eBay", ebay)) if p)
            return statistics.median(prices), len(prices), label, q
        if not loosen or len(words) <= 2:
            return None, len(prices), None, q
        words = words[:-1]
        time.sleep(0.5)


def load_market() -> MarketIndex:
    """{value_key: typical asking price} for fresh, usable cache entries."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT model_key, typical, source FROM market_prices"
            " WHERE typical IS NOT NULL AND fetched_at >= ?",
            (cutoff,),
        ).fetchall()
    market = MarketIndex()
    for key, typical, source in rows:
        market[key] = typical
        market.sources[key] = source or "Reverb"
    return market


def refresh_market_prices(titles: list[str], local_index: dict[str, float],
                          max_lookups: int = 60, pause: float = 1.0,
                          cfg: Optional[dict] = None) -> int:
    """Looks up every item in `titles` that has no local typical price and
    no fresh cached lookup — model-number items first, then title-word ones.
    Capped per call and paced, so a run never floods Reverb or eBay.
    Returns how many items got a usable price."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    ebay_cfg = (cfg or {}).get("ebay_api") or {}
    with _conn() as conn:
        _ensure_table(conn)
        fresh = {k for (k,) in conn.execute(
            "SELECT model_key FROM market_prices WHERE fetched_at >= ?", (cutoff,))}
    models: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for title in titles:
        if is_partial(title):
            continue
        mkey = model_key(title)
        if mkey and mkey in local_index:
            continue
        key = value_key(title)
        if not key or key in fresh or key in models or key in by_title:
            continue
        if key.startswith("t:"):
            by_title[key] = key[2:]
        else:
            models[key] = value_query(title)
    todo = list(models.items()) + list(by_title.items())
    todo = todo[:max_lookups]

    found = 0
    for key, query in todo:
        try:
            typical, n, label, used = market_typical(query, ebay_cfg, loosen=key.startswith("t:"))
        except Exception as e:
            logger.warning("Market price lookup failed for %r: %s", query, e)
            continue
        with _conn() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO market_prices (model_key, query, typical, samples, fetched_at, source)"
                " VALUES (?,?,?,?,?,?)",
                (key, used, typical, n, datetime.now(timezone.utc).isoformat(), label),
            )
            conn.commit()
        if typical:
            found += 1
        time.sleep(pause)
    if todo:
        logger.info("Market prices: looked up %d items, %d usable", len(todo), found)
    return found
