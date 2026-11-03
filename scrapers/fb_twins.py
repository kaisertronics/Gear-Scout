"""Facebook ad-twin hider: a seller often posts the same Marketplace item twice.
Two Facebook ads are twins when the title is the same (or nearly), the price is
within a few dollars, the seller is the same (or the photo is the same, or the
seller is unknown and the location is the same), and they were posted within a
few days of each other. Display lists keep only the newest twin.

Pure and display-only: returns a new, shorter list; nothing is stored, edited
or deleted. Non-Facebook ads, starred ads and ads without a title/price are
never touched."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

PRICE_TOLERANCE = 5.0   # dollars
TITLE_SIMILARITY = 0.8  # share of words in common
MAX_DAYS_APART = 7


def is_facebook(l: dict) -> bool:
    return "facebook.com" in str(l.get("url") or "").lower()


def _words(title) -> frozenset:
    return frozenset(re.findall(r"[a-z0-9]+", str(title or "").lower()))


def _similar(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= TITLE_SIMILARITY


def _price(l) -> Optional[float]:
    from scrapers.enrich import parse_price
    return parse_price(l.get("price"))


def _seller(l) -> str:
    return str(l.get("seller") or l.get("seller_name") or "").strip().lower()


def _city(l) -> str:
    return str(l.get("location") or "").split(" @")[0].strip().lower()


def _photo(l) -> str:
    u = str(l.get("image_url") or "").split("?")[0].rstrip("/")
    return u.rsplit("/", 1)[-1].lower() if u else ""


def _when(l) -> Optional[datetime]:
    s = str(l.get("first_seen") or "")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def are_twins(a: dict, b: dict) -> bool:
    if not (is_facebook(a) and is_facebook(b)) or a.get("url") == b.get("url"):
        return False
    if not _similar(_words(a.get("title")), _words(b.get("title"))):
        return False
    pa, pb = _price(a), _price(b)
    if pa is None or pb is None or abs(pa - pb) > PRICE_TOLERANCE:
        return False
    sa, sb = _seller(a), _seller(b)
    if sa and sb:
        same_who = sa == sb
    else:
        ca, cb = _city(a), _city(b)
        same_who = bool(ca) and ca == cb
    if not same_who:
        pha, phb = _photo(a), _photo(b)
        same_who = bool(pha) and pha == phb
    if not same_who:
        return False
    ta, tb = _when(a), _when(b)
    if ta and tb and abs((ta - tb).days) > MAX_DAYS_APART:
        return False
    return True


def hide_twins(listings: list) -> list:
    """The list without the older Facebook twins (the newest one stays, in its
    own position). Starred ads stay. Any error returns the input unchanged."""
    try:
        fb = [l for l in listings if isinstance(l, dict) and is_facebook(l)]
        if len(fb) < 2:
            return listings
        newest_first = sorted(fb, key=lambda l: l.get("first_seen") or "", reverse=True)
        kept, hidden = [], set()
        for l in newest_first:
            if l.get("favorite") or not any(are_twins(l, k) for k in kept):
                kept.append(l)
            else:
                hidden.add(id(l))
        return [l for l in listings if id(l) not in hidden]
    except Exception:
        import logging
        logging.exception("fb twin hiding failed; showing full list")
        return listings
