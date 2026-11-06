"""
Model scarcity: how many distinct ads of this exact model (same key as
market_value._key) Gear Scout has ever seen. Display only; a chip shows
for rare / very rare models, never for common ones.

One query builds the whole index; cached ~30 minutes. Never raises.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VERY_RARE_MAX = 2     # 1-2 ads ever seen
RARE_MAX = 4          # 3-4 ads
UNCOMMON_MAX = 14     # 5-14 ads; 15+ is common

_cache: dict = {"at": 0.0, "idx": None, "months": 1}
_lock = threading.Lock()


def classify(n: int) -> str:
    if n <= VERY_RARE_MAX:
        return "very rare"
    if n <= RARE_MAX:
        return "rare"
    if n <= UNCOMMON_MAX:
        return "uncommon"
    return "common"


def _build() -> dict:
    from scrapers.market_value import _key
    from scrapers.store import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT title, price, first_seen FROM seen WHERE duplicate = 0").fetchall()
    ads: dict = {}
    seen = set()
    first = None
    for title, price, fs in rows:
        if not title:
            continue
        ad = (re.sub(r"\s+", " ", title.lower()).strip(), str(price or ""))
        if ad in seen:
            continue
        seen.add(ad)
        k = _key(title)
        if k:
            ads[k] = ads.get(k, 0) + 1
        if fs and (first is None or fs < first):
            first = fs
    months = 1
    try:
        if first:
            d = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            months = max(1, round((datetime.now(timezone.utc) - d).days / 30))
    except Exception:
        months = 1
    _cache.update(at=time.time(), idx=ads, months=months)
    return ads


def index(max_age: float = 1800) -> dict:
    if _cache["idx"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["idx"]
    if _cache["idx"] is not None:
        if not _lock.acquire(blocking=False):
            return _cache["idx"]
    else:
        _lock.acquire()
    try:
        if _cache["idx"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["idx"]
        return _build()
    finally:
        _lock.release()


def scarcity(title: str) -> Optional[dict]:
    """{'level': 'rare'|'very rare', 'n': ads, 'months': span, 'label': text}
    for rare models only; None for common/uncommon/unknown or on any error."""
    try:
        from scrapers.market_value import _key
        k = _key(title or "")
        if not k:
            return None
        n = index().get(k, 0)
        if n <= 0:
            return None
        level = classify(n)
        if level not in ("rare", "very rare"):
            return None
        m = _cache.get("months") or 1
        label = "%s: only %d ad%s seen in %d month%s" % (
            "Very rare" if level == "very rare" else "Rare", n, "" if n == 1 else "s", m, "" if m == 1 else "s")
        return {"level": level, "n": n, "months": m, "label": label}
    except Exception:
        logger.debug("scarcity failed", exc_info=True)
        return None
