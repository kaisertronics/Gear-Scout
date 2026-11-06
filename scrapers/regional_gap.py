"""Regional price gap: the same mic model asking much less in another state.

Region = US state, read from a listing's "City, ST" location (Facebook
Marketplace, OfferUp, Craigslist with a state). Sources without a state in the
location (Reverb, eBay, bare Craigslist towns, Kijiji) have no reliable region
and are left out. For each model (enrich.model_key) with MIN_N+ live ads in a
state, the median asking price is compared with the median in the owner's home
state (from home_zip); a state GAP_PCT or more under it is flagged, with its
cheapest live ads, so a road trip or shipping may be worth it. When the home
state has too few ads of a model, the median of all located US ads is the
yardstick instead.

Same ad filters as the market pulse (hidden, duplicate, auction, Canadian,
sold, pending and accessory-priced ads left out). Reads the local DB only; no
network calls.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from scrapers.market_pulse import MIN_ASK, MIN_N, _ts

logger = logging.getLogger(__name__)

GAP_PCT = 25       # a region this far under the home median is flagged
WINDOW_DAYS = 90   # only ads posted / first seen this recently
SHOW_ADS = 3       # cheapest live ads listed per flagged model
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "out": None}
_STATE = re.compile(r",\s*([A-Z]{2})\s*(?:\d{5}(?:-\d{4})?)?$")
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def region(location: Optional[str]) -> Optional[str]:
    """"Houston, TX" -> "TX"; None unless the location ends in a US state."""
    m = _STATE.search((location or "").strip())
    return m.group(1) if m and m.group(1) in STATES else None


def gaps(items: list, home: Optional[str]) -> list[dict]:
    """items = [{price, region, ...}] for one model -> flagged regions, biggest
    gap first: [{region, median, low, base, base_label, pct, n, ads}]."""
    by: dict = {}
    for it in items:
        by.setdefault(it["region"], []).append(it)
    home_prices = [i["price"] for i in by.get(home, [])]
    if len(home_prices) >= MIN_N:
        base, label = median(home_prices), home
    else:
        base, label = median(i["price"] for i in items), "all regions"
    out = []
    for reg, ads in by.items():
        if reg == home or len(ads) < MIN_N or not base:
            continue
        m = median(a["price"] for a in ads)
        pct = round((base - m) / base * 100)
        if pct >= GAP_PCT:
            ads = sorted(ads, key=lambda a: a["price"])
            out.append({"region": reg, "median": round(m), "low": round(ads[0]["price"]), "base": round(base),
                        "base_label": label, "pct": pct, "n": len(ads), "ads": ads[:SHOW_ADS]})
    return sorted(out, key=lambda g: -g["pct"])


def build(cfg: dict, now: Optional[datetime] = None) -> list[dict]:
    """[{label, home, gaps:[...]}] for models with a flagged region, biggest
    gap first. Empty without a home ZIP."""
    from scrapers import mic_focus
    from scrapers.enrich import model_key, parse_price
    from scrapers.geo import zip_state
    from scrapers.store import _conn
    home = zip_state(str(cfg.get("home_zip") or ""))
    if not home:
        return []
    now = now or datetime.now(timezone.utc)
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    floors = {t["key"]: max(t.get("min_price") or 0, MIN_ASK) for t in targets}
    cut = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    per: dict = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, price, url, location, description FROM seen"
            " WHERE hidden = 0 AND duplicate = 0 AND sold = 0 AND pending = 0 AND price IS NOT NULL AND price NOT LIKE 'C$%'"
            " AND location IS NOT NULL AND location != '' AND (country IS NULL OR country = 'US')"
            " AND COALESCE(NULLIF(posted_at, ''), first_seen) >= ?", (cut,)).fetchall()
    for title, price, url, loc, desc in rows:
        if (desc or "").startswith("Auction"):
            continue  # a current bid isn't an asking price
        t = mic_focus.classify(title or "", targets)
        reg, v = region(loc), parse_price(price)
        mk = (model_key(title) or "") if t else ""
        if not mk or not reg or not v or v < floors[t["key"]]:
            continue
        per.setdefault(mk, []).append({"price": v, "region": reg, "title": title, "url": url, "location": loc})
    out = []
    for mk, items in per.items():
        g = gaps(items, home)
        if g:
            out.append({"label": mk.split("|")[0].split(":")[-1].upper(), "home": home, "gaps": g})
    return sorted(out, key=lambda m: -m["gaps"][0]["pct"])


def regional_gaps(cfg: dict) -> list[dict]:
    """build(), cached for 30 minutes; never raises ([] on trouble)."""
    if _cache["out"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["out"]
    try:
        out = build(cfg)
    except Exception:
        logger.exception("Regional price gap failed")
        return []
    _cache["out"], _cache["at"] = out, time.time()
    return out
