"""
Mic Focus: Gear Scout narrowed to the mics the owner is hunting right now
(Oct 22). While it's on, the 15-minute sweep searches only these mics, every
other alert is paused, and the front page is the Mic Focus page.

Each target says which listings count (brand / model / tube) and how far
under price one has to be to push to the phone. The yardstick is today's
NEW price (Reverb Brand New + eBay New, median) when the mic is still made,
else the used-market price; "% off" is figured on the delivered price
(listing + listed shipping). Pushes and the page's list go by price now: every listing under $1,000
delivered and at least $300 (owner's call, Oct 22); C12-style under $375, AKG D12 under $500. The % off is shown alongside.

US listings only (tariffs on Canadian ones). Parts, capsules, power
supplies, repair jobs and auctions are left out.
"""
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT = Path("/data/mic_focus.json")
LIVE_DAYS = 90          # the page lists everything still for sale seen within this many days (owner, Oct 22)
FRESH_DAYS = 10         # pushes only for listings a site still showed within this many days
NEW_HOURS = 6           # "new" for alerts: first found within this many hours
ALERT_MAX_SINGLE = 8    # more than this in one pass → one summary push
MIN_PRICE = 40.0        # under this it's a placeholder or a part
MIN_PUSH_PRICE = 300    # owner's floor (Oct 22): nothing under $300 delivered pushes or makes the list

_TUBE = r"\btube\b|\bvalve\b|\bvacuum\b"
_FET = re.compile(r"\bfet\b|\bjr\.?\b|\bjunior\b|\bsolid[- ]state\b|\bwa-?47 ?f\b|\b47 ?fet\b|\bu-?47 ?fet\b", re.I)
_NOT_MIC = re.compile(
    r"\b(?:pre-?amps?|preamplifiers?|compressors?|limiters?|equali[sz]ers?|eq|channel ?strips?|500[\s-]?series|"
    r"lunchbox|di box|interface|headphones?|monitors?|speakers?|guitar|pedal|plugin|plug-in|software|t-?shirt|"
    r"hat|sticker|poster|book|magazine|diecast|die-cast|figures?|toys?|chogokin|popy|robot|lego|model kit|tuner|"
    r"headsets?|aviation|intercom|pilot)\b", re.I)
_PART = re.compile(
    r"\b(?:capsules?(?! (?:included|incl))|power suppl(?:y|ies)|psu|cables? only|cable for|shock ?mounts?|"
    r"spider|mount only|clip|case only|flight case|box only|grille|head ?basket|basket|body only|"
    r"(?:transformer|tube|valve|pcb|board|kit|parts?|socket)s?(?: only| for)|for parts|parts only|replacement|"
    r"windscreen|pop filter|foam|manual|cradle|holder|adapter|repair|not working|as[- ]is|broken|attenuators?|"
    r"(?:\d+\s?db\s)?pads?)\b", re.I)

