"""
Typical used prices from Reverb, for models Gear Scout hasn't seen often
enough locally (fewer than 4 priced listings).

Uses Reverb's public listings API (no account needed) — so these are
current ASKING prices, which run a little higher than what gear actually
sells for; the dashboard labels them "Reverb ~$X" and the deal threshold is
stricter for them. Reverb's price guide (real sale prices) needs an account.

Lookups are cached in the listings database for CACHE_DAYS, including
"not enough matches" results, so each model is fetched at most once per
cache period.
"""
import logging
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from scrapers.enrich import is_lot, is_partial, model_key, model_query, parse_price
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


def reverb_typical(query: str) -> tuple[Optional[float], int]:
    """Median used asking price on Reverb for listings whose title actually
    matches every word of `query` (model-number aware, same rule as live
    search). Returns (median or None, number of listings used)."""
    from scrapers.base import keyword_match

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
        if is_partial(title) or is_lot(title) or not keyword_match(title, [query]):
            continue
        value = parse_price(str(price.get("amount") or ""))
        if value and value >= 20:
            prices.append(value)
    if len(prices) < MIN_SAMPLES:
        return None, len(prices)
    return statistics.median(prices), len(prices)


def load_market() -> dict[str, float]:
    """{model_key: typical asking price} for fresh, usable cache entries."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    with _conn() as conn:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT model_key, typical FROM market_prices WHERE typical IS NOT NULL AND fetched_at >= ?",
            (cutoff,),
        ).fetchall()
    return {k: v for k, v in rows}


def refresh_market_prices(titles: list[str], local_index: dict[str, float],
                          max_lookups: int = 60, pause: float = 1.0) -> int:
    """Looks up (on Reverb) every model in `titles` that has no local typical
    price and no fresh cached lookup. Capped per call and paced, so a run
    never floods Reverb. Returns how many models got a usable price."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
    todo: dict[str, str] = {}
    with _conn() as conn:
        _ensure_table(conn)
        fresh = {k for (k,) in conn.execute(
            "SELECT model_key FROM market_prices WHERE fetched_at >= ?", (cutoff,))}
    for title in titles:
        key = model_key(title)
        # Only brand-qualified models: a bare "x32" or "c38" is too
        # ambiguous to look up on its own.
        if not key or ":" not in key or key in local_index or key in fresh or key in todo:
            continue
        if is_partial(title):
            continue
        todo[key] = model_query(title)
        if len(todo) >= max_lookups:
            break

    found = 0
    for key, query in todo.items():
        try:
            typical, n = reverb_typical(query)
        except Exception as e:
            logger.warning("Reverb price lookup failed for %r: %s", query, e)
            continue
        with _conn() as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO market_prices (model_key, query, typical, samples, fetched_at)"
                " VALUES (?,?,?,?,?)",
                (key, query, typical, n, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        if typical:
            found += 1
        time.sleep(pause)
    if todo:
        logger.info("Reverb market prices: looked up %d models, %d usable", len(todo), found)
    return found
