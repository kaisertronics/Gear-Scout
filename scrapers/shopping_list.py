"""Studio shopping list: matches the cable/gear list against the listings
Gear Scout has found (still for sale, seen in the last 10 days). build()
is cached for 10 minutes. Matching logic comes from the one-off match.py."""
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from scrapers.enrich import needs_repair, parse_price, price_basis

DB = "/data/seen_listings.db"
STATE_PATH = "/data/shopping_state.json"
TAX = 0.107  # WA sales tax
CAD = 0.72  # USD per Canadian dollar (approx.)
USED_OF_NEW = 0.75  # a used piece is worth about this share of new when there is no price history

# --------------------------------------------------------------- helpers
FT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:ft\b|feet\b|foot\b|'|’|ft\.)", re.I)
M = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b|metre)", re.I)


def length_ft(t):
    m = FT.search(t)
    if m:
        return float(m.group(1))
    m = M.search(t)
    if m and float(m.group(1)) <= 30:
        return round(float(m.group(1)) * 3.281, 1)
    return None


def has(t, *pats):
    return all(re.search(p, t, re.I) for p in pats)


def no(t, *pats):
    return not any(re.search(p, t, re.I) for p in pats)


CABLE_JUNK = [r"\by[- ]?(cable|split)", r"splitter", r"\bdual\b", r"insert", r"\btrrs\b", r"3\.5\d?\s?mm", r"\brca\b", r"1/8", r"\baux\b", r"summing", r"xlrf.*xlrm|xlrm.*xlrf",
              r"adapter\b(?!.*cable)", r"\bdmx\b", r"speakon", r"\bmidi\b", r"\bmic(rophone)?\b(?!.*cable)", r"snake",
              r"patch ?bay", r"\bdi\b", r"stand", r"\bbox\b", r"\bbag\b", r"\bcase\b"]


def xlr_f_trs(t):   # XLR female → ¼" TRS
    return (has(t, r"xlr") and has(t, r"\btrs\b|1/4|¼|6\.35") and
            re.search(r"xlr[\s-]*(f\b|female)|female[\s-]*xlr|(trs|1/4|¼).{0,25}(f\b|female)", t, re.I) and
            no(t, r"xlr[\s-]*(m\b|male)\b(?!.*female)", r"\bts\b(?!.*trs)", *CABLE_JUNK))


def trs_xlr_m(t):   # ¼" TRS → XLR male
    return (has(t, r"xlr") and has(t, r"\btrs\b|1/4|¼|6\.35") and
            re.search(r"xlr[\s-]*(m\b|male)|male[\s-]*xlr", t, re.I) and no(t, r"female", r"\bts\b(?!.*trs)", *CABLE_JUNK))


def trs_trs(t):
    return (re.search(r"trs.{0,30}(to|-|–|↔)\s*.{0,10}trs|1/4.{0,20}trs.{0,20}male to male|trs (patch )?cable", t, re.I)
            and no(t, r"xlr", r"\bts\b(?!.*trs)", *CABLE_JUNK))


def xlr_mf(t):
    return (has(t, r"xlr") and re.search(r"(m(ale)?)\s*(to|-|/)\s*(f(emale)?)|(f(emale)?)\s*(to|-|/)\s*(m(ale)?)|mic(rophone)? cable|xlr cable|xlr-xlr|xlr to xlr", t, re.I)
            and no(t, r"trs|1/4|¼|6\.35", r"\bts\b", r"snake", r"splitter", r"\by[- ]?cable", r"\bdmx\b", r"\bbox\b", r"microphone(?! cable)", r"\bmic\b(?! cable)"))


def ts_cable(t):
    return (re.search(r"instrument cable|guitar cable|\bts\b.{0,20}(cable|to)|1/4.{0,10}(to|-)\s*1/4", t, re.I)
            and no(t, r"trs", r"xlr", r"midi", r"speaker", r"snake", r"female", r"extension", r"coil", r"stereo", *CABLE_JUNK[:6]))