# key, name, min_pct, brand/model regex, extra requirement, exclusions,
# sweep queries, model-code regex, brand name used for price lookups
TARGETS = [
    dict(key="peluso", name="Peluso", min_pct=60, max_price=1000,
         match=r"\bpeluso\b", require=None,
         # capsules (CK-2, CK-21, CEK 12…), the PADB direct box, stereo-matching / power-supply parts
         exclude=r"\bck-?\d|\bp-?k\d|\bcek\b|\bpadb\b|direct box|stereo matching|\bmt-?\d|\bps-?\d",
         queries=["peluso", "peluso microphone", "peluso 22 47", "peluso p-12"],
         model=r"\b(22\s?-?47\s?(?:le|se)?|22\s?-?251|2247\s?(?:le|se)?|22251|p-?\d{2,3}[a-z]{0,2}|cemc-?\d|r-?\d{2})\b",
         brand="Peluso"),
    dict(key="golden_age", name="Golden Age tube mics", min_pct=60, max_price=1000,
         match=r"golden\s*age|\bga[\s-]?(?:47|251|12|8000)\b",
         require=_TUBE + r"|\bga[\s-]?(?:47|251|12|8000)\b|ela ?-?m ?-?251|\br-?1 ?tube\b",
         exclude=r"\b(?:pre[\s-]?73|pre[\s-]?ns|pre[\s-]?573|comp[\s-]?54|comp[\s-]?2a|eq[\s-]?73|eq[\s-]?81|ga[\s-]?73|ns[\s-]?73|d[\s-]?2)\b|\bfet\b|ribbon(?! ?tube)|\br-?1 ?a\b",
         queries=["golden age premier ga-47", "golden age premier ga-251", "golden age r1 tube", "golden age tube mic"],
         model=r"\b(ga[\s-]?47\s?(?:mk\s?ii|mkii|mk2)?|ga[\s-]?251|ga[\s-]?12|ga[\s-]?8000|r-?1\s?tube)\b",
         brand="Golden Age Premier"),
    dict(key="stam", name="Stam tube mics", min_pct=60, max_price=1000,
         match=r"\bstam\b",
         require=_TUBE + r"|\bsa[\s-]?(?:47|251|12|67|800|87t)\b",
         exclude=r"\bsa-?(?:73|4000|2a|76|n73|80|500|1176|3a|cl|81|n?2)\b|\bfet\b|\bsa-?87\b",
         queries=["stam sa-47", "stam audio mic", "stam tube microphone"],
         model=r"\b(sa[\s-]?\d{2,3}t?\s?(?:mk\s?(?:iii|ii|3|2)|mkiii|mkii)?)\b",
         brand="Stam Audio"),
    # Owner (Oct 23): Warm Audio over $500 isn't shown anywhere, not just not pushed.
    dict(key="warm", name="Warm Audio tube mics", min_pct=60, max_price=500, hide_over_limit=True,
         match=r"warm\s*audio|\bwa[\s-]?(?:47|251|8000|cx-?12|67)\b",
         require=r"\bwa[\s-]?(?:47|251|8000|cx-?12|67)\b|warm\s*audio.{0,30}(?:" + _TUBE + r")",
         exclude=r"\bwa-?47 ?jr\b|\bwa-?47 ?f\b|\bwa-?(?:12|73|76|2a|412|273|2mpx|1b|84|87|14|ma)\b",
         queries=["warm audio wa-47", "warm audio wa-251", "warm audio wa-8000", "warm audio wa-cx12", "warm audio wa-67"],
         model=r"\b(wa[\s-]?47|wa[\s-]?251|wa[\s-]?8000|wa[\s-]?cx-?12|wa[\s-]?67)\b",
         brand="Warm Audio"),
    dict(key="lauten", name="Lauten Audio", min_pct=60, max_price=1000,
         match=r"\blauten\b", require=None, exclude=None,
         queries=["lauten audio", "lauten atlantis", "lauten eden", "lauten oceanus"],
         model=r"\b(atlantis|eden|oceanus|clarion|horizon|torch|synergy|la-?\d{3}|ft-?\d{3}|ls-?\d{3})\b",
         brand="Lauten Audio"),
    dict(key="bees_neez", name="Bees Neez tube mics", min_pct=60, max_price=1000,
         match=r"bees?[\s'-]?neez|beezneez|bee'?s knees mic",
         require=None, exclude=r"\bfet\b",
         queries=["bees neez microphone", "beesneez", "beezneez"],
         model=r"\b(lulu|arya|jumbo|vivian|ursa|mona|honey|queen|cleo|ada|willa|tiara|dolly|ruby|ella)\b",
         brand="Bees Neez"),
    dict(key="advanced_audio", name="Advanced Audio", min_pct=60, max_price=1000,
         match=r"advanced\s*audio|\bcm[\s-]?(?:47|12|251|49|269|800|67|87|414)\w*\b(?=.*\b(?:mic|tube|condenser))",
         require=r"\bmic(?:rophone)?s?\b|condenser|\btube\b|\bcm[\s-]?\d",
         exclude=r"\bcar\b|amplifier|speaker|subwoofer|stereo receiver",
         queries=["advanced audio microphone"],
         model=r"\b(cm[\s-]?\d{2,3}[a-z]{0,3})\b", brand="Advanced Audio"),
    dict(key="united", name="United Studio Technologies", min_pct=60, max_price=1000,
         match=r"united\s*studio\s*tech|\but[\s-]?(?:twin[\s-]?\d{2}|fet[\s-]?47|tube[\s-]?67|vintage[\s-]?87|47|251|12|67)\b",
         require=r"united\s*studio|\bmic(?:rophone)?s?\b|condenser",
         exclude=r"la-?2a|1176|turntable|record player|teletronix",
         queries=["united microphone", "united studio technologies microphone"],
         model=r"\b(ut[\s-]?(?:twin[\s-]?\d{2}|fet[\s-]?47|tube[\s-]?67|vintage[\s-]?87|\d{2,3})|twin[\s-]?\d{2}|vintage[\s-]?87)\b",
         brand="United Studio Technologies"),
    dict(key="signal_art", name="Signal Art", min_pct=60, max_price=1000,
         match=r"signal\s*arts?\b",
         require=r"\bmic(?:rophone)?s?\b|condenser|ribbon|\btube\b|\bpreamp\b(?!.*painted)",
         exclude=r"painted|wooden|wear|shirt|crew neck|hoodie|sign\b|canvas|print",
         queries=["signal arts microphone", "signal art microphone"],
         model=None, brand=None),
    dict(key="dachman", name="Dachman Audio", min_pct=60, max_price=1000,
         # "DA" model codes are Dachman's too ("DA 87SE", "DA-87i", "DA FET 47") — owner, Oct 22
         match=r"\bdachman\b|\bda[\s-]?(?:fet[\s-]?)?(?:87|67|47|251|12|57|414|17|20)[a-z]{0,3}\b",
         require=r"\bdachman\b|\bmic(?:rophone)?s?\b|condenser|\btube\b|\bfet\b|\b(?:87|47|67)\s?se\b|\b(?:87|67|47)i\b",
         exclude=r"^(?!.*dachman)(?=.*\b(?:aea|akg|neumann|rode|shure|sennheiser|telefunken|audio-technica|behringer|"
                 r"mxl|warm audio|golden age|stam|peluso|lauten|united|soyuz|mojave|avantone|blue|cad|sony|yamaha|roland|"
                 r"boss|korg|digidesign|avid|apogee|focusrite|presonus|motu)\b)",
         queries=["dachman microphone", "da 87se"],
         model=r"\b(da[\s-]?\d{2,3}[a-z]{0,3}|\d{2,3}se)\b", brand="Dachman Audio"),
    dict(key="d12", name="AKG D12", min_pct=60, max_price=500,
         match=r"(?<![\w-])d-?12\b(?![-\s]?\d)",
         require=r"\bakg\b",
         exclude=r"\bd-?112\b|\bd-?12\d\b|\btype\b|\bstyle\b|\bclone\b|nikon|canon|camera|lens|drone|dji|truck|hydraulic|\bc-?12\b",
         queries=["akg d12", "akg d12 vr", "akg d12e"],
         model=r"\b(d-?12 ?vr|d-?12 ?e|d-?12)\b",
         brand="AKG"),
    dict(key="c12", name="C12-style tube mics", min_pct=60, max_price=375,
         match=r"\bc-?12\b|\bcv-?12\b",
         require=_TUBE + r"|\bc-?12\b",
         exclude=r"\bc-?12 ?a\b|\bfet\b|\bd-?12\b|\bbehringer\b|\bmxl\b|socket",
         queries=["c12 tube mic", "c12 tube microphone", "c12 clone"],
         model=None, brand=None),
    dict(key="classic_tube", name="U47 / ELA M 251-style tube mics", min_pct=60, max_price=1000,
         match=r"\bu-?\s?47\b|\bela ?-?m ?-?251\b|\b251\b",
         require=_TUBE + r"|\bu-?\s?47\b|\bela ?-?m ?-?251\b",
         exclude=r"\bc-?12 ?a\b|\bc-?12 ?vr\b(?=.*\bfet\b)|\bfet\b|\bd-?12\b|\bnt-?\d|\bb-?\d{2,3}\b(?=.*behringer)|"
                 r"\bbehringer\b|\bmxl\b|socket|\b251(?:st|nd)\b",
         queries=["u47 tube microphone", "ela m 251", "251 tube microphone", "u47 tube"],
         model=None, brand=None),
    # Catch-all (owner, Oct 22): any mic, any brand or none, that's sold by its
    # model number alone — "47 tube mic", "U87 clone", "67 condenser", "C12 style".
    dict(key="generic", name="Any 47 / 67 / 87 / 251 / C12 mic", min_pct=60, max_price=1000,
         match=r"(?<![\d.$,])(?:\b|(?<=[a-z]))(?:u|m|k|ela ?m ?|tlm ?)?[\s-]?(?:47|67|87|251)(?:\s?(?:ai|i|fet|tube|se|le|mk\s?i{1,3}))?\b(?![\d.,]|\s?(?:mm|cm|in\b|inch|ft|lbs?|watt|w\b|hp|mph|cc|v\b|year|yr))|\bc[\s-]?12\b",
         require=r"\bmic(?:rophone)?s?\b|condenser|\btube\b|\bvalve\b|\bfet\b|\bclone\b|\bstyle\b|large[\s-]?diaphragm|\bldc\b",
         # brands with their own section stay there (a WA-87 FET isn't a "Warm tube mic", and isn't a catch-all one either)
         exclude=r"warm\s*audio|\bwa[\s-]?\d|peluso|golden\s*age|\bstam\b|lauten|neez|advanced\s*audio|united\s*studio|dachman|signal\s*art|"
                 r"handheld|hand-held|\bdynamic\b|bluetooth|earbuds?|wireless|karaoke|lavalier|gooseneck|"
                 r"\b(?:19|20)\d\d\b(?=.*\b(?:car|truck|chevy|ford|corvette|mustang|jeep|honda|toyota)\b)|\bshure\s?sm\b|\bsm[\s-]?57\b|\bbeta\b|"
                 r"\bglock\b|\bcaliber\b|\bammo\b|\brifle\b|\bak-?47\b|\bpreamp\b|\binterface\b|\bsocket\b",
         queries=["47 microphone", "67 microphone", "87 microphone", "251 microphone", "c12 microphone"],
         model=None, brand=None),
]


