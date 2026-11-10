"""The flip math. Pure functions: no database, no network, no Gear Scout
imports, so this file moves to a stand-alone Gear Flip unchanged.

For one listing it works out, per place you could resell it (Reverb, eBay or
local pickup on Facebook/Craigslist):
  buy cost      ask + their shipping + sales tax (online) or a pickup trip (local)
  resale        the used value, trimmed when it rests on asking prices
  fees          the selling site's cut
  shipping      by size: a mic box, a rack unit, a heavy console...
  repair        typical bench cost, with the odds the fix works
  net profit, ROI, ROI per month, days to sell, confidence, score
and the "walk-away" price: the most you can pay and still hit your minimum ROI.
All of it is an estimate; fee rates are the sites' published rates (2026)."""
import re
from typing import Optional

# Selling fees. pct of the sale price + a fixed amount per sale.
# price: resale vs the used value; days: how long it takes vs the typical time.
CHANNELS = {
    "reverb": {"label": "Reverb", "pct": 0.0819, "fixed": 0.49, "ships": True, "price": 1.00, "days": 1.0},
    # eBay Musical Instruments & Gear: 13.6% final value fee + $0.40 per order
    "ebay": {"label": "eBay", "pct": 0.136, "fixed": 0.40, "ships": True, "price": 0.98, "days": 0.8},
    # Facebook Marketplace / Craigslist local pickup: no fees, no box, lower price, a bit slower
    # (the quick-sale price already reflects local ads, so only a small extra cut)
    "local": {"label": "Local pickup (FB/Craigslist)", "pct": 0.0, "fixed": 0.0, "ships": False, "price": 0.95, "days": 1.3},
}
INSURE_PCT = 0.01           # shipping insurance, share of the sale price

# Shipping (box, packing, label) by what kind of thing it is.
SHIP = {"small": 28, "rack": 48, "medium": 70, "heavy": 160}
_HEAVY = re.compile(r"\bconsole\b|\bmixing desk\b|\b(?:24|32|48)[\s-]?(?:ch|channel)\b|\breel[\s-]?to[\s-]?reel\b|"
                    r"\btape (?:machine|recorder|deck)\b|\bmulti[\s-]?track\b|\bstuder\b|\bampex\b|\botari\b|"
                    r"\bpiano\b|\bhammond\b|\bleslie\b|\bsubwoofer\b|\bpa speakers?\b|\bcabinet\b|\b(?:4|2)x12\b|"
                    r"\bplate reverb\b|\bemt\s?140\b|\bhalf[\s-]?stack\b", re.I)
_MEDIUM = re.compile(r"\bmonitors?\b|\bspeakers?\b|\bmixer\b|\bsynth(?:esizer)?\b|\bkeyboard\b|\bdrum machine\b|"
                     r"\bsampler\b|\bmpc\b|\bamp(?:lifier)?\b|\bhead\b|\bcombo\b|\b[3-9]u\b|\bsummi?ng\b", re.I)
_RACK = re.compile(r"\bcompressor\b|\blimiter\b|\bpre[\s-]?amp\b|\bpreamp\b|\beq\b|\bequali[sz]er\b|\binterface\b|"
                   r"\bconverter\b|\bclock\b|\b[12]u\b|\brack\b|\bchannel strip\b|\b1176\b|\bla[\s-]?[23]a\b|"
                   r"\bdistressor\b|\bpultec\b|\bapollo\b|\breverb unit\b|\bdelay\b|\blexicon\b|\beventide\b", re.I)


def size_class(title: Optional[str]) -> str:
    t = title or ""
    if _HEAVY.search(t):
        return "heavy"
    if _MEDIUM.search(t):
        return "medium"
    if _RACK.search(t):
        return "rack"
    return "small"


