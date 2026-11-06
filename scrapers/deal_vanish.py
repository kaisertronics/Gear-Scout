"""
Deal-vanish tracker (Mics page card).

For mic deals Gear Scout flagged (each one is recorded in mic_alerted when it
was pushed), works out how long they stayed up before they sold or vanished,
per site. "Gone" = marked sold, or not seen by any scrape for STALE_DAYS.
Read-only: nothing here writes to the database.
"""
import logging
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MIN_PER_SITE = 3  # fewer gone deals than this on a site is too little to say anything
STALE_DAYS = 3
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


def label(hours: float) -> str:
    """'under an hour', '6h', '2 days'."""
    if hours < 1:
        return "under an hour"
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f} days"


def vanish_stats(rows: list[dict], now: datetime | None = None) -> dict:
    """Pure. rows: dicts with source_name, alerted_at (when the deal was
    flagged), last_seen, sold, sold_at, sold_checked_at. Returns
    {"sites": [{"site", "n", "median_hours", "text"}], "n": total gone deals}.
    Only sites with MIN_PER_SITE+ gone deals are listed."""
    now = now or datetime.now(timezone.utc)
    stale = now - timedelta(days=STALE_DAYS)
    by_site: dict[str, list[float]] = {}
    for r in rows:
        start = _parse(r.get("alerted_at"))
        if not start:
            continue
        seen = _parse(r.get("last_seen"))
        if r.get("sold"):
            end = _parse(r.get("sold_at") or r.get("sold_checked_at")) or seen
            if seen and (not end or seen < end):
                end = seen  # last time it was seen live is the best guess
        elif seen and seen < stale:
            end = seen
        else:
            continue  # still up (or too recent to tell)
        if not end or end < start:
            continue
        by_site.setdefault(_site(r.get("source_name")), []).append((end - start).total_seconds() / 3600)
    sites = []
    for s, v in by_site.items():
        if len(v) < MIN_PER_SITE:
            continue
        m = statistics.median(v)
        sites.append({"site": s, "n": len(v), "median_hours": round(m, 1),
                      "text": f"Deals like this last ~{label(m)} on {s}"})
    sites.sort(key=lambda x: x["median_hours"])
    return {"sites": sites, "n": sum(len(v) for v in by_site.values())}


def gone_within(stats: dict, source: str | None) -> str:
    """'Typically gone within ~6h' for a listing's source, or '' if unknown."""
    site = _site(source)
    for s in (stats or {}).get("sites") or []:
        if s["site"] == site:
            return f"Typically gone within ~{label(s['median_hours'])}"
    return ""


def track() -> dict:
    from scrapers.store import DB_PATH
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT s.source_name, s.last_seen, s.sold, s.sold_at, s.sold_checked_at, MIN(a.at) AS alerted_at"
            " FROM mic_alerted a JOIN seen s ON s.url = a.url"
            " WHERE s.hidden = 0 AND COALESCE(s.duplicate, 0) = 0 GROUP BY s.url")]
    finally:
        conn.close()
    return vanish_stats(rows)


def stats() -> dict:
    """Cached for a few minutes. Never raises; empty stats = render nothing."""
    if _cache["out"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["out"]
    try:
        out = track()
    except Exception:
        logger.exception("deal vanish tracker")
        return _cache["out"] or {"sites": [], "n": 0}
    _cache.update(at=time.time(), out=out)
    return out