_BRANDS = re.compile(r"\b(aea|akg|avantone|lauten|royer|sennheiser|shure|neumann|peluso|warm audio|golden age|stam|"
                     r"beesneez|bees neez|telefunken|rode|audio-technica|mojave|blue|electro-voice|ev|beyerdynamic|"
                     r"coles|cad|mxl|soyuz|wunder|chandler|manley|ua|universal audio|josephson|schoeps|sony)\b", re.I)


def settings(cfg: dict) -> dict:
    """{enabled, targets: [target with min_pct/on from config]}"""
    mf = cfg.get("mic_focus") or {}
    over = mf.get("targets") or {}
    out = []
    for t in TARGETS:
        o = over.get(t["key"]) or {}
        t = dict(t)
        try:
            t["min_pct"] = max(0, min(95, int(o.get("min_pct", t["min_pct"]))))
        except (TypeError, ValueError):
            pass
        try:
            t["max_price"] = max(0, int(o.get("max_price", t["max_price"])))
        except (TypeError, ValueError):
            pass
        t["on"] = bool(o.get("on", True))
        out.append(t)
    try:
        floor = max(0, int(mf.get("min_price", MIN_PUSH_PRICE)))
    except (TypeError, ValueError):
        floor = MIN_PUSH_PRICE
    return {"enabled": bool(mf.get("enabled")), "targets": out, "min_price": floor}


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("mic_focus") or {}).get("enabled"))


