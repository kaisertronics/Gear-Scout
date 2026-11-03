"""Price-band dimmer: learns the price range of the ads you starred, so cards
priced far outside it can be shown dimmed (never hidden) on Home and Mics.

band() -> (low, high) or None.  None means "don't dim anything": fewer than
MIN_STARS starred ads with a price.  The band is the 10th-90th percentile of
starred prices, widened by a margin so only prices clearly out of range dim.
Display only; reads the DB read-only and caches the result for 30 minutes.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

MIN_STARS = 15
LOW_PCT, HIGH_PCT = 0.10, 0.90
LOW_MARGIN, HIGH_MARGIN = 0.7, 1.3
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "band": None}

TOOLTIP = "Dimmed: outside your usual price range"


def _percentile(sorted_vals: list, q: float) -> float:
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def compute(prices: list) -> Optional[tuple]:
    vals = sorted(p for p in prices if p and p > 0)
    if len(vals) < MIN_STARS:
        return None
    return (_percentile(vals, LOW_PCT) * LOW_MARGIN, _percentile(vals, HIGH_PCT) * HIGH_MARGIN)


def band() -> Optional[tuple]:
    now = time.time()
    if now - _cache["at"] < _TTL:
        return _cache["band"]
    result = None
    try:
        from scrapers.enrich import parse_price
        from scrapers.store import _conn
        conn = _conn()
        try:
            rows = conn.execute("SELECT price FROM seen WHERE favorite = 1").fetchall()
        finally:
            conn.close()
        result = compute([parse_price(p) for (p,) in rows])
    except Exception:
        logger.exception("Couldn't learn the price band")
    _cache["at"], _cache["band"] = now, result
    return result


def is_outside(price: Optional[float], b: Optional[tuple]) -> bool:
    return bool(b and price and (price < b[0] or price > b[1]))