def length_ok(want):
    lo, hi = want
    return lambda t: (lambda L: L is not None and lo <= L <= hi)(length_ft(t))


ITEMS = [
    # key, name, qty, need, new unit price, new source, new url, match(title), extra note
    dict(key="nys", name="Neutrik NYS-SPP-L1 48-point ¼\" TRS patchbay", qty=4, need="Bays 1–4",
         new=108.86, nsrc="Sweetwater", nurl="https://www.sweetwater.com/store/detail/NYSSPPL1--neutrik-nys-spp-l1-48-point-1-4-inch-trs-patchbay",
         match=lambda t: re.search(r"nys[- ]?spp", t, re.I) and no(t, r"cable", r"label", r"strip only", r"clip", r"clamp", r"grounding")),
    dict(key="p16", name="ART P16 XLR patchbay", qty=3, need="Mic input panels D, E, F",
         new=73.00, nsrc="eBay (new)", nurl="https://www.ebay.com/itm/298701675930",
         match=lambda t: re.search(r"\bart\b.{0,40}\bp-?16\b|\bp-?16\b.{0,30}\bart\b", t, re.I) and no(t, r"behringer", r"powerplay", r"p16-?[dmi]\b", r"artist", r"label", r"compatible with")),
    dict(key="dtp", name="DB25 → 8 × ¼\" TRS snake, Tascam pinout, about 2 m", qty=3, need="W01, W02, W03 (Oryx / x8p DB25s)",
         new=57.90, nsrc="eBay (Hosa DTP-802 new)", nurl="https://www.ebay.com/itm/403241609223",
         match=lambda t: (re.search(r"dtp[- ]?80[2-5]|db-?25.{0,25}(to|-|>)\s*(8\s*x?\s*)?(1/4|trs)|db-?25.{0,10}trs", t, re.I)
                          and no(t, r"aes", r"yamaha", r"xlr", r"\bts\b(?!.*trs)", r"various", r"\blot\b", r"patch ?bay", r"bundle", r"assorted") and
                          (re.search(r"dtp|tascam|mogami|digidesign|avid|hosa", t, re.I) is not None)),
         note="Must be the Tascam (DTP / Digidesign) pinout. Mogami Gold DB25-TRS and Hosa DTP-80x are."),
    dict(key="stx", name="8 × XLR female → ¼\" TRS snake, about 5 m (15–16 ft)", qty=3, need="L03, L05, L06: left-wing outputs → bays",
         new=69.00, nsrc="eBay (Hosa STX-805F)", nurl="https://www.ebay.com/itm/267177294758",
         match=lambda t: (re.search(r"stx[- ]?80[3-7]f|stx ?800|8.{0,15}xlr.{0,6}(f|female).{0,20}trs.{0,15}snake", t, re.I)
                          or (re.search(r"stx[- ]?80[3-7](?![a-z0-9])", t, re.I) and has(t, r"female")))  # "Hosa STX-805 ... TRS to Female XLR"
                         and no(t, r"xlr ?m(ale)?\b(?!.*female)", r"\d ?xlr ?m\b|xlrm") and (length_ok((13, 25))(t) or bool(re.search(r"stx-?80[57]f?", t, re.I)))),
    dict(key="cpp", name="8 × ¼\" TS ↔ TS snake, about 5 m", qty=1, need="L08 MLA7 outputs → Bay 4",
         new=54.89, nsrc="eBay (Hosa CPP-805)", nurl="https://www.ebay.com/itm/256284378367",
         match=lambda t: re.search(r"cpp[- ]?80[2-5]|8.{0,10}(ch|channel).{0,15}\bts\b.{0,15}snake", t, re.I) and no(t, r"patch ?bay", r"bundle", r"rca")),
    dict(key="sacb", name="8-channel XLR snake, 25 ft, fan-outs both ends", qty=1, need="A01 P16 'D' → Alctron 1–8 (R1)",
         new=72.42, nsrc="eBay (Seismic SACB-8x25 used)", nurl="https://www.ebay.com/itm/117390320104",
         match=lambda t: (re.search(r"\b8\b.{0,12}(ch|channel)|8x25|sacb-?8", t, re.I) and has(t, r"xlr") and has(t, r"snake|fan")
                          and no(t, r"trs", r"rca", r"db-?25", r"stage ?box", r"\bbox\b", r"\bts\b") and length_ok((20, 35))(t))),
    dict(key="css", name="8 × ¼\" TRS ↔ TRS snake, 2 m", qty=3, need="W04, W05, W06: interfaces ↔ bays (right rack)",
         new=49.95, nsrc="eBay (Hosa CSS-802, est.)", nurl="https://www.ebay.com/sch/i.html?_nkw=Hosa+CSS-802",
         match=lambda t: (re.search(r"css-?80[2-3]", t, re.I) or (re.search(r"\b8\b.{0,12}(ch|channel)", t, re.I) and re.search(r"trs.{0,20}(to|-|↔|/).{0,12}trs|trs.{0,10}male snake", t, re.I) and has(t, r"snake") and length_ok((5.5, 11))(t)))
                         and no(t, r"xlr", r"\bts\b(?!.*trs)", r"rca", r"db-?25", r"patch ?bay", r"bundle")),
    dict(key="sntx15", name="8 × ¼\" TRS → 8 × XLR male snake, 15 ft", qty=1, need="CR3 'CUES': Bay 2 → control-room panel headphone jacks",
         new=70.10, nsrc="eBay (new)", nurl="https://www.ebay.com/sch/i.html?_nkw=8+channel+TRS+to+XLR+male+snake+15ft",
         match=lambda t: re.search(r"\b8\b.{0,12}(ch|channel)|stx-?80[3-5]m", t, re.I) and has(t, r"trs") and re.search(r"xlr.{0,6}(m\b|male)|male.{0,3}xlr", t, re.I) and no(t, r"female", r"rca", r"db-?25") and (length_ok((13, 20))(t) or re.search(r"stx-?805m", t, re.I))),
    dict(key="sntx25", name="8 × ¼\" TRS → 8 × XLR male snake, 25 ft", qty=2, need="'R2 SEND', 'FAR SEND': bays → cabinet inputs",
         new=115.00, nsrc="eBay / Seismic Audio (est.)", nurl="https://www.ebay.com/sch/i.html?_nkw=8+channel+TRS+to+XLR+male+snake+25ft",
         match=lambda t: re.search(r"\b8\b.{0,12}(ch|channel)", t, re.I) and has(t, r"trs") and re.search(r"xlr.{0,6}(m\b|male)|male.{0,3}xlr", t, re.I) and no(t, r"female", r"rca", r"db-?25", r"\bbox\b", r"junction") and length_ok((23, 30))(t)),
    dict(key="snxt25", name="8 × XLR female → 8 × ¼\" TRS snake, 25 ft", qty=3, need="'R1 OUT', 'R2 RETURN', 'FAR RETURN': cabinet outputs → bays",
         new=115.00, nsrc="eBay / Seismic Audio (est.)", nurl="https://www.ebay.com/sch/i.html?_nkw=8+channel+XLR+female+to+TRS+snake+25ft",
         match=lambda t: re.search(r"\b8\b.{0,12}(ch|channel)", t, re.I) and has(t, r"trs") and re.search(r"xlr.{0,6}(f\b|female)|female.{0,3}xlr", t, re.I) and no(t, r"xlr[\s-]*(m\b|male)\b(?!.*female)", r"rca", r"db-?25") and length_ok((23, 30))(t)),
    dict(key="disn", name="8-channel XLR snake, 15 ft (male–female fan-outs)", qty=1, need="L15: DI800 outputs → tails DI 1–8 at the P16s",
         new=55.00, nsrc="eBay (est.)", nurl="https://www.ebay.com/sch/i.html?_nkw=8+channel+XLR+snake+15+ft",
         match=lambda t: (re.search(r"\b8\b.{0,12}(ch|channel)|8x1[05]", t, re.I) and has(t, r"xlr") and has(t, r"snake|fan")
                          and no(t, r"trs", r"rca", r"db-?25", r"stage ?box", r"\bbox\b", r"\bts\b", r"splitter") and length_ok((12, 20))(t))),
    dict(key="brush", name="1U rack brush panel", qty=1, need="Right rack space 8: tails come up to the P16s",
         new=19.99, nsrc="eBay (est.)", nurl="https://www.ebay.com/sch/i.html?_nkw=1U+rack+brush+panel",
         match=lambda t: re.search(r"brush", t, re.I) and re.search(r"\b1u\b|rack", t, re.I) and re.search(r"panel|plate|strip", t, re.I) and no(t, r"2u", r"3u", r"grommet(?!.*1u)")),
    dict(key="xftrs15", name="XLR female → ¼\" TRS cable, 15 ft", qty=4, need="L14 WA-412 outputs",
         new=10.48, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/xlr-3-pin-female-to-1-4-inch-trs-male-balanced-cable",
         match=lambda t: xlr_f_trs(t) and length_ok((15, 20))(t)),
    dict(key="xftrs10", name="XLR female → ¼\" TRS cable, 10 ft", qty=2, need="E02 Ampex outputs",
         new=8.85, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/xlr-3-pin-female-to-1-4-inch-trs-male-balanced-cable",
         match=lambda t: xlr_f_trs(t) and length_ok((10, 14))(t)),
    dict(key="trsxm15", name="¼\" TRS → XLR male cable, 15 ft", qty=3, need="W14 left monitor + CR4 spare headphone lines",
         new=10.48, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/xlr-3-pin-male-to-1-4-inch-trs-male-balanced-cable",
         match=lambda t: trs_xlr_m(t) and length_ok((15, 20))(t)),
    dict(key="trsxm10", name="¼\" TRS → XLR male cable, 10 ft", qty=2, need="E01 Ampex inputs",
         new=8.85, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/xlr-3-pin-male-to-1-4-inch-trs-male-balanced-cable",
         match=lambda t: trs_xlr_m(t) and length_ok((10, 14))(t)),
    dict(key="trs6", name="¼\" TRS ↔ TRS cable, 6 ft", qty=2, need="W09 Oryx reamp outs → Bay 4",
         new=3.44, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/1-4-inch-trs-male-to-1-4-inch-trs-male-cable",
         match=lambda t: trs_trs(t) and length_ok((5, 7))(t)),
    dict(key="trs1", name="¼\" TRS patch cable, 1 ft", qty=12, need="Front patching, neighboring bays",
         new=2.25, nsrc="Infinite Cables (est.)", nurl="https://us.infinitecables.com/products/1-4-inch-trs-male-to-1-4-inch-trs-male-cable",
         match=lambda t: (trs_trs(t) or re.search(r"\btrs\b.{0,20}patch (cable|cord)|patch (cable|cord)s?.{0,20}\btrs\b", t, re.I) and no(t, r"xlr", r"\btt\b", r"bantam", r"tt-?\d", *CABLE_JUNK[:6])) and length_ok((0.8, 1.5))(t)),
    dict(key="trs2", name="¼\" TRS patch cable, 2 ft", qty=12, need="Front patching across the bays",
         new=2.49, nsrc="Infinite Cables (est.)", nurl="https://us.infinitecables.com/products/1-4-inch-trs-male-to-1-4-inch-trs-male-cable",
         match=lambda t: (trs_trs(t) or re.search(r"\btrs\b.{0,20}patch (cable|cord)|patch (cable|cord)s?.{0,20}\btrs\b", t, re.I) and no(t, r"xlr", r"\btt\b", r"bantam", r"tt-?\d", *CABLE_JUNK[:6])) and length_ok((1.6, 2.5))(t)),
    dict(key="trs10", name="¼\" TRS ↔ TRS cable, 10 ft", qty=7, need="H01, H02 (wall jacks → headphone amps)",
         new=8.74, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/1-4-inch-trs-male-to-1-4-inch-trs-male-cable",
         match=lambda t: trs_trs(t) and length_ok((10, 15))(t)),
    dict(key="xlr3", name="XLR male–female cable, 3 ft", qty=8, need="W11 P16 'F' → x8p mic inputs",
         new=7.26, nsrc="Infinite Cables", nurl="https://us.infinitecables.com/products/xlr-3-pin-male-to-xlr-3-pin-female-balanced-cable-black",
         match=lambda t: xlr_mf(t) and length_ok((1.5, 5))(t)),
    dict(key="ts25", name="¼\" TS instrument cable, 25 ft", qty=3, need="C03, C04 SPX90 in/out",
         new=8.49, nsrc="eBay (new)", nurl="https://www.ebay.com/itm/400944841195",
         match=lambda t: ts_cable(t) and length_ok((20, 30))(t)),
    dict(key="bnc", name="BNC 75 Ω word-clock cable, 3 ft", qty=2, need="W13 Oryx → x8p, 8p",
         new=13.95, nsrc="eBay (Hosa BNC-59-103)", nurl="https://www.ebay.com/itm/257480139257",
         match=lambda t: has(t, r"\bbnc\b") and re.search(r"75|word ?clock|bnc-59", t, re.I) and no(t, r"\blot\b", r"adapter", r"\bsdi\b(?!.*75)", r"50 ?ohm", r"camera", r"cctv", r"snake", r"xlr", r"\bav\b") and length_ok((2, 6))(t)),
    dict(key="nc3fd", name="Female XLR panel jack, screw terminals (Sescom NC3FD-SCREW)", qty=34, need="Room mic jacks + control-room headphone jacks",
         new=8.49, nsrc="Markertek", nurl="https://www.markertek.com/product/nc3fd-screw/sescom-nc3fd-screw-3-pin-female-xlr-panel-chassis-mount-connector-with-rear-screw-terminals",
         match=lambda t: re.search(r"nc3fd[- ]?screw|nc3fd-s-1|(female|nc3fd|xlr-?f).{0,40}screw ?term|screw ?term.{0,40}(female|xlr-?f)", t, re.I) and no(t, r"\bmale\b(?!.*female)", r"plug", r"cable", r"adapter"),
         note="Neutrik's own NC3FD-S-1-B is the same screw-terminal jack (black/gold) and works the same."),
    dict(key="nc3md", name="Male XLR panel jack, screw terminals (Sescom NC3MD-SCREW)", qty=24, need="Control-room panel TRK 1–16, ISO 1–8",
         new=12.49, nsrc="Markertek", nurl="https://www.markertek.com/search?q=NC3MD-SCREW",
         match=lambda t: re.search(r"nc3md[- ]?screw|nc3md-s-1|(\bmale\b|nc3md|xlr-?m).{0,40}screw ?term|screw ?term.{0,40}(\bmale\b|xlr-?m)", t, re.I) and no(t, r"female", r"plug", r"cable", r"adapter"),
         note="Neutrik's own NC3MD-S-1-B is the same screw-terminal jack (black/gold) and works the same."),
    dict(key="nj3", name="Neutrik NJ3FP6C ¼\" TRS panel jack", qty=9, need="Room headphone-line jacks",
         new=9.99, nsrc="eBay", nurl="https://www.ebay.com/itm/226658031792",
         match=lambda t: re.search(r"nj3fp6", t, re.I)),
    dict(key="penn", name="Penn Elcom D-series rack panels (1U-16, 2U-24, 3U-36)", qty=3, need="Wall panels",
         new=26.00, nsrc="Penn Elcom (est.)", nurl="https://penn-elcom.com/us/3u-rack-panels-punched-for-36-x-d-series-connectors-r1269-3uk-36",
         match=lambda t: re.search(r"r1269|(punched|d[- ]?(series|size|type)).{0,30}rack ?panel|(1u|2u|3u|rack ?panel).{0,30}(punched|d[- ]?(series|size|type)|for \d+ (xlr|neutrik|d))", t, re.I) and no(t, r"ne8", r"ethercon", r"rj45", r"\bjack\b", r"blank", r"r1285", r"vent", r"connector(?!s\b)")),
    dict(key="ha400", name="Behringer HA400 headphone amp (optional)", qty=1, need="Iso room",
         new=29.00, nsrc="new (typical)", nurl="https://www.sweetwater.com/store/detail/HA400--behringer-microamp-ha400-4-channel-stereo-headphone-amplifier",
         match=lambda t: re.search(r"ha-?400|microamp", t, re.I) and has(t, r"behringer") and no(t, r"stand", r"adapter", r"charger", r"power supply", r"\bpsu\b", r"cable")),
]