def sweep_terms(cfg: dict) -> list[str]:
    """What the 15-minute sweep searches while Mic Focus is on."""
    out = []
    for t in settings(cfg)["targets"]:
        if t["on"]:
            out += [q for q in t["queries"] if q not in out]
    return out


# Facebook gets one broad search per brand (its search is fuzzy, and each
# term costs ~1 minute of the account's time), so every brand comes round
# every ~2 hours instead of every 5–14 with all ~45 site searches (Oct 23).
FB_TERMS = {
    "peluso": ["peluso microphone"], "golden_age": ["golden age microphone"], "stam": ["stam microphone"],
    "warm": ["warm audio microphone"], "lauten": ["lauten microphone"], "bees_neez": ["bees neez microphone"],
    "advanced_audio": ["advanced audio microphone"], "united": ["united microphone"],
    "signal_art": ["signal art microphone"], "dachman": ["dachman microphone"], "d12": ["akg d12"],
    "c12": ["c12 microphone"], "classic_tube": ["u47 microphone", "251 microphone"],
    "generic": ["47 microphone", "67 microphone", "87 microphone"],
}


def fb_terms(cfg: dict) -> list[str]:
    out = []
    for t in settings(cfg)["targets"]:
        if t["on"]:
            out += [q for q in FB_TERMS.get(t["key"], t["queries"][:1]) if q not in out]
    return out