# Repair kinds (labels match Gear Scout's fixer-upper wording):
# (typical bench cost $, chance the fix works, extra days before you can sell)
REPAIRS = {
    "capsule / re-skin": (350, 0.70, 21),
    "recap": (250, 0.85, 14),
    "tubes": (150, 0.92, 5),
    "power supply": (200, 0.80, 14),
    "general repair": (300, 0.60, 21),
    "check-over": (120, 0.80, 7),
}
DEFAULT_REPAIR = (300, 0.60, 21)
SALVAGE_PCT = 0.30          # a unit that can't be fixed still sells for parts at about this share of its value
DIAG_CAP = 100              # what finding out it can't be fixed costs, at most
NO_REPAIR_SUCCESS = 0.97    # even "working" gear sometimes isn't as described
DOWNSIDE_PRICE = 0.85       # worst realistic case for working gear: you sell it this far under value


def repair_terms(label: Optional[str]) -> tuple:
    """(cost, success, days) for a repair label; (0, 1, 0) for working gear."""
    if not label:
        return 0, 1.0, 0
    return REPAIRS.get(label, DEFAULT_REPAIR)


def days_fallback(resale: float) -> float:
    """Typical days to sell when Gear Scout has no sell-through data for the model."""
    for cap, d in ((300, 12), (800, 18), (2000, 25), (5000, 35)):
        if resale < cap:
            return d
    return 50


def sell_side(channel: str, resale: float, size: str) -> dict:
    """What one sale nets on one channel."""
    c = CHANNELS[channel]
    price = resale * c["price"]
    fees = price * c["pct"] + (c["fixed"] if price else 0)
    ship = (SHIP[size] + price * INSURE_PCT) if c["ships"] else 0.0
    return {"channel": channel, "label": c["label"], "price": price, "fees": fees, "ship": ship,
            "net": price - fees - ship}


def buy_cost(ask: float, ship_in: Optional[float], local: bool, tax_pct: float, pickup: float) -> dict:
    ship_in = ship_in if isinstance(ship_in, (int, float)) and ship_in > 0 else 0.0
    tax = 0.0 if local else (ask + ship_in) * tax_pct / 100
    trip = pickup if local else 0.0
    return {"ask": ask, "ship_in": ship_in, "tax": tax, "pickup": trip, "total": ask + ship_in + tax + trip}


def confidence(n: int, sold_basis: bool, success: float, ask: float, value: float,
               flags: int = 0, age_days: Optional[float] = None, speed_known: bool = False) -> int:
    """0-100: how much to trust the numbers. More comparable listings, real
    sales, a low-risk repair and a believable price all raise it."""
    c = 15 + min(n, 30) / 30 * 45 + (20 if sold_basis else 5)
    c -= (1 - success) * 60
    if value and ask < value * 0.30:
        c -= 15  # far under value: often not what it seems
    c -= 10 * flags
    if age_days is not None and age_days <= 3:
        c += 5
    if speed_known:
        c += 5
    return int(max(5, min(95, round(c))))


def score(expected: float, roi: float, days: float, conf: int, scale: float = 500) -> int:
    """0-100 ranking: profit, return on the money, speed, all scaled by confidence.
    `scale` is the profit that counts as "full marks" (half your budget), so a
    $500 bankroll ranks $100-$250 flips well instead of chasing big-ticket gear."""
    p = max(0.0, min(expected / max(scale, 50), 1.0))
    r = max(0.0, min(roi / 60, 1.0))
    v = max(0.0, min(14 / max(days, 1), 1.0))
    return int(round(100 * (0.45 * p + 0.30 * r + 0.25 * v) * (0.4 + 0.6 * conf / 100)))


