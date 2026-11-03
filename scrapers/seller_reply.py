"""Seller-reply speed chip.

Pure functions over the offer log (scrapers/offer_log.py), no network. For a
listing's seller, reply_chip() says how often that seller answered the offers
you sent (anything but "No reply"), and how fast when an entry carries a
"replied_at" timestamp. The log only stores the send time and the reply kind
today, so the speed part appears only once replied_at exists; the reply rate
works now. No offers to that seller, or nobody ever replied and only one offer
sent: nothing shown (None). Never raises.
"""
import os
from datetime import datetime
from typing import Optional

_cache: dict = {"mtime": None, "path": None, "items": []}


def _ts(v) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _hours(o) -> Optional[float]:
    """Hours from offer sent to seller reply, or None when unknown / nonsense."""
    a, b = _ts(o.get("at")), _ts(o.get("replied_at"))
    if not a or not b:
        return None
    try:
        h = (b - a).total_seconds() / 3600
    except TypeError:  # naive vs aware
        return None
    return h if 0 <= h < 24 * 60 else None


def _label(h: float) -> str:
    if h < 1:
        return "under an hour"
    if h < 48:
        return f"~{round(h)}h"
    return f"~{round(h / 24)}d"


def reply_stats(items, source, seller) -> Optional[dict]:
    """{'n', 'replied', 'hours'} for this source + seller, or None if no offers."""
    try:
        src = str(source or "").split(" — ")[0][:60]
        sel = str(seller or "")[:60]
        if not sel.strip():
            return None
        mine = [o for o in (items or []) if isinstance(o, dict)
                and o.get("seller") == sel and (not src or o.get("source") == src)]
        if not mine:
            return None
        replied = [o for o in mine if o.get("reply") in ("accepted", "countered", "declined")]
        hrs = sorted(h for h in (_hours(o) for o in replied) if h is not None)
        med = None
        if hrs:
            m = len(hrs) // 2
            med = hrs[m] if len(hrs) % 2 else (hrs[m - 1] + hrs[m]) / 2
        return {"n": len(mine), "replied": len(replied), "hours": med}
    except Exception:
        return None


def chip(stats) -> Optional[dict]:
    """{'text', 'title', 'good'} for the card, or None when there is nothing useful."""
    try:
        if not stats or not stats["n"]:
            return None
        n, r, h = stats["n"], stats["replied"], stats["hours"]
        if r == 0 and n < 2:
            return None  # one unanswered offer is too little to say anything
        if r == 0:
            return {"text": f"No reply to {n} offers", "good": False,
                    "title": f"You sent this seller {n} offers and none got a reply"}
        text = f"Replied to {r} of {n} offer{'s' if n != 1 else ''}"
        if h is not None:
            text += f" · usually {_label(h)}"
        return {"text": text, "good": True,
                "title": "From your offer log: how often this seller answered your offers"
                         + (" and how long it took" if h is not None else "")}
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


def reply_chip(source, seller) -> Optional[dict]:
    """Card chip for a listing's seller, read from the (cached) offer log."""
    try:
        return chip(reply_stats(_items(), source, seller))
    except Exception:
        return None
