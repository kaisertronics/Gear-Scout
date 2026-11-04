"""
Resale listing drafter (Saved page, "Draft a resale listing").

For gear you bought: finds the original ad Gear Scout saw (same model words,
price near what you paid, seen before the purchase date), then writes a
ready-to-paste title + description from templates. Condition and included
accessories come only from the ad text; the asking price comes from the going
used value and your cost. No network, no LLM; the drafts are plain text.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from scrapers.deal_calibration import _when, _words, _num

logger = logging.getLogger(__name__)
PRICE_TOL = 0.15
FEE_RATE = 0.08          # Reverb fee + payment processing; the floor covers it
MIN_MARGIN = 1.05        # below this over break-even there's no room to haggle
OFFER_ROOM = 1.08        # open-to-offers asks sit this far over the used value
MINT_ADJ, ROUGH_ADJ = 1.05, 0.85
MAX_DESC = 1500
LOOKBACK_DAYS = 730
CACHE_TTL = 1800
_cache: dict = {"at": 0.0, "rows": None}
_lock = threading.Lock()

# (pattern, honest wording). First matching tier wins, worst first, so a
# "mint" ad that also says "scratched" is described as worn.
_CONDITION = (
    (re.compile(r"\bas[- ]is\b|\bfor parts\b|\bnot working\b|\bneeds repair\b", re.I),
     "Sold as described; it has issues noted in the original ad, so please ask before buying."),
    (re.compile(r"\bheavy wear\b|\bbeat[- ]up\b|\bwell used\b|\bworn\b|\brough\b|\bdents?\b|\bscratch(?:es|ed)?\b|\bcosmetic (?:wear|damage)\b", re.I),
     "Used with visible cosmetic wear (scratches, scuffs or dings)."),
    (re.compile(r"\bmint\b|\blike[- ]new\b|\bpristine\b|\bbarely used\b|\bexcellent\b", re.I),
     "Excellent used condition with very little sign of use."),
    (re.compile(r"\bgood condition\b|\bvery good\b|\bworks (?:great|perfectly|well)\b|\bfully (?:working|functional)\b", re.I),
     "Good used condition, fully working."),
)
_DEFAULT_CONDITION = "Used. [Confirm it's fully working and note any wear, scratches or noise.]"

# (pattern, label) for things an ad says come with the item
_ACCESSORIES = (
    (re.compile(r"\bshock ?mount\b", re.I), "shock mount"),
    (re.compile(r"\bpop ?(?:filter|screen)\b", re.I), "pop filter"),
    (re.compile(r"\b(?:hard|flight|road)? ?case\b|\bgig ?bag\b", re.I), "case"),
    (re.compile(r"\bpower (?:supply|cable|cord|adapter)\b|\bpsu\b|\bwall ?wart\b", re.I), "power supply"),
    (re.compile(r"\bxlr cables?\b|\bcables?\b", re.I), "cable"),
    (re.compile(r"\bmanual\b", re.I), "manual"),
    (re.compile(r"\boriginal box\b|\bin (?:the )?box\b|\bwith box\b|\bbox\b", re.I), "original box"),
    (re.compile(r"\bmic ?clip\b|\bclip\b", re.I), "mic clip"),
    (re.compile(r"\bstand\b", re.I), "stand"),
    (re.compile(r"\brack ?ears?\b|\brack ?mount kit\b", re.I), "rack ears"),
    (re.compile(r"\bwindscreen\b", re.I), "windscreen"),
)


def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def condition_line(ad_text: str | None) -> str:
    """Pure. Honest condition sentence from the ad text; a placeholder to fill
    in when the ad says nothing."""
    for rx, line in _CONDITION:
        if rx.search(ad_text or ""):
            return line
    return _DEFAULT_CONDITION


def accessories(ad_text: str | None) -> list[str]:
    """Pure. Labels of accessories the ad mentions, in a fixed order."""
    t = ad_text or ""
    found = [label for rx, label in _ACCESSORIES if rx.search(t)]
    if "case" in found and "cable" in found and re.search(r"\bcable case\b", t, re.I):
        found.remove("case")
    return found


def suggest_price(cost, used, tier: str = "normal") -> dict | None:
    """Pure. {"ask", "floor", "firm", "basis"} or None with no cost and no used
    value. floor = break-even after fees. Ask follows the used value (nudged
    for a mint/rough ad), is never under floor, and with no used value falls
    back to cost + 25%. 'firm' = too little margin over the floor to haggle."""
    cost, used = _num(cost), _num(used)
    cost = cost if cost and cost > 0 else None
    used = used if used and used > 0 else None
    if not cost and not used:
        return None
    floor = _round5(cost / (1 - FEE_RATE)) if cost else None
    if used:
        adj = {"mint": MINT_ADJ, "rough": ROUGH_ADJ}.get(tier, 1.0)
        base, basis = used * adj, f"going used value ${used:,.0f}"
    else:
        base, basis = cost * 1.25, f"your cost ${cost:,.0f} plus 25% (no used value found)"
    firm = bool(floor) and base < floor * MIN_MARGIN
    ask = _round5(base if firm else base * OFFER_ROOM)
    if floor and ask < floor:
        ask, basis = floor, basis + f"; raised to break-even after ~{round(FEE_RATE * 100)}% fees"
    return {"ask": ask, "floor": floor, "firm": firm, "basis": basis}


def draft(item: str, cost=None, used=None, ad_title: str | None = None,
          ad_text: str | None = None) -> dict:
    """Pure. Returns {"title", "description", "text", "ask", "floor", "firm",
    "basis", "found_ad"}. text = title + blank line + description, ready to
    copy. ad_title/ad_text come from the original listing (may be None)."""
    from scrapers import market_value
    name = re.sub(r"\s+", " ", str(item or "").strip()) or "Gear"
    blob = f"{ad_title or ''}\n{ad_text or ''}"
    tier = market_value._tier(ad_title, ad_text)
    price = suggest_price(cost, used, tier)
    cond = condition_line(blob)
    acc = accessories(blob)
    title = f"{name} - Used" if cond != _CONDITION[2][1] else f"{name} - Excellent Condition"
    lines = [f"{name}.", "", f"Condition: {cond}", ""]
    lines.append("Included: " + (", ".join(acc) + ". Nothing else unless listed."
                                 if acc else "the unit itself. [List any cables, case or extras you're including.]"))
    if price:
        lines += ["", f"Asking ${price['ask']:,.0f}. " + ("Price is firm." if price["firm"]
                                                        else "Open to reasonable offers.")]
    lines += ["", "Ships well packed and insured. Local pickup welcome."]
    desc = "\n".join(lines)
    return {"title": title, "description": desc, "text": f"{title}\n\n{desc}",
            "ask": price["ask"] if price else None, "floor": price["floor"] if price else None,
            "firm": price["firm"] if price else None, "basis": price["basis"] if price else "",
            "found_ad": bool(ad_title or ad_text)}


def find_ad(purchase: dict, listings: list[dict]) -> dict | None:
    """Pure. The earliest listing seen before the purchase date whose title
    holds every model word of the item and whose price is within PRICE_TOL of
    what was paid. Without a usable date the date test is skipped. purchase:
    item, paid, date. listings: title, price, first_seen, description."""
    iw, paid = _words(purchase.get("item")), _num(purchase.get("paid"))
    bought = _when(purchase.get("date"))
    if not iw or not paid or paid <= 0:
        return None
    best = None
    for l in listings or []:
        p, t = _num(l.get("price")), _when(l.get("first_seen"))
        if not p or abs(p - paid) > PRICE_TOL * paid or not iw <= _words(l.get("title")):
            continue
        if bought and t and t >= bought:
            continue
        key = t or datetime.max.replace(tzinfo=timezone.utc)
        if best is None or key < best[0]:
            best = (key, l)
    return best[1] if best else None


def _listings() -> list[dict]:
    from scrapers.store import DB_PATH
    cut = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("SELECT title, price, first_seen, description FROM seen WHERE first_seen >= ?"
                            " AND price IS NOT NULL AND title IS NOT NULL", (cut,)).fetchall()
    finally:
        conn.close()
    return [{"title": t, "price": p, "first_seen": f, "description": (d or "")[:MAX_DESC]}
            for t, p, f, d in rows]


def _cached_listings() -> list[dict]:
    if _cache["rows"] is not None and time.time() - _cache["at"] < CACHE_TTL:
        return _cache["rows"]
    with _lock:
        if _cache["rows"] is None or time.time() - _cache["at"] >= CACHE_TTL:
            _cache.update(rows=_listings(), at=time.time())
        return _cache["rows"]


def drafts(items: list[dict]) -> dict:
    """{item name: draft dict} for the Saved page's purchases. Falls back to
    market_value when the sheet row has no used value. Never raises; a failure
    just leaves that item (or all of them) without a draft."""
    out: dict = {}
    try:
        listings = _cached_listings()
    except Exception:
        logger.exception("resale drafter listings")
        listings = []
    for it in items or []:
        try:
            name = it.get("item")
            if not name or name in out:
                continue
            ad = find_ad(it, listings) or {}
            used = it.get("used")
            if not used:
                from scrapers import market_value
                used = market_value.value(ad.get("title") or name, ad.get("description"))[0]
            out[name] = draft(name, it.get("paid"), used, ad.get("title"), ad.get("description"))
        except Exception:
            logger.exception("resale drafter %s", it.get("item"))
    return out
