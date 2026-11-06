"""Hidden sellers: one tap on a card hides every ad from that seller (site +
seller name), now and later. Display filtering only: nothing is deleted from
the database. Stored in /data/hidden_sellers.json (never touches config);
HIDDEN_SELLERS_PATH overrides the file for tests. Sellers you follow (seller
watchlist) or have starred an ad from are never hidden."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PATH = Path(os.environ.get("HIDDEN_SELLERS_PATH") or "/data/hidden_sellers.json")

_cache: dict = {"mtime": None, "path": None, "items": [], "keys": frozenset()}


def _src(source) -> str:
    return str(source or "").split(" — ")[0].strip()[:60]


def key(source, seller) -> tuple:
    return (_src(source).lower(), re.sub(r"\s+", " ", str(seller or "")).strip().lower())


def load() -> list[dict]:
    try:
        items = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [w for w in items if isinstance(w, dict) and w.get("source") and w.get("seller")] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PATH)


def add(source, seller) -> Optional[dict]:
    """None if blank/generic seller, or the seller is on the watchlist."""
    from scrapers import seller_watch
    from scrapers.base import usable_seller
    seller = re.sub(r"\s+", " ", str(seller or "")).strip()[:60]
    if not _src(source) or not usable_seller(seller) or seller_watch.is_watched(source, seller):
        return None
    items = load()
    k = key(source, seller)
    for w in items:
        if key(w["source"], w["seller"]) == k:
            return w
    w = {"source": _src(source), "seller": seller, "added": datetime.now(timezone.utc).isoformat()}
    items.append(w)
    _save(items)
    return w


def remove(source, seller) -> bool:
    items = load()
    k = key(source, seller)
    kept = [w for w in items if key(w["source"], w["seller"]) != k]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def keys() -> frozenset:
    """Set of (site, seller) keys, cached on the file's mtime. Never raises."""
    try:
        m = PATH.stat().st_mtime
    except OSError:
        return frozenset()
    try:
        if _cache["mtime"] != m or _cache["path"] != str(PATH):
            items = load()
            _cache.update(mtime=m, path=str(PATH), items=items,
                          keys=frozenset(key(w["source"], w["seller"]) for w in items))
        return _cache["keys"]
    except Exception:
        return frozenset()


def _protected() -> set:
    """Keys of sellers that must stay visible: followed, or an ad was starred."""
    out = set()
    try:
        from scrapers import seller_watch
        out |= {key(w["source"], w["seller"]) for w in seller_watch.load()}
    except Exception:
        pass
    try:
        from scrapers.store import _conn
        c = _conn()
        try:
            for src, sel in c.execute(
                    "SELECT DISTINCT source_name, seller FROM seen WHERE favorite = 1 AND seller IS NOT NULL"):
                out.add(key(src, sel))
        finally:
            c.close()
    except Exception:
        pass
    return out


def hidden_set() -> frozenset:
    """Keys to filter out right now (hidden minus protected). Empty when none
    are hidden, so the common case costs nothing."""
    ks = keys()
    if not ks:
        return frozenset()
    return frozenset(ks - _protected())


def is_hidden(listing: dict, hidden: frozenset) -> bool:
    if not hidden or not listing.get("seller") or listing.get("favorite"):
        return False
    return key(listing.get("source_name"), listing.get("seller")) in hidden


def is_hidden_seller(source, seller) -> bool:
    """For the card button label."""
    return bool(seller) and key(source, seller) in keys()