def classify(title: str, cfg_targets: Optional[list] = None) -> Optional[dict]:
    """The first target a listing title belongs to, or None."""
    if not title or _NOT_MIC.search(title) or _PART.search(title):
        return None
    from scrapers.wanted import is_spare_tube
    if is_spare_tube(title) or (re.search(r"6au6|ef ?94|6072|12ay7|ef ?86|6sj7|vf ?14", title, re.I)
                                and re.search(r"nos|new old stock|^\s*\d+\s?x\b|\bpairs?\b|matched", title, re.I)):
        return None  # a spare tube for one of these mics
    # A "studio microphones: AEA, AKG, Avantone, Lauten, Royer…" lot isn't one mic.
    if len(set(m.lower() for m in _BRANDS.findall(title))) >= 3:
        return None
    for t in cfg_targets or TARGETS:
        if not re.search(t["match"], title, re.I):
            continue
        if t.get("require") and not re.search(t["require"], title, re.I):
            continue
        if t.get("exclude") and re.search(t["exclude"], title, re.I):
            continue
        if t["key"] in ("warm", "c12", "classic_tube", "golden_age", "stam", "bees_neez") and _FET.search(title):
            continue
        return t
    return None


def _model_query(t: dict, title: str) -> Optional[str]:
    if not t.get("model") or not t.get("brand"):
        return None
    m = re.search(t["model"], title, re.I)
    if not m:
        return None
    model = re.sub(r"\s+", " ", m.group(1)).strip().upper()
    if t["key"] == "lauten" and not re.match(r"[A-Z]{2}-?\d", model):
        model = model.title()
    return f"{t['brand']} {model}"


_price_cache: dict = {}


def _model_prices(q: str) -> dict:
    """{new, used, new_basis, used_basis} for a model, via the Saved page's
    lookup (cached for a month there; for an hour here)."""
    hit = _price_cache.get(q)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        from scrapers.savings import prices
        p = prices(q)
    except Exception:
        logger.exception("Mic Focus price lookup failed for %r", q)
        p = {}
    _price_cache[q] = (time.time(), p)
    return p


def _candidates() -> list[dict]:
    from scrapers.store import _conn
    likes = ["da 87", "da-87", "da87", "da 67", "da-67", "da67", "da 47", "da-47", "da47", "da fet", "advanced audio", "cm47", "cm-47", "cm12", "united studio", "ut twin", "ut fet", "ut tube", "signal art", "dachman", "peluso", "golden age", "ga-47", "ga47", "ga-251", "ga251", "stam", "warm audio", "wa-", "wa47", "wa251",
             "wa8000", "wacx", "lauten", "neez", "d12", "d-12", "c12", "c-12", "47", "251", "67", "87"]
    cut = (datetime.now(timezone.utc) - timedelta(days=LIVE_DAYS)).isoformat()
    sql = ("SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
           " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
           " AND url NOT LIKE '%search/posts%' AND (country IS NULL OR country = 'US')"
           " AND COALESCE(last_seen, first_seen) >= ? AND (" + " OR ".join("LOWER(title) LIKE ?" for _ in likes) + ")")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, [cut] + [f"%{w}%" for w in likes])]


def find(cfg: dict) -> list[dict]:
    """Every live US listing that belongs to a Mic Focus target, with
    r['mic'] = {target, name, min_pct, pct, ref, ref_kind, new, used,
    delivered, hit, model}. Best discount first."""
    from scrapers.comps import evaluate, price_index, similar_index, similar_price
    from scrapers.enrich import exclude_match, is_accessory_only, is_partial, needs_repair, parse_price, price_basis
    from scrapers.market import load_market
    st = settings(cfg)
    targets = [t for t in st["targets"] if t["on"]]
    floor = st["min_price"]
    fresh_cut = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).isoformat()
    exclude_words = cfg.get("exclude_words") or []
    market = index = similar = None
    out, seen_ads = [], set()
    for r in _candidates():
        title = r.get("title") or ""
        t = classify(title, targets)
        if not t:
            continue
        if (r.get("description") or "").startswith("Auction"):
            continue
        src = (r.get("source_name") or "").lower()
        if "kijiji" in src or "mcquade" in src or "C$" in str(r.get("price") or ""):
            continue  # Canadian: tariffs
        if exclude_match(title, exclude_words) or is_accessory_only(title) or is_partial(title) \
                or needs_repair(title, r.get("description")):
            continue
        value = parse_price(r.get("price"))
        if not value or value < MIN_PRICE:
            continue
        unit = price_basis(title, None, value)[2] or value
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        delivered = round(unit + (ship or 0), 2)
        if t.get("hide_over_limit") and delivered > t["max_price"]:
            continue
        ad = (title.lower().strip(), round(value))
        if ad in seen_ads:
            continue
        seen_ads.add(ad)
        q = _model_query(t, title)
        new = used = None
        basis = ""
        if q:
            p = _model_prices(q)
            new, used = p.get("new"), p.get("used")
            if t["key"] == "d12" and "VR" not in q.upper():
                new = None  # the original D12 / D12E isn't made: only the D12 VR has a new price
            # a "new" price under the used one is an accessory or a clone that slipped through
            if new and used and new < used * 0.8:
                new = None
            basis = q
        if not new and not used:
            if market is None:
                market, index, similar = load_market(), price_index(), similar_index()
            c = evaluate(title, r.get("price"), r.get("description"), r["url"], market, index, similar)
            if c and not c.get("est"):
                peer = similar_price(similar, title, r["url"])
                used = min(c["ref"], peer) if peer else c["ref"]
                basis = c["label"]
        ref = new or used
        ref_kind = "new" if new else ("used" if used else None)
        pct = round((1 - delivered / ref) * 100) if ref else None
        hit = floor <= delivered <= t["max_price"]  # by price (Oct 22), not % off
        r["mic"] = {"target": t["key"], "name": t["name"], "min_pct": t["min_pct"], "max_price": t["max_price"], "pct": pct,
                    "ref": round(ref) if ref else None, "ref_kind": ref_kind, "new": round(new) if new else None,
                    "used": round(used) if used else None, "delivered": delivered, "hit": hit,
                    "model": q, "basis": basis, "check": bool(pct is not None and pct >= 85),
                    "fresh": (r.get("last_seen") or "") >= fresh_cut}
        out.append(r)
    out.sort(key=lambda r: r["mic"]["delivered"])  # cheapest first
    return out


