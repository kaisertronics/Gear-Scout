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
LIVE_DAYS = 10          # still listed by a site within this many days
NEW_HOURS = 6           # "new" for alerts: first found within this many hours
ALERT_MAX_SINGLE = 8    # more than this in one pass → one summary push
MIN_PRICE = 40.0        # under this it's a placeholder or a part
MIN_PUSH_PRICE = 300    # owner's floor (Oct 22): nothing under $300 delivered pushes or makes the list

_TUBE = r"\btube\b|\bvalve\b|\bvacuum\b"
_FET = re.compile(r"\bfet\b|\bjr\.?\b|\bjunior\b|\bsolid[- ]state\b|\bwa-?47 ?f\b|\b47 ?fet\b|\bu-?47 ?fet\b", re.I)
_NOT_MIC = re.compile(
    r"\b(?:pre-?amps?|preamplifiers?|compressors?|limiters?|equali[sz]ers?|eq|channel ?strips?|500[\s-]?series|"
    r"lunchbox|di box|interface|headphones?|monitors?|speakers?|guitar|pedal|plugin|plug-in|software|t-?shirt|"
    r"hat|sticker|poster|book|magazine|diecast|die-cast|figures?|toys?|chogokin|popy|robot|lego|model kit|tuner)\b", re.I)
_PART = re.compile(
    r"\b(?:capsules?(?! (?:included|incl))|power suppl(?:y|ies)|psu|cables? only|cable for|shock ?mounts?|"
    r"spider|mount only|clip|case only|flight case|box only|grille|head ?basket|basket|body only|"
    r"(?:transformer|tube|valve|pcb|board|kit|parts?|socket)s?(?: only| for)|for parts|parts only|replacement|"
    r"windscreen|pop filter|foam|manual|cradle|holder|adapter|repair|not working|as[- ]is|broken)\b", re.I)

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
    dict(key="warm", name="Warm Audio tube mics", min_pct=60, max_price=1000,
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
         match=r"\bdachman\b", require=None, exclude=None,
         queries=["dachman microphone"],
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
    likes = ["advanced audio", "cm47", "cm-47", "cm12", "united studio", "ut twin", "ut fet", "ut tube", "signal art", "dachman", "peluso", "golden age", "ga-47", "ga47", "ga-251", "ga251", "stam", "warm audio", "wa-", "wa47", "wa251",
             "wa8000", "wacx", "lauten", "neez", "d12", "d-12", "c12", "c-12", "47", "251"]
    cut = (datetime.now(timezone.utc) - timedelta(days=LIVE_DAYS)).isoformat()
    sql = ("SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
           " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
           " AND url NOT LIKE '%search/posts%' AND (country IS NULL OR country = 'US')"
           " AND last_seen >= ? AND (" + " OR ".join("LOWER(title) LIKE ?" for _ in likes) + ")")
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
        hit = floor <= delivered < t["max_price"]  # by price (Oct 22), not % off
        r["mic"] = {"target": t["key"], "name": t["name"], "min_pct": t["min_pct"], "max_price": t["max_price"], "pct": pct,
                    "ref": round(ref) if ref else None, "ref_kind": ref_kind, "new": round(new) if new else None,
                    "used": round(used) if used else None, "delivered": delivered, "hit": hit,
                    "model": q, "basis": basis, "check": bool(pct is not None and pct >= 85)}
        out.append(r)
    out.sort(key=lambda r: r["mic"]["delivered"])  # cheapest first
    return out


def build(cfg: dict) -> dict:
    """Recomputes the list and saves it for the page."""
    rows = find(cfg)
    keep = ("title", "price", "url", "image_url", "source_name", "location", "shipping", "first_seen", "last_seen",
            "posted_at", "global_id", "description", "previous_price")
    data = {"built": datetime.now(timezone.utc).isoformat(),
            "rows": [{k: r.get(k) for k in keep} | {"mic": r["mic"]} for r in rows]}
    try:
        SNAPSHOT.write_text(json.dumps(data))
    except OSError:
        logger.exception("Couldn't save the Mic Focus list")
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
    hits = [r for r in data["rows"] if r["mic"]["hit"]]
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