# What the sweep types into eBay / Reverb / Craigslist for each item (one search per item, every
# sweep, same rate as every Telex term). The match() above still decides what counts.
SEARCH = {
    "nys": ["Neutrik NYS-SPP-L1"], "p16": ["ART P16 patchbay"], "dtp": ["Hosa DTP-802", "Mogami DB25 TRS snake"],
    "stx": ["Hosa STX-805F"], "cpp": ["Hosa CPP-805"], "sacb": ["8 channel XLR snake 25 ft"],
    "css": ["Hosa CSS-802"], "sntx15": ["8 channel TRS XLR male snake 15 ft"], "sntx25": ["8 channel TRS XLR male snake 25 ft"],
    "snxt25": ["8 channel XLR female TRS snake 25 ft"], "disn": ["8 channel XLR snake 15 ft"],
    "brush": ["1U rack brush panel"], "xftrs15": ["XLR female to TRS cable 15 ft"], "xftrs10": ["XLR female to TRS cable 10 ft"],
    "trsxm15": ["TRS to XLR male cable 15 ft"], "trsxm10": ["TRS to XLR male cable 10 ft"], "trs6": ["TRS to TRS cable 6 ft"],
    "trs1": ["TRS patch cable 1 ft"], "trs2": ["TRS patch cable 2 ft"], "trs10": ["TRS to TRS cable 10 ft"],
    "xlr3": ["XLR cable 3 ft"], "ts25": ["instrument cable 25 ft"], "bnc": ["BNC word clock cable 75 ohm"],
    "nc3fd": ["Sescom NC3FD-SCREW"], "nc3md": ["Sescom NC3MD-SCREW"], "nj3": ["Neutrik NJ3FP6C"],
    "penn": ["Penn Elcom R1269"], "ha400": ["Behringer HA400"],
}
# The exact model, accepted when it is only in the listing's description ("Hosa STX-805" titled
# "8-Channel Audio Snake Cable"); the optional F/M after the digits is the gender, not a model.
MODEL = {
    "dtp": r"dtp[- ]?80[2-5]", "stx": r"stx[- ]?80[3-7][fm]?", "cpp": r"cpp[- ]?80[2-5]", "css": r"css[- ]?80[2-3]",
    "nys": r"nys[- ]?spp", "nc3fd": r"nc3fd[- ]?screw", "nc3md": r"nc3md[- ]?screw", "nj3": r"nj3fp6",
    "penn": r"r1269", "ha400": r"ha-?400", "bnc": r"bnc-59",
}


