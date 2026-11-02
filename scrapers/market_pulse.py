"""Mic market pulse: a small weekly trend for each mic you hunt.

For the last WEEKS weeks, per Mic Focus target (Neumann, Peluso, ...): how many
new listings turned up each week, the median asking price each week, and
whether the latest 4 weeks are up, down or flat against the 4 before. Where
sold history exists (deal_clock's first-seen-to-sold hours), the median days a
listing took to sell.

Ads are dated by the seller's posted date (`posted_at`) when known, else
first_seen; asks under the brand's floor (at least MIN_ASK) are accessories and
dropped; weeks before our records begin are left out. Reads the `seen` table
only (hidden and duplicate rows ignored); no network calls. A number is only shown with MIN_N or more listings behind it: nothing
is better than a misleading figure from one or two ads.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

logger = logging.getLogger(__name__)

WEEKS = 8
MIN_N = 3          # listings needed before a median / trend / days-to-sell is shown
TREND_MIN = 5      # ads needed in EACH 4-week half before an arrow is shown
MIN_ASK = 150      # under this (or the brand's own minimum, if higher) it's an accessory or a part
FLAT_PCT = 3      # a change smaller than this counts as "flat"
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "out": None}


def _ts(s) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _covered(i: int, now: datetime, start: Optional[datetime], weeks: int) -> bool:
    """True when week i (0 = oldest) began after our records start: before
    that the scraper wasn't running, so a low count means nothing."""
    return start is None or now - timedelta(days=(weeks - i) * 7) >= start


def weekly(items: list, now: datetime, weeks: int = WEEKS, start: Optional[datetime] = None) -> list[dict]:
    """items = [(date the ad was posted, price)] -> one dict per week, oldest
    first: {n, median}. median is None when the week has fewer than MIN_N
    prices; n and median are both None for a week before `start` (our records
    begin), which the chart leaves out."""
    buckets: list[list] = [[] for _ in range(weeks)]
    for when, price in items:
        if when is None or price is None:
            continue
        age = (now - when).total_seconds()
        if age < 0 or age >= weeks * 7 * 86400:
            continue
        buckets[weeks - 1 - int(age // (7 * 86400))].append(price)
    return [{"n": len(b), "median": round(median(b)) if len(b) >= MIN_N else None} if _covered(i, now, start, weeks)
            else {"n": None, "median": None} for i, b in enumerate(buckets)]


def trend(items: list, now: datetime, weeks: int = WEEKS, start: Optional[datetime] = None) -> Optional[dict]:
    """Median asking price of the latest half of the weeks against the half
    before: {pct, dir ('up'/'down'/'flat'), now, before}, or None when either
    half has fewer than TREND_MIN ads or the older half starts before our records."""
    half = weeks // 2
    if not all(_covered(i, now, start, weeks) for i in range(half)):
        return None
    recent, older = [], []
    for when, price in items:
        if when is None or price is None:
            continue
        age = (now - when).total_seconds()
        if age < 0 or age >= weeks * 7 * 86400:
            continue
        (recent if age < half * 7 * 86400 else older).append(price)
    if len(recent) < TREND_MIN or len(older) < TREND_MIN:
        return None
    a, b = median(recent), median(older)
    if not b:
        return None
    pct = round((a - b) / b * 100)
    d = "flat" if abs(pct) < FLAT_PCT else ("up" if pct > 0 else "down")
    return {"pct": pct, "dir": d, "now": round(a), "before": round(b)}


def days_to_sell(hours: list) -> Optional[dict]:
    """{days, n}: median days from first seen to sold, or None under MIN_N."""
    hours = [h for h in hours if h and h > 0]
    if len(hours) < MIN_N:
        return None
    return {"days": round(median(hours) / 24, 1), "n": len(hours)}


def _points(vals: list, w: int = 80, h: int = 22) -> str:
    """SVG polyline points for the weekly values (None = gap, drawn as a break
    by simply skipping the week); empty when fewer than 2 weeks have a value."""
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1
    step = w / max(len(vals) - 1, 1)
    return " ".join(f"{i * step:.1f},{h - 2 - (v - lo) / span * (h - 4):.1f}" for i, v in pts)


def build(cfg: dict, now: Optional[datetime] = None) -> list[dict]:
    """One row per Mic Focus mic that's switched on (outboard such as the
    Distressor left out) with listings in the window:
    {key, name, weeks:[{n, median, h}], total, line, trend, sell}."""
    from scrapers import deal_clock, mic_focus
    from scrapers.enrich import parse_price
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    cut = (now - timedelta(days=WEEKS * 7)).isoformat()
    per: dict = {t["key"]: [] for t in targets}
    with _conn() as conn:
        # the seller's posted date when known, else when we first saw the ad
        rows = conn.execute(
            "SELECT title, price, COALESCE(NULLIF(posted_at, ''), first_seen), description FROM seen"
            " WHERE hidden = 0 AND duplicate = 0 AND price IS NOT NULL AND price NOT LIKE 'C$%'"
            " AND (country IS NULL OR country = 'US') AND COALESCE(NULLIF(posted_at, ''), first_seen) >= ?", (cut,)).fetchall()
        first = conn.execute("SELECT MIN(first_seen) FROM seen WHERE first_seen IS NOT NULL").fetchone()
    start = _ts(first[0]) if first and first[0] else None  # before this the scraper wasn't running
    floors = {t["key"]: max(t.get("min_price") or 0, MIN_ASK) for t in targets}
    for title, price, found, desc in rows:
        if (desc or "").startswith("Auction"):
            continue  # a current bid isn't an asking price
        t = mic_focus.classify(title or "", targets)
        v = parse_price(price)
        if not t or not v or v < floors[t["key"]]:
            continue
        per[t["key"]].append((_ts(found), v))
    try:
        sold = deal_clock._load()
    except Exception:
        logger.exception("Market pulse: sold history unavailable")
        sold = []
    out = []
    for t in targets:
        items = per[t["key"]]
        if not items:
            continue
        wk = weekly(items, now, start=start)
        top = max((w["n"] or 0) for w in wk) or 1
        for w in wk:
            w["h"] = round(w["n"] / top * 20) if w["n"] is not None else None  # bar height in the 22px-tall chart
        out.append({"key": t["key"], "name": t["name"], "weeks": wk, "total": sum(w["n"] or 0 for w in wk),
                    "line": _points([w["median"] for w in wk]), "trend": trend(items, now, start=start),
                    "sell": days_to_sell([h for s, k, h in sold if k == t["key"]])})
    out.sort(key=lambda r: -r["total"])
    return out


def pulse(cfg: dict) -> list[dict]:
    """build(), cached for 30 minutes; never raises (an empty list on trouble)."""
    if _cache["out"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["out"]
    try:
        out = build(cfg)
    except Exception:
        logger.exception("Market pulse failed")
        return []
    _cache["out"], _cache["at"] = out, time.time()
    return out
