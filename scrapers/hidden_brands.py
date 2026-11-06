"""Hidden-brand memory: brands the owner keeps hiding (the X on a card) drop
to a collapsed "usually hidden" line on the Dashboard, unless the ad is a real
steal (UNLESS_PCT or more under its comp) or is starred.

Read-only: counts hidden rows in the database, changes nothing."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

HIDE_THRESHOLD = 25   # hidden ads of one brand before it is "usually hidden"
UNLESS_PCT = 60       # an ad this far under its comp still shows
_CACHE_SECS = 300

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "counts": {}}


def hidden_brand_counts() -> dict[str, int]:
    """{brand: how many ads of it you've hidden}, cached 5 minutes."""
    with _lock:
        if time.time() - _cache["at"] < _CACHE_SECS:
            return _cache["counts"]
    counts: dict[str, int] = {}
    try:
        from scrapers.enrich import canonical_brand
        from scrapers.store import _conn
        c = _conn()
        try:
            for (title,) in c.execute("SELECT title FROM seen WHERE hidden = 1"):
                b = canonical_brand(title)
                if b:
                    counts[b] = counts.get(b, 0) + 1
        finally:
            c.close()
    except Exception:
        logging.exception("Couldn't count hidden brands")
        counts = _cache["counts"]
    with _lock:
        _cache.update(at=time.time(), counts=counts)
    return counts


def usually_hidden_brands(threshold: int = HIDE_THRESHOLD) -> dict[str, int]:
    return {b: n for b, n in hidden_brand_counts().items() if n >= threshold}


def is_usually_hidden(listing: dict, brands: dict[str, int]) -> Optional[str]:
    """The brand if this ad belongs in the collapsed line, else None.
    Starred ads and big discounts (UNLESS_PCT+) always stay in the main list."""
    if not brands or listing.get("favorite"):
        return None
    from scrapers.enrich import canonical_brand
    brand = canonical_brand(listing.get("title"))
    if brand not in brands:
        return None
    if ((listing.get("comp") or {}).get("pct") or 0) >= UNLESS_PCT:
        return None
    return brand