def analyze(*, title: str, ask: float, value: float, n: int, sold_basis: bool = False,
            ship_in: Optional[float] = None, local: bool = False, repair: Optional[str] = None,
            days_base: Optional[float] = None, age_days: Optional[float] = None, flags: int = 0,
            settings: Optional[dict] = None, conf_adj: int = 0) -> Optional[dict]:
    """Every number for one listing, on the best place to resell it.
    None when the inputs are unusable."""
    s = settings or {}
    if not ask or ask <= 0 or not value or value <= 0:
        return None
    tax_pct = float(s.get("buy_tax_pct", 10.4))
    pickup = float(s.get("local_pickup_cost", 15))
    sell_on = s.get("sell_on", "best")
    size = size_class(title)
    buy = buy_cost(ask, ship_in, local, tax_pct, pickup)
    # `value` is already a quick-sale price (real sales, or the lower quarter of
    # asks), so it isn't discounted again here; only the channel's own price
    # factor applies (one discount, not two).
    resale = value
    rc, success, rdays = repair_terms(repair)
    diag = min(rc, DIAG_CAP) if repair else 0
    speed_known = days_base is not None
    base_days = days_base if speed_known else days_fallback(resale)
    channels = [sell_on] if sell_on in CHANNELS else list(CHANNELS)
    best = None
    for ch in channels:
        side = sell_side(ch, resale, size)
        days = base_days * CHANNELS[ch]["days"] + rdays
        if repair:
            fixed = side["net"] - buy["total"] - rc
            parts = sell_side(ch, value * SALVAGE_PCT, "small" if size != "heavy" else size)
            failed = max(parts["net"], 0) - buy["total"] - diag
            expected = success * fixed + (1 - success) * failed
            spend = success * rc + (1 - success) * diag
            downside = failed
            k = success * (side["net"] - rc) + (1 - success) * (max(parts["net"], 0) - diag)
        else:
            fixed = side["net"] - buy["total"]
            low = sell_side(ch, resale * DOWNSIDE_PRICE, size)
            expected = NO_REPAIR_SUCCESS * fixed + (1 - NO_REPAIR_SUCCESS) * (low["net"] - buy["total"])
            spend = 0.0
            downside = low["net"] - buy["total"]
            k = NO_REPAIR_SUCCESS * side["net"] + (1 - NO_REPAIR_SUCCESS) * low["net"]
        # Pick the channel with the most profit after a carrying cost of 1% a month on the money tied up
        carry = (buy["total"] + spend) * 0.01 * days / 30
        key = expected - carry
        if best is None or key > best[0]:
            best = (key, ch, side, days, fixed, expected, spend, downside, k)
    _, ch, side, days, fixed, expected, spend, downside, k = best
    cash_in = buy["total"] + spend
    roi = expected / cash_in * 100 if cash_in else 0.0
    conf = confidence(n, sold_basis, success * (NO_REPAIR_SUCCESS if not repair else 1), ask, value, flags,
                      age_days, speed_known)
    conf = int(max(5, min(95, conf + conf_adj)))  # e.g. what the photo check saw
    # Walk-away: the most you could pay and still hit the minimum ROI and profit.
    r = float(s.get("min_roi", 25)) / 100
    buy_max = min((k - r * spend) / (1 + r), k - float(s.get("min_profit", 40)))
    walk = (buy_max - buy["pickup"]) / (1 + (0 if local else tax_pct / 100)) - buy["ship_in"]
    return {
        "size": size, "channel": ch, "channel_label": side["label"],
        "ask": round(ask), "buy_ship": round(buy["ship_in"]), "tax": round(buy["tax"]), "pickup": round(buy["pickup"]),
        "buy_total": round(buy["total"]), "value": round(value), "resale": round(side["price"]),
        "fees": round(side["fees"]), "ship_out": round(side["ship"]),
        "repair": repair, "repair_cost": rc, "repair_odds": round(success * 100) if repair else None,
        "cash_in": round(cash_in), "cash_need": round(buy["total"] + rc), "profit_if_ok": round(fixed), "expected": round(expected),
        "downside": round(downside), "roi": round(roi, 1), "days": round(days),
        "roi_month": round(roi * 30 / max(days, 1), 1), "margin": round(expected / side["price"] * 100, 1) if side["price"] else 0,
        "confidence": conf, "score": score(expected, roi, days, conf, max(100.0, float(s.get("budget", 1000)) * 0.5)), "comps": n, "sold_basis": sold_basis,
        "speed_known": speed_known, "walk_away": max(0, round(walk)),
    }


def passes(m: dict, settings: dict) -> bool:
    """Does an analyzed listing clear your minimums?"""
    return (m["expected"] >= float(settings.get("min_profit", 40)) and m["roi"] >= float(settings.get("min_roi", 25))
            and m["confidence"] >= float(settings.get("min_confidence", 35)) and m["days"] <= float(settings.get("max_days", 75)))