def build(cfg: dict) -> dict:
    """Recomputes the list and saves it for the page."""
    rows = find(cfg)
    keep = ("title", "price", "url", "image_url", "source_name", "location", "shipping", "first_seen", "last_seen",
            "posted_at", "global_id", "description", "previous_price")
    try:
        auc = auctions(cfg)
    except Exception:
        logger.exception("Mic auction list failed")
        auc = []
    data = {"built": datetime.now(timezone.utc).isoformat(),
            "rows": [{k: r.get(k) for k in keep} | {"mic": r["mic"]} for r in rows],
            "auctions": [{k: r.get(k) for k in keep} | {"mic": r["mic"]} for r in auc]}
    try:
        SNAPSHOT.write_text(json.dumps(data))
    except OSError:
        logger.exception("Couldn't save the Mic Focus list")
    # The hourly sold check looks at these first (every 4 hours each): your
    # price range first, then everything else on the page, so an older ad
    # that sold drops off instead of lingering.
    try:
        order = [r["url"] for r in rows if r["mic"]["hit"]] + [r["url"] for r in rows if not r["mic"]["hit"]]
        Path("/data/shown_mics.txt").write_text("\n".join(dict.fromkeys(order)))
    except OSError:
        pass
    return data


def load() -> dict:
    try:
        return json.loads(SNAPSHOT.read_text())
    except Exception:
        return {"built": None, "rows": []}


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS mic_alerted (url TEXT, price TEXT, at TEXT, PRIMARY KEY (url, price))")


def _line(r: dict) -> str:
    m = r["mic"]
    ref = (f"new ~${m['new']:,}" if m["new"] else "") + (" / " if m["new"] and m["used"] else "") + \
          (f"used ~${m['used']:,}" if m["used"] else "")
    off = f"{m['pct']}% off {m['ref_kind']}" if m["pct"] is not None else "no price to compare yet"
    where = (r.get("location") or "").split(" @")[0]
    return f"${m['delivered']:,.0f} delivered · {off}" + (f" ({ref})" if ref else "") + \
           f" · {r.get('source_name') or ''}" + (f" · {where}" if where else "")


