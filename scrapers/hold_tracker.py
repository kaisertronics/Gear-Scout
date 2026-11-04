"""
Hold-value tracker (Mics page card).

For the mic models the owner has starred, looks at the asking prices single,
used, US listings of that model have had over the last year and flags the
models whose price is flat or rising over time: the ones likely to hold their
value if bought and kept. Needs MIN_POINTS+ listings spread over MIN_SPAN_DAYS+.
Trend = median of the newer half of the listings vs the older half.
Read-only, no network; nothing here writes to the database.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MIN_POINTS = 3       # listings needed per model
MIN_SPAN_DAYS = 45   # ...spread over at least this long, else it's not a trend
FLAT_PCT = -3.0      # trend at or above this counts as "holding" (flat or rising)
MAX_ROWS = 8
YEAR_DAYS = 365
CACHE_TTL = 1800
_cache: dict = {"at": 0.0, "out": None}


def _parse(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def hold_models(points: list[dict]) -> list[dict]:
    """Pure. points: dicts with model, label, price (number), at (ISO time).
    Returns [{"model", "label", "n", "value", "trend_pct", "text"}] for models
    that are flat or rising, best trend first. Empty when data is thin."""
    by: dict[str, dict] = {}
    for p in points or []:
        t, v = _parse(p.get("at")), p.get("price")
        if not p.get("model") or t is None or not isinstance(v, (int, float)) or v <= 0:
            continue
        e = by.setdefault(p["model"], {"label": p.get("label") or p["model"], "pts": []})
        e["pts"].append((t, float(v)))
    out = []
    for model, e in by.items():
        pts = sorted(e["pts"])
        if len(pts) < MIN_POINTS or (pts[-1][0] - pts[0][0]).days < MIN_SPAN_DAYS:
            continue
        half = len(pts) // 2
        old = statistics.median(v for _, v in pts[:half])
        new = statistics.median(v for _, v in pts[len(pts) - max(half, 1):])
        if old <= 0:
            continue
        pct = round((new / old - 1) * 100, 1)
        if pct < FLAT_PCT:
            continue
        word = "rising" if pct > 3 else "flat"
        out.append({"model": model, "label": e["label"], "n": len(pts), "value": round(new),
                    "trend_pct": pct, "text": f"{word} {pct:+.0f}%"})
    out.sort(key=lambda r: (-r["trend_pct"], -r["n"]))
    return out[:MAX_ROWS]


def track() -> list[dict]:
    from scrapers.enrich import parse_price
    from scrapers.market_value import _key, _SKIP, skip_listing
    from scrapers.store import DB_PATH
    cut = (datetime.now(timezone.utc) - timedelta(days=YEAR_DAYS)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        stars = conn.execute("SELECT title FROM seen WHERE favorite = 1 AND title IS NOT NULL"
                             " ORDER BY first_seen DESC").fetchall()
        labels: dict[str, str] = {}
        for (t,) in stars:
            k = _key(t)
            if k and ":module" not in k:
                labels.setdefault(k, t)
        if not labels:
            return []
        rows = conn.execute(
            "SELECT title, price, first_seen FROM seen WHERE first_seen >= ? AND price IS NOT NULL"
            " AND duplicate = 0 AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND COALESCE(description, '') NOT LIKE 'Auction%'", (cut,)).fetchall()
    finally:
        conn.close()
    points = []
    for title, price, at in rows:
        if not title or _SKIP.search(title) or skip_listing(title):
            continue
        k = _key(title)
        v = parse_price(price) if k in labels else None
        if v and v >= 25:
            points.append({"model": k, "label": labels[k], "price": v, "at": at})
    return hold_models(points)


def stats() -> list[dict]:
    """Cached for 30 minutes. Never raises; empty list = render nothing."""
    if _cache["out"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["out"]
    try:
        out = track()
    except Exception:
        logger.exception("hold tracker")
        return _cache["out"] or []
    _cache.update(at=time.time(), out=out)
    return out
