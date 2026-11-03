"""Flip engine: SUGGESTIONS ONLY. It reads listings already in the database and
shows which are cheap enough to buy and resell for a profit; it never posts,
lists, messages or contacts anyone. A flip qualifies when the expected resale
(used value from market_value, net of Reverb fees, minus shipping, via
resale_profit) leaves at least MIN_PROFIT dollars AND MIN_PROFIT_PCT percent
over what you'd pay, the value rests on MIN_COMPS+ comps, and the ad shows no
scam signs. Separate from every mic alert rule; nothing here changes them.

Flip tracker: the owner taps "I bought this", later enters the sold price and
shipping. Lives in /data/flips.json (never touches the DB or config);
FLIPS_PATH overrides the file for tests."""
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PATH = Path(os.environ.get("FLIPS_PATH") or "/data/flips.json")
MIN_PROFIT = 75        # dollars left after fees and shipping
MIN_PROFIT_PCT = 25    # ... and at least this much of what you'd pay
MIN_COMPS = 8         # listings behind the value (market_value needs 3; flips want more)
MIN_PRICE = 50         # under this the fixed shipping cost swamps everything
MAX_PRICE = 4000
LIVE_DAYS = 7          # last confirmed on its site within this many days
MAX_RESULTS = 60
TOO_GOOD_PCT = 75      # same cut as market_value: further under than this is a scam or mix-up
ASK_HAIRCUT = 0.90     # the used value is a median ASKING price; expect to net a bit less
SHIP_HEAVY = 90        # consoles, tape machines, speakers... (resale_profit assumes a mic-sized box)
_TTL = 600

_HEAVY = re.compile(r"\bconsole\b|\bmixers?\b|\bmixing\b|\bdesk\b|\breel\b|\btape (?:machine|deck)\b|\bmonitors?\b|"
                    r"\bspeakers?\b|\bamp(?:lifier)?\b|\bsubwoofer\b|\bpiano\b|\bkeyboard\b|\bsynth(?:esizer)?\b", re.I)
_NEW = re.compile(r"^\W*new\b|\[new\]|\bnew (?:in|with|old)\b|\bnos\b", re.I)

_cache: dict = {"at": 0.0, "items": None}


# ---------------------------------------------------------------- engine
def _rows() -> list[dict]:
    """Live, non-hidden, non-duplicate, US, priced listings seen recently."""
    from scrapers.store import _conn
    cut = (datetime.now(timezone.utc) - timedelta(days=LIVE_DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
            " AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND url NOT LIKE '%search/posts%' AND COALESCE(description, '') NOT LIKE 'Auction%'"
            " AND COALESCE(last_seen, first_seen) >= ?", (cut,))]


def find(cfg: Optional[dict] = None, max_age: float = _TTL) -> list[dict]:
    """Ranked flip suggestions (most profit first), cached 10 minutes. Each is
    the listing row plus r["flip"] = {buy, resale, net, profit, pct, n, ship, used}."""
    if _cache["items"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["items"]
    from scrapers import deal_checks, market_value as mv, resale_profit as rp
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form, needs_repair,
                                 parse_price, price_basis, title_says_sold)
    from scrapers.store import _conn
    exclude_words = (cfg or {}).get("exclude_words") or []
    cand, seen_ads = [], set()
    for r in _rows():
        title = r.get("title") or ""
        src = (r.get("source_name") or "").lower()
        if "kijiji" in src or "mcquade" in src:
            continue
        if mv.skip_listing(title) or title_says_sold(title) or exclude_match(title, exclude_words) \
                or is_accessory_only(title) or is_partial(title) or is_not_audio(title) \
                or needs_repair(title, r.get("description")) or item_form(title) == "plugin":
            continue
        ask = parse_price(r.get("price"))
        if not ask or not MIN_PRICE <= ask <= MAX_PRICE:
            continue
        _, qty, unit = price_basis(title, None, ask)
        unit = unit or ask
        ship_in = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        buy = round(unit + (ship_in or 0) / max(int(qty or 1), 1), 2)  # what you'd pay delivered
        ad = (re.sub(r"\s+", " ", title.lower()).strip(), round(ask))
        if ad in seen_ads:
            continue
        seen_ads.add(ad)
        if _NEW.search(title):  # new-in-box ads aren't comparable to used values
            continue
        est = rp.estimate(title, r.get("description"), buy)  # cheap pre-check, and the comp count
        if not est or est["n"] < MIN_COMPS:
            continue
        # Asking prices run above what things sell for, and heavy gear costs more to ship
        resale = round(est["value"] * ASK_HAIRCUT)
        ship = SHIP_HEAVY if _HEAVY.search(title) else est["ship"]
        net = resale * rp.NET_FACTOR - ship
        profit = net - buy
        pct = profit / buy * 100
        if profit < MIN_PROFIT or pct < MIN_PROFIT_PCT:
            continue
        if (1 - buy / est["value"]) * 100 > TOO_GOOD_PCT:  # absurdly cheap: usually not what it seems
            continue
        if deal_checks.scam_flags(r, est["value"], buy):
            continue
        r["flip"] = {"buy": buy, "resale": resale, "net": round(net), "profit": round(profit),
                     "pct": round(pct), "n": est["n"], "ship": ship, "basis": mv.basis(title)}
        cand.append(r)
    try:  # a photo that also sits on another seller's ad is a classic scam
        with _conn() as conn:
            stolen = deal_checks.stolen_photos(conn, {deal_checks._img_key(r.get("image_url")) for r in cand})
        cand = [r for r in cand if deal_checks._img_key(r.get("image_url")) not in stolen]
    except Exception:
        logger.exception("flip photo check")
    cand.sort(key=lambda r: -r["flip"]["profit"])
    _cache.update(at=time.time(), items=cand[:MAX_RESULTS])
    return _cache["items"]


