"""Best month to buy, per mic model: which calendar month has the lowest asking prices.

Pure helper `best_month(items)` takes [(date, price)] and returns the cheapest
calendar month, or None unless the data can honestly support the claim:
  - at least MIN_MONTHS calendar months with MIN_N+ ads each,
  - the ads span at least MIN_SPAN_DAYS (otherwise it is one stretch of time,
    not a pattern across seasons),
  - the cheapest month is at least MIN_GAP_PCT under the average of months.
`best_months(cfg)` reads the local DB (read-only, same ad filters as
price_history) over ALL stored history and returns {model_key: result}.
Models without enough data are simply absent, so nothing is shown for them.
"""
import logging
import time
from datetime import datetime, timezone
from statistics import median
from typing import Optional

logger = logging.getLogger(__name__)

MIN_MONTHS = 3
MIN_N = 5
MIN_SPAN_DAYS = 300
MIN_GAP_PCT = 5
_NAMES = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "out": None}


def best_month(items: list, min_months: int = MIN_MONTHS, min_n: int = MIN_N,
               min_span_days: int = MIN_SPAN_DAYS, min_gap_pct: int = MIN_GAP_PCT) -> Optional[dict]:
    """items = [(datetime, price)] -> {month, name, pct, line} or None when too thin."""
    if not items:
        return None
    dates = [w for w, _ in items]
    if (max(dates) - min(dates)).days < min_span_days:
        return None
    by: dict = {}
    for when, price in items:
        by.setdefault(when.month, []).append(price)
    meds = {m: median(v) for m, v in by.items() if len(v) >= min_n}
    if len(meds) < min_months:
        return None
    avg = sum(meds.values()) / len(meds)
    if avg <= 0:
        return None
    m = min(meds, key=lambda k: meds[k])
    pct = round((avg - meds[m]) / avg * 100)
    if pct < min_gap_pct:
        return None
    return {"month": m, "name": _NAMES[m - 1], "pct": pct,
            "line": f"Cheapest month to buy: {_NAMES[m - 1]} (about {pct}% under average)"}


def build(cfg: dict) -> dict:
    from scrapers import mic_focus
    from scrapers.enrich import model_key, parse_price
    from scrapers.market_pulse import MIN_ASK, _ts
    from scrapers.store import _conn
    now = datetime.now(timezone.utc)
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    floors = {t["key"]: max(t.get("min_price") or 0, MIN_ASK) for t in targets}
    per: dict = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, price, COALESCE(NULLIF(posted_at, ''), first_seen), description FROM seen"
            " WHERE hidden = 0 AND duplicate = 0 AND price IS NOT NULL AND price NOT LIKE 'C$%'"
            " AND (country IS NULL OR country = 'US')").fetchall()
    for title, price, found, desc in rows:
        if (desc or "").startswith("Auction"):
            continue
        t = mic_focus.classify(title or "", targets)
        v, when = parse_price(price), _ts(found)
        mk = (model_key(title) or "") if t else ""
        if not mk or not v or not when or when > now or v < floors[t["key"]]:
            continue
        per.setdefault(mk, []).append((when, v))
    out = {}
    for mk, items in per.items():
        r = best_month(items)
        if r:
            out[mk] = r
    return out


def best_months(cfg: dict) -> dict:
    """build(), cached 30 minutes; never raises ({} on trouble)."""
    if _cache["out"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["out"]
    try:
        out = build(cfg)
    except Exception:
        logger.exception("Best month failed")
        return {}
    _cache["out"], _cache["at"] = out, time.time()
    return out