def alert(cfg: dict) -> int:
    """Rebuilds the list, then pushes each new hit once per price. The very
    first run sends one summary push instead of the whole backlog."""
    from scrapers.notify import push_enabled, send_push
    from scrapers.store import _conn
    data = build(cfg)
    hits = [r for r in data["rows"] if r["mic"]["hit"] and r["mic"].get("fresh", True)]
    with _conn() as conn:
        _ensure(conn)
        done = {(u, p) for u, p in conn.execute("SELECT url, price FROM mic_alerted")}
        first = not conn.execute("SELECT 1 FROM mic_alerted LIMIT 1").fetchone()
    todo = [r for r in hits if (r["url"], str(r.get("price"))) not in done]
    if not todo:
        return 0
    dash = ((cfg.get("email") or {}).get("dashboard_url") or "").rstrip("/")
    sent = 0
    if push_enabled(cfg):
        if first or len(todo) > ALERT_MAX_SINGLE:
            lines = "\n".join(f"• {r['mic']['name']}: {r['title'][:60]} — {_line(r)}" for r in todo[:10])
            send_push(cfg, f"🎤 Mic Focus: {len(todo)} mic{'s' if len(todo) != 1 else ''} worth a look",
                      lines, url=(dash + "/mics") if dash else None, tags=["microphone"])
            sent = 1
        else:
            for r in todo:
                m = r["mic"]
                head = f"${m['delivered']:,.0f}" + (f" ({m['pct']}% off {m['ref_kind']})" if m["pct"] and m["pct"] > 0 else "")
                chk = " (double-check)" if m["check"] else ""
                send_push(cfg, f"🎤 {m['name']}: {head}{chk}", f"{r['title'][:100]}\n{_line(r)}",
                          url=r["url"], tags=["microphone"], image=r.get("image_url"))
                sent += 1
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _ensure(conn)
        conn.executemany("INSERT OR IGNORE INTO mic_alerted VALUES (?, ?, ?)",
                         [(r["url"], str(r.get("price")), now) for r in todo])
        conn.commit()
    logger.info("Mic Focus: %d hits, %d new, %d pushes", len(hits), len(todo), sent)
    return sent


# --------------------------------------------------------------------------
# eBay auctions for your mics (owner, Oct 23): a lot of under-$1,000 tube
# mics sell at auction. Each mic's broad search runs against eBay auctions
# once an hour (17 API calls), stored like other listings (price = current
# bid, description "Auction · current bid (N bids) · ends …" Pacific).
# Shown on the Mics page by end time; a push about an hour before one ends
# while bid + shipping is still under that mic's limit — the bid is
# re-checked with eBay right before the push.

AUCTION_MARK = Path("/data/mic_auctions_fetched.txt")
AUCTION_EVERY_MIN = 60
AUCTION_DAYS = 3          # shown when ending within this many days
AUCTION_ALERT_MIN = 75    # push when ending within this many minutes


def _auction_due() -> bool:
    try:
        last = datetime.fromisoformat(AUCTION_MARK.read_text().strip())
        return datetime.now(timezone.utc) - last >= timedelta(minutes=AUCTION_EVERY_MIN - 5)
    except Exception:
        return True


def fetch_auctions(cfg: dict) -> int:
    """Searches eBay auctions for each mic (hourly) and stores the matches."""
    if not _auction_due():
        return 0
    import requests
    from scrapers.auctions import ebay_auction_listing
    from scrapers.ebay_api import (CONDITION_FILTER, SEARCH_URL, _check_response, _get_token, _headers,
                                   country_of, ebay_paused_until, shipping_of)
    from scrapers.store import _conn, mark_seen
    api = cfg.get("ebay_api") or {}
    if not (api.get("client_id") and api.get("client_secret")) or ebay_paused_until():
        return 0
    targets = [t for t in settings(cfg)["targets"] if t["on"]]
    token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
    stored = 0
    for q in fb_terms(cfg):
        params = {"q": q, "sort": "endingSoonest", "limit": "100",
                  "filter": f"{CONDITION_FILTER},buyingOptions:{{AUCTION}},priceCurrency:USD,itemLocationCountry:US"}
        try:
            resp = requests.get(SEARCH_URL, params=params, headers=_headers(token), timeout=30)
            _check_response(resp)
        except Exception as e:
            logger.warning("Mic auction search failed for %r: %s", q, e)
            break
        if resp.status_code != 200:
            continue
        for item in resp.json().get("itemSummaries", []) or []:
            listing = ebay_auction_listing(item, "eBay — Auctions")
            if not listing or not classify(listing.title, targets):
                continue
            listing.url = listing.url.split("?")[0]  # eBay adds per-search tracking to the link
            listing.shipping = shipping_of(item)
            listing.country = country_of(item)
            mark_seen(listing)
            with _conn() as conn:  # bids move: keep the stored bid current
                conn.execute("UPDATE seen SET price = ?, description = ?, last_seen = ? WHERE url = ?",
                             (listing.price, listing.description, datetime.now(timezone.utc).isoformat(), listing.url))
                conn.commit()
            stored += 1
        time.sleep(0.3)
    AUCTION_MARK.write_text(datetime.now(timezone.utc).isoformat())
    logger.info("Mic auctions: %d stored from eBay", stored)
    return stored


def _auction_end(desc: str) -> Optional[datetime]:
    m = re.search(r"ends (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", desc or "")
    if not m:
        return None
    from zoneinfo import ZoneInfo
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))


