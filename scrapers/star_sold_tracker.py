"""
Starred-ad sold tracker (Mics page card).

Looks at the ads the owner starred that have since sold or gone away and works
out how long they usually lasted (first seen -> last seen live / noticed sold).
Then it lists the starred ads still up that have been listed long enough that
they may go any day now. Read-only: nothing here writes to the database.
"""
import logging
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MIN_SOLD = 3  # fewer sold stars than this is too little to say anything
STALE_DAYS = 3  # not seen by any scrape for this long = probably gone already
CACHE_TTL = 300
_cache: dict = {"at": 0.0, "out": None}


def _parse(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _site(source: str | None) -> str:
    return re.split(r"\s+[—-]\s+", source or "", maxsplit=1)[0].strip() or "Other"


def _days(a, b) -> float | None:
    a, b = _parse(a), _parse(b)
    if not a or not b:
        return None
    return max(0.0, (b - a).total_seconds() / 86400)


def _label(days: float) -> str:
    return "under a day" if days < 1 else f"{days:.0f} day{'' if round(days) == 1 else 's'}"


def track(now: datetime | None = None) -> dict:
    """{"enough": bool, "n_sold", "median_days", "typical", "by_site", "at_risk", "note"}"""
    from scrapers.store import DB_PATH
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        sold = [dict(r) for r in conn.execute(
            "SELECT title, source_name, first_seen, last_seen, sold_at, sold_checked_at FROM seen"
            " WHERE favorite = 1 AND sold = 1 AND first_seen IS NOT NULL")]
        live = [dict(r) for r in conn.execute(
            "SELECT title, url, price, source_name, first_seen, last_seen FROM seen"
            " WHERE favorite = 1 AND hidden = 0 AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0"
            " AND first_seen IS NOT NULL")]
    finally:
        conn.close()

    spans, by_site = [], {}
    for r in sold:
        # last time it was seen live is the best guess; else when the checker noticed it gone
        d = _days(r["first_seen"], r["last_seen"] or r["sold_at"] or r["sold_checked_at"])
        if d is None:
            continue
        spans.append(d)
        by_site.setdefault(_site(r["source_name"]), []).append(d)
    out = {"enough": len(spans) >= MIN_SOLD, "n_sold": len(spans), "median_days": None, "typical": "",
           "by_site": [], "at_risk": [], "note": ""}
    if not out["enough"]:
        out["note"] = ("Not enough sold stars yet to say how fast your starred ads sell "
                       f"({len(spans)} so far, need {MIN_SOLD}).")
        return out

    med = statistics.median(spans)
    out["median_days"] = round(med, 1)
    out["typical"] = _label(med)
    site_med = {s: statistics.median(v) for s, v in by_site.items() if len(v) >= 2}
    out["by_site"] = sorted(({"site": s, "days": _label(m), "n": len(by_site[s])}
                             for s, m in site_med.items()), key=lambda x: x["site"])
    threshold = max(1.0, med / 2)
    for r in live:
        if (_parse(r["last_seen"] or r["first_seen"]) or now) < now - timedelta(days=STALE_DAYS):
            continue  # not seen by any scrape lately: probably gone already
        age = _days(r["first_seen"], now.isoformat())
        site = _site(r["source_name"])
        fast = site in site_med and site_med[site] < med
        # a faster-selling site counts a little sooner
        if age is None or age < (threshold * 0.75 if fast else threshold):
            continue
        out["at_risk"].append({
            "title": (r["title"] or "")[:90], "url": r["url"], "price": r["price"], "site": site,
            "age": _label(age), "age_days": age,
            "why": f"up {_label(age)}; starred ads usually sell in about {_label(med)}"
                   + (f" ({site} ones in about {_label(site_med[site])})" if fast else "")})
    out["at_risk"].sort(key=lambda x: -x["age_days"])
    return out


def tracker() -> dict:
    """Cached for a few minutes so the Mics page never rescans per visit."""
    if _cache["out"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["out"]
    try:
        out = track()
    except Exception:
        logger.exception("star sold tracker")
        return _cache["out"] or {"enough": False, "n_sold": 0, "at_risk": [], "by_site": [],
                                 "note": "Sold tracker isn't available right now."}
    _cache.update(at=time.time(), out=out)
    return out
