"""Offer odds line.

Pure functions over the offer log (scrapers/offer_log.py), no network. For a
listing's site, odds_chip() says how often your offers of about 15% under
asking were accepted, e.g. "Offers ~15% under ask: accepted 3 of 7 on Reverb".
Needs at least MIN_N logged offers near that depth (same site first, then all
sites); otherwise None and the card shows nothing. Never raises.
"""
import os
from typing import Optional

TARGET = 15.0   # percent under asking we quote odds for
WIDTH = 5.0     # offers within +/- this many points of TARGET count
MIN_N = 3       # data points needed before anything is shown

_cache: dict = {"mtime": None, "path": None, "items": []}


def _disc(o) -> Optional[float]:
    try:
        a, f = float(o["asking"]), float(o["offered"])
        return (a - f) / a * 100 if a > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


def _near(items) -> list:
    out = []
    for o in items or []:
        if not isinstance(o, dict) or o.get("reply") not in ("accepted", "countered", "declined", "no_reply"):
            continue
        d = _disc(o)
        if d is not None and abs(d - TARGET) <= WIDTH:
            out.append(o)
    return out


def odds_stats(items, source) -> Optional[dict]:
    """{'n', 'accepted', 'scope'} for offers near TARGET%, or None when < MIN_N."""
    try:
        src = str(source or "").split(" — ")[0][:60]
        near = _near(items)
        for scope, rows in ((src, [o for o in near if src and o.get("source") == src]),
                            ("", near)):
            if len(rows) >= MIN_N:
                return {"n": len(rows), "accepted": sum(1 for o in rows if o["reply"] == "accepted"),
                        "scope": scope}
        return None
    except Exception:
        return None


def chip(stats) -> Optional[dict]:
    try:
        if not stats or stats["n"] < MIN_N:
            return None
        where = f"on {stats['scope']}" if stats["scope"] else "across all sites"
        return {"text": f"Offers ~{TARGET:g}% under ask: accepted {stats['accepted']} of {stats['n']} {where}",
                "good": stats["accepted"] > 0,
                "title": f"From your offer log: offers {TARGET - WIDTH:g}-{TARGET + WIDTH:g}% under the asking price"}
    except Exception:
        return None


def _items() -> list:
    try:
        from scrapers import offer_log
        m = os.stat(offer_log.PATH).st_mtime
        if _cache["mtime"] != m or _cache["path"] != str(offer_log.PATH):
            _cache.update(mtime=m, path=str(offer_log.PATH), items=offer_log.load())
        return _cache["items"]
    except Exception:
        return []


def odds_chip(source) -> Optional[dict]:
    try:
        return chip(odds_stats(_items(), source))
    except Exception:
        return None