def search_terms():
    """Every shopping-list item's own search terms, for the sweep."""
    return list(dict.fromkeys(q for it in ITEMS for q in SEARCH.get(it["key"], [])))


def is_search_term(term):
    return term in set(search_terms())


def matches_item(it, title, description=None):
    """Title matches the item, or the exact model is written in the description."""
    if it["match"](title):
        return True
    model, desc = MODEL.get(it["key"]), re.sub(r"<[^>]+>", " ", description or "")
    if not model or not re.search(r"(?<![a-z0-9])" + model + r"(?![a-z0-9])", desc, re.I):
        return False
    others = re.findall(r"\b(?:dtp|stx|cpp|css)[- ]?8\d{2}[fm]?\b", title, re.I)  # the title names a different model
    return not any(not re.fullmatch(model, o, re.I) for o in others)


def _unit_of(r):
    v = parse_price(r["price"])
    if not v or v < 1:
        return None, None, None
    if "C$" in (r["price"] or "") or (r["country"] == "CA"):
        v = round(v * CAD, 2)
    # Quantity from the title only ("I have 3" in a description is stock, not the price basis).
    basis, qty, unit = price_basis(r["title"], None, v)
    return v, max(qty, 1), unit


def _usable(r):
    t = r["title"]
    return not (needs_repair(t, r["description"]) or re.search(r"\bwanted\b|\biso\b|\bwtb\b|looking for", t, re.I))