def auctions(cfg: dict) -> list[dict]:
    """Live eBay auctions for your mics ending within AUCTION_DAYS, soonest
    first, with r['mic'] like find() (price = current bid + shipping)."""
    from scrapers.enrich import is_partial, needs_repair, parse_price
    from scrapers.store import _conn
    targets = [t for t in settings(cfg)["targets"] if t["on"]]
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE description LIKE 'Auction%' AND url LIKE '%ebay.com%' AND hidden = 0"
            " AND duplicate = 0 AND COALESCE(sold, 0) = 0 AND (country IS NULL OR country = 'US')")]
    out, seen = [], set()
    for r in rows:
        end = _auction_end(r.get("description"))
        if not end or not (now < end <= now + timedelta(days=AUCTION_DAYS)):
            continue
        t = classify(r.get("title") or "", targets)
        if not t or is_partial(r["title"]) or needs_repair(r["title"], r.get("description")) or r["url"] in seen:
            continue
        bid = parse_price(r.get("price")) or 0
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        total = round(bid + (ship or 0), 2)
        if total > t["max_price"]:
            continue
        seen.add(r["url"])
        mins = int((end - now).total_seconds() // 60)
        r["mic"] = {"target": t["key"], "name": t["name"], "max_price": t["max_price"], "delivered": total,
                    "bid": bid, "ends": end.isoformat(), "mins_left": mins}
        out.append(r)
    out.sort(key=lambda r: r["mic"]["ends"])
    return out


def _ensure_auction(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS mic_auction_alerted (url TEXT PRIMARY KEY, at TEXT)")


def _current_bid(url: str, cfg: dict) -> Optional[tuple]:
    """(bid, shipping, ended) straight from eBay, or None if it can't tell."""
    import requests
    from scrapers.ebay_api import SEARCH_URL, _check_response, _get_token, _headers, shipping_of
    m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", url or "")
    api = cfg.get("ebay_api") or {}
    if not m or not api.get("client_id"):
        return None
    try:
        token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
        base = SEARCH_URL.rsplit("/item_summary", 1)[0]
        resp = requests.get(f"{base}/item/get_item_by_legacy_id", params={"legacy_item_id": m.group(1)},
                            headers=_headers(token), timeout=20)
        _check_response(resp)
        if resp.status_code == 404:
            return (None, None, True)
        j = resp.json()
        bid = float(((j.get("currentBidPrice") or j.get("price") or {}).get("value")))
        return (bid, shipping_of(j), False)
    except Exception as e:
        logger.debug("Couldn't re-check auction %s: %s", url, e)
        return None


def alert_auctions(cfg: dict) -> int:
    """A push about an hour before a mic auction ends, while the bid (checked
    again with eBay just now) plus shipping is still under that mic's limit."""
    from scrapers.notify import push_enabled, send_push
    from scrapers.store import _conn
    if not push_enabled(cfg):
        return 0
    soon = [r for r in auctions(cfg) if r["mic"]["mins_left"] <= AUCTION_ALERT_MIN]
    if not soon:
        return 0
    with _conn() as conn:
        _ensure_auction(conn)
        done = {u for (u,) in conn.execute("SELECT url FROM mic_auction_alerted")}
    sent = 0
    for r in soon:
        if r["url"] in done:
            continue
        m = r["mic"]
        live = _current_bid(r["url"], cfg)
        if live and live[2]:
            continue  # ended / removed
        bid, ship = (live[0], live[1]) if live and live[0] is not None else (m["bid"], r.get("shipping"))
        total = bid + (ship or 0)
        with _conn() as conn:
            _ensure_auction(conn)
            conn.execute("INSERT OR IGNORE INTO mic_auction_alerted VALUES (?, ?)",
                         (r["url"], datetime.now(timezone.utc).isoformat()))
            conn.commit()
        if total > m["max_price"]:
            continue
        ship_txt = " + free shipping" if ship == 0 else (f" + ${ship:,.0f} shipping" if ship else "")
        send_push(cfg, f"🔨 {m['name']}: auction ends in {max(1, m['mins_left'])} min",
                  f"{r['title'][:100]}\nCurrent bid ${bid:,.0f}{ship_txt} · your limit ${m['max_price']:,}",
                  url=r["url"], tags=["hammer"], image=r.get("image_url"))
        sent += 1
    if sent:
        logger.info("Mic auction alerts sent: %d", sent)
    return sent
