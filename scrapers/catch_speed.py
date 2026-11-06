"""Catch-speed scoreboard: how fast we catch a new ad after the seller posts it.

Read-only. Uses the `seen` table: minutes from posted_at (when the seller posted
the ad) to first_seen (when we first saw it), per source, over the last 14 days.
Sources with no posted time are left out. Rows whose gap is negative by more
than a few minutes, or older than MAX_GAP_DAYS (re-listed or republished ads),
are ignored so they do not skew the numbers. Needs >= MIN_N rows per source.
"""
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

DAYS = 14
MIN_N = 5
MAX_GAP_DAYS = 7
_TTL = 10 * 60
_cache: dict = {"at": 0.0, "rows": None}
_lock = threading.Lock()


def _ts(s) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pct(vals: list, p: float) -> float:
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _group(name: str) -> str:
    """Craigslist and Facebook Marketplace have one source per city/region; fold each into one line."""
    n = (name or "").strip()
    if n.lower().startswith("craigslist"):
        return "Craigslist"
    if n.lower().startswith("fb marketplace"):
        return "FB Marketplace"
    return re.sub(r"\s+", " ", n)


def _compute() -> list:
    from scrapers.store import _conn
    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    conn = _conn()
    try:
        cur = conn.execute("SELECT source_name, posted_at, first_seen FROM seen "
                           "WHERE first_seen >= ? AND posted_at IS NOT NULL AND posted_at <> ''", (cutoff,))
        by: dict = {}
        for name, posted, seen in cur.fetchall():
            a, b = _ts(posted), _ts(seen)
            if not a or not b:
                continue
            mins = (b - a).total_seconds() / 60.0
            if mins < -5 or mins > MAX_GAP_DAYS * 1440:
                continue
            by.setdefault(_group(name), []).append(max(mins, 0.0))
    finally:
        conn.close()
    rows = [{"source": s, "n": len(v), "median": _pct(v, 0.5), "p90": _pct(v, 0.9)}
            for s, v in by.items() if len(v) >= MIN_N]
    rows.sort(key=lambda r: r["median"])
    return rows


def scoreboard() -> list:
    """[{source, n, median, p90}] in minutes, fastest first. Cached ~10 min; one
    caller computes at a time and others get the last result (or [])."""
    if _cache["rows"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["rows"]
    if not _lock.acquire(blocking=False):
        return _cache["rows"] or []
    try:
        if _cache["rows"] is not None and time.time() - _cache["at"] < _TTL:
            return _cache["rows"]
        try:
            rows = _compute()
        except Exception:
            rows = _cache["rows"] or []
        _cache.update(at=time.time(), rows=rows)
        return rows
    finally:
        _lock.release()


def fmt(mins: float) -> str:
    m = int(round(mins))
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{m / 60:.1f} h"
    return f"{m / 1440:.1f} d"
