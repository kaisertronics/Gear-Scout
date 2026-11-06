"""Deal clock: how long similar mic listings usually last before they sell.

Uses the `seen` table: hours from first_seen to sold_at for rows with sold = 1.
Comparable = same source type (reverb / ebay / facebook / other) and same mic
target (mic_focus.classify key); falls back to all sold mics of that source
type, then all sold mics. Needs >= MIN_N comparable rows, else no text.
"""
import re
import time
from datetime import datetime, timezone
from statistics import median
from typing import Optional

MIN_N = 5
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "rows": None}


def _src_type(name: str) -> str:
    n = (name or "").lower()
    for k in ("reverb", "ebay", "facebook"):
        if k in n:
            return k
    return "other"


def _ts(s) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _key(title: str) -> Optional[str]:
    from scrapers import mic_focus
    t = mic_focus.classify(title or "")
    return t["key"] if t else None


def _load(conn=None) -> list:
    """[(source_type, mic_key, hours)] for sold mic listings, cached ~30 min."""
    if _cache["rows"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["rows"]
    own = conn is None
    if own:
        from scrapers.store import _conn
        conn = _conn()
    try:
        cur = conn.execute("SELECT title, source_name, first_seen, sold_at FROM seen "
                           "WHERE sold = 1 AND sold_at IS NOT NULL AND first_seen IS NOT NULL")
        out = []
        for row in cur.fetchall():
            title, src, fs, sa = row[0], row[1], row[2], row[3]
            k = _key(title)
            if not k:
                continue
            a, b = _ts(fs), _ts(sa)
            if not a or not b:
                continue
            h = (b - a).total_seconds() / 3600
            if h > 0:
                out.append((_src_type(src), k, h))
    finally:
        if own:
            conn.close()
    _cache["rows"], _cache["at"] = out, time.time()
    return out


def typical_sale_hours(row_or_title, conn=None) -> Optional[float]:
    """Median hours to sell for comparable listings, or None if < MIN_N."""
    if isinstance(row_or_title, dict):
        title, src = row_or_title.get("title") or "", _src_type(row_or_title.get("source_name"))
    else:
        title, src = str(row_or_title or ""), None
    data = _load(conn)
    key = _key(title)
    pools = []
    if src and key:
        pools.append([h for s, k, h in data if s == src and k == key])
    if key:
        pools.append([h for s, k, h in data if k == key])
    if src:
        pools.append([h for s, k, h in data if s == src])
    pools.append([h for s, k, h in data])
    for p in pools:
        if len(p) >= MIN_N:
            return float(median(p))
    return None


def clock_text(row) -> str:
    try:
        h = typical_sale_hours(row)
        if h is None:
            return ""
        if h < 1:
            t = f"{max(1, round(h * 60))} min"
        elif h < 48:
            t = f"{h:.0f} hours" if h >= 1.5 else "1 hour"
        else:
            t = f"{h / 24:.0f} days"
        return f"similar mics usually sell in ~{t}"
    except Exception:
        return ""