def _build():
    now = datetime.now(timezone.utc)
    live_cut = (now - timedelta(days=10)).isoformat()
    hist_cut = (now - timedelta(days=90)).isoformat()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""SELECT title, price, url, source_name, seller, location, shipping, last_seen,
                                      sold, pending, hidden, duplicate, description, country
                               FROM seen WHERE title IS NOT NULL AND (country IS NULL OR country IN ('US','CA'))""").fetchall()
    finally:
        conn.close()
    out = []
    for it in ITEMS:
        live, hist = [], []
        for r in rows:
            t = r["title"]
            if not matches_item(it, t, r["description"]) or not _usable(r):
                continue
            price, qty, unit = _unit_of(r)
            if not unit:
                continue
            auction = (r["description"] or "").startswith("Auction")
            if (r["last_seen"] or "") >= hist_cut and not auction:
                hist.append(unit)
            if r["sold"] or r["pending"] or r["hidden"] or r["duplicate"] or (r["last_seen"] or "") < live_cut:
                continue
            ship = r["shipping"] if isinstance(r["shipping"], (int, float)) else 0
            ship_unit = ship / qty
            live.append({"title": t, "source": r["source_name"], "url": r["url"], "unit": round(unit, 2),
                         "ship": round(ship_unit, 2), "auction": auction, "seller": r["seller"], "pieces": qty,
                         "delivered": round((unit + ship_unit) * (1 + TAX), 2)})
        uniq, seen_urls = [], set()
        for rec in sorted(live, key=lambda x: x["delivered"]):
            if rec["url"] not in seen_urls:
                seen_urls.add(rec["url"])
                uniq.append(rec)
        med = statistics.median(hist) if len(hist) >= 3 else None
        if med:  # drop junk prices far under the market (a $3 "cable" that's a plug)
            # Floor is a quarter of the market median or of the new price, whichever is lower: a pricey
            # outlier family (Mogami $230 DB25 snakes) must not hide a $22 Hosa (Nov 3).
            uniq = [x for x in uniq if x["unit"] >= min(med, it["new"]) * 0.25]
        if it["key"] not in ("nys", "p16", "dtp", "penn"):
            uniq = [x for x in uniq if x["unit"] <= it["new"] * 4]
        used_unit = min(med, it["new"]) if med else it["new"] * USED_OF_NEW
        out.append({k: v for k, v in it.items() if k != "match"} | {
            "new_taxed": round(it["new"] * (1 + TAX), 2), "matches": uniq[:3], "total_matches": len(uniq),
            "used_unit": round(used_unit, 2), "by_seller": [x for x in uniq if x.get("seller") and not x["auction"]]})
    return {"built": now.isoformat(), "items": out}


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def build():
    """The shopping list with live matches; rebuilt at most every 10 minutes."""
    with _lock:
        if _cache["data"] is None or time.time() - _cache["at"] > 600:
            _cache["data"], _cache["at"] = _build(), time.time()
        return _cache["data"]


def load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    return {"bought": dict(s.get("bought") or {}), "budget": s.get("budget")}


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_PATH)


BUNDLE_MIN_ITEMS = 3     # different shopping-list items from one seller
BUNDLE_UNDER = 0.15      # the offer is this far under the combined used value
BUNDLE_FLOOR = 0.70      # never offer under this share of the combined ask
BUNDLE_LIMIT = 5


def _r5(x):
    return int(round(x / 5.0) * 5)


def find_bundles(items, bought):
    """Pure. One seller listing BUNDLE_MIN_ITEMS+ different items still needed -> one lot offer.
    items = _build()["items"]; bought = {key: pieces already bought}. Per seller (same site), the cheapest
    listing of each needed item; ask = their prices + shipping; value = pieces x the item's used price;
    offer = BUNDLE_UNDER under the value (and under the ask), never below BUNDLE_FLOOR of the ask."""
    groups: dict = {}
    for it in items:
        left = it["qty"] - min(max(int(bought.get(it["key"], 0) or 0), 0), it["qty"])
        if left <= 0:
            continue
        best: dict = {}
        for m in it.get("by_seller") or []:
            g = (m["source"], str(m["seller"]).strip().lower())
            if g not in best or m["delivered"] < best[g]["delivered"]:
                best[g] = m
        for g, m in best.items():
            groups.setdefault(g, []).append((it, m, left))
    out = []
    for (source, _sel), rows in groups.items():
        if len(rows) < BUNDLE_MIN_ITEMS:
            continue
        lines, ask, value = [], 0.0, 0.0
        for it, m, left in rows:
            pcs = max(1, min(int(m.get("pieces") or 1), left))
            cost = (m["unit"] + m["ship"]) * pcs
            ask += cost
            value += it["used_unit"] * pcs
            lines.append({"name": it["name"], "title": m["title"], "url": m["url"], "pieces": pcs, "cost": round(cost, 2)})
        if ask <= 0 or value <= 0:
            continue
        offer = max(_r5(min(value * (1 - BUNDLE_UNDER), ask * 0.9)), _r5(ask * BUNDLE_FLOOR))
        offer = min(offer, int(ask))
        who = rows[0][1]["seller"]
        listing = "; ".join(f"{l['pieces']} x {l['title'][:60]}" for l in lines[:6]) + (f"; and {len(lines) - 6} more" if len(lines) > 6 else "")
        out.append({"seller": who, "source": source, "lines": lines, "ask": round(ask, 2), "value": round(value, 2),
                    "offer": offer, "pct_under_value": round((1 - offer / value) * 100), "pct_under_ask": round((1 - offer / ask) * 100),
                    "message": (f"Hi! I see you have several things I'm looking for: {listing}. Would you take ${offer:,} for all of them, "
                                f"shipped to Seattle? I can pay right away. Thanks!")})
    out.sort(key=lambda b: -(b["value"] - b["offer"]))
    return out[:BUNDLE_LIMIT]


def summary():
    """Items with how many are still needed, plus the running total."""
    st = load_state()
    items, total = [], 0.0
    for it in build()["items"]:
        got = min(max(int(st["bought"].get(it["key"], 0) or 0), 0), it["qty"])
        left = it["qty"] - got
        best = it["matches"][0]["delivered"] if it["matches"] else None
        unit = best if best is not None else it["new_taxed"]
        saving = round(it["new_taxed"] - best, 2) if best is not None else None
        if left:
            total += unit * left
        items.append(dict(it, got=got, left=left, unit_used=unit, used=best is not None, saving=saving))
    total = round(total, 2)
    budget = st["budget"]
    return {"items": items, "total": total, "budget": budget,
            "diff": round(abs(budget - total), 2) if budget is not None else None,
            "over": budget is not None and total > budget, "built": build()["built"],
            "bundles": find_bundles(build()["items"], st["bought"])}