def draft(r: dict) -> dict:
    """A plain-text resale listing to copy by hand: {"title","description","price"}.
    Built from the ad's title and condition words only; nothing is sent anywhere."""
    from scrapers import market_value as mv
    title = re.sub(r"\s+", " ", r.get("title") or "").strip()
    title = re.sub(r"\s*[-|,]\s*(?:free ship\w*|obo|must sell|local pickup)\s*$", "", title, flags=re.I)[:80]
    tier = mv._tier(title, r.get("description"))
    cond = {"mint": "Excellent condition, lightly used",
            "rough": "Used with visible cosmetic wear (see photos)"}.get(tier, "Used, good working condition")
    price = int(round(r["flip"]["resale"] / 5.0)) * 5
    lines = [title, "",
             f"{cond}.",
             "Tested and working as it should. [Confirm after you test it.]",
             "[Add: serial number, what is included (cables, power supply, box, manual), any flaws.]",
             "",
             "Ships within 2 business days, packed well and insured. Open to reasonable offers.",
             "Photos are of the actual unit. [Take your own photos before posting.]"]
    return {"title": title, "description": "\n".join(lines), "price": price}


# ---------------------------------------------------------------- tracker
def _num(v) -> Optional[float]:
    try:
        n = float(re.sub(r"[^\d.]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return None
    return n if n > 0 else None


def load() -> list[dict]:
    try:
        items = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [f for f in items if isinstance(f, dict) and f.get("id")] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PATH)


def bought(url, title, source, paid, expected=None) -> Optional[dict]:
    """Records "I bought this". None if the price is unusable; an unsold entry
    for the same link is returned as is (no double-tap duplicates)."""
    paid, expected = _num(paid), _num(expected)
    if not paid:
        return None
    items = load()
    for f in items:
        if f.get("url") and f["url"] == str(url or "")[:500] and f["status"] == "bought":
            return f
    f = {"id": uuid.uuid4().hex[:8], "at": datetime.now(timezone.utc).isoformat(),
         "url": str(url or "")[:500], "title": re.sub(r"\s+", " ", str(title or "")).strip()[:120],
         "source": str(source or "").split(" — ")[0][:60], "paid": paid, "expected": expected,
         "status": "bought", "sold_price": None, "sold_ship": None, "sold_at": None}
    items.append(f)
    _save(items)
    return f


def profit_of(f: dict) -> Optional[float]:
    """Realized profit: sold price after Reverb fees (resale_profit.NET_FACTOR),
    minus the shipping you paid, minus what you paid for it."""
    if f.get("status") != "sold" or f.get("sold_price") is None:
        return None
    from scrapers.resale_profit import NET_FACTOR
    return round(f["sold_price"] * NET_FACTOR - (f.get("sold_ship") or 0) - f["paid"], 2)


def sold(fid: str, price, ship=0) -> Optional[dict]:
    """Marks a bought flip as sold. Shipping may be 0; the sold price may not."""
    price = _num(price)
    ship = _num(ship) or 0
    if not price:
        return None
    items = load()
    for f in items:
        if f["id"] == fid:
            f.update(status="sold", sold_price=price, sold_ship=ship, sold_at=datetime.now(timezone.utc).isoformat())
            _save(items)
            return f
    return None


def remove(fid: str) -> bool:
    items = load()
    kept = [f for f in items if f["id"] != fid]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def summary(items: Optional[list[dict]] = None) -> dict:
    items = load() if items is None else items
    done = [f for f in items if f.get("status") == "sold" and f.get("sold_price") is not None]
    open_ = [f for f in items if f.get("status") == "bought"]
    for f in done:
        f["profit"] = profit_of(f)
    return {"n": len(items), "sold": len(done), "open": len(open_),
            "profit": round(sum(f["profit"] for f in done), 2),
            "invested": round(sum(f["paid"] for f in open_), 2),
            "open_items": sorted(open_, key=lambda f: f["at"], reverse=True),
            "sold_items": sorted(done, key=lambda f: f["sold_at"], reverse=True)}


def bought_urls() -> set:
    """Links already marked bought, so the suggestion list can show that."""
    return {f["url"] for f in load() if f.get("url")}
