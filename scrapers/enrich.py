"""
Per-listing helpers shared by the scrape cycle, the dashboard and the email:
price parsing, "needs repair" detection, cross-post duplicate keys, the
user's exclude-words list, and typical-price / deal context.
"""
import functools
import re
import statistics
from typing import Optional
from urllib.parse import urlparse

# Things sellers write when gear is broken or sold for parts. Flagged, not
# hidden — plenty of buyers want repair projects.
_REPAIR_PATTERNS = [
    r"not working", r"non[\s-]?working", r"doesn'?t work", r"does not work",
    r"needs? (?:a )?(?:repair|work|service|fix|recap)", r"for (?:parts|repair)",
    r"parts only", r"as[\s-]is", r"broken", r"untested", r"no power",
    r"won'?t (?:power|turn) on", r"faulty", r"repair project", r"project (?:mic|amp|unit)",
    r"dead (?:unit|channel)", r"not functional",
]
_REPAIR_RE = re.compile(r"\b(?:" + "|".join(_REPAIR_PATTERNS) + r")\b", re.I)


# Parts and pieces of a model ("U47 head", "M70 capsule", "body only",
# "PSU only") — priced nothing like the complete unit, so they're left out
# of typical prices and never tagged as a deal against complete units.
_PARTIAL_RE = re.compile(
    r"\b(?:heads?|capsules?|(?:body|psu|case|pcb|chassis|faceplate|tube|transformer|grille|cable)s? only"
    r"|only (?:the )?(?:body|psu|capsule|case|head)|power suppl(?:y|ies) only|parts? only|for parts"
    r"|diy kit|kit only|replacement (?:capsule|grille|part|parts|tube)|empty (?:case|box|body))\b",
    re.I,
)


# Multiples priced as a set ("4 Sennheiser 421", "pair of KM184", "2x SM57")
# can't be compared with the typical price of one unit.
_LOT_RE = re.compile(
    r"^\W*(?:[2-9]|\d{2})\s+(?!ch|channel|track|space|band|way|input|output)[a-z]|"
    r"\b(?:pair|pairs|matched|stereo set|lot of|set of|bundle of|qty|quantity)\b|"
    r"\b[2-9]\s?x\b(?!\s?\d)|\bx\s?[2-9]\b|\(\s*[2-9]\s*\)",
    re.I,
)


_GEAR_NOUN = re.compile(
    r"\b(?:amp|amplifier|head|cab|cabinet|mic|mics|microphone|interface|monitors?|speakers?|sub|subwoofer|"
    r"mixer|preamp|pre-amp|compressor|eq|equalizer|headphones?|recorder|deck|synth|keyboard|controller|"
    r"pedal|di|console|rack|reverb|delay|limiter)s?\b", re.I)
_MODELISH = re.compile(r"\b[a-z]*\d+[a-z0-9-]*\b", re.I)
_ACCESSORY_WORDS = re.compile(
    r"^\s*(?:(?:a|the|its|original|matching)\s+)?(?:case|cases|cable|cables|cords?|shock ?mount|mount|clip|"
    r"stand|stands|pop filter|windscreen|foam|box|manual|power supply|psu|adapter|strap|bag|cover)\b", re.I)


_DEVICE = re.compile(
    r"\b(?:mic|mics|microphones?|interface|headphones?|monitors?|speakers?|subwoofer|sub|mixer|laptop|"
    r"keyboard|controller|stands?|turntable|cabinet|cab|amp|amplifier|head|combo|synth)\b", re.I)
_KIND = {"mics": "mic", "microphone": "mic", "microphones": "mic", "headphone": "headphones",
         "monitor": "speaker", "monitors": "speaker", "speakers": "speaker", "sub": "subwoofer",
         "cab": "cabinet", "amplifier": "amp", "head": "amp", "combo": "amp", "stands": "stand"}


def _model_tokens(text: str) -> set[str]:
    """Distinct model numbers in a piece of text ("VT-737SP", "18i20", "5150"),
    reduced to their digits so "VT-737SP" and "737 SP" count once."""
    out = set()
    for m in re.finditer(r"\b(?=[a-z0-9-]*\d)[a-z0-9]+(?:-[a-z0-9]+)*\b", text.lower()):
        tok = m.group(0)
        digits = re.sub(r"\D", "", tok)
        if not digits or re.fullmatch(r"(?:19|20)\d\d", tok) or re.fullmatch(r"\d{1,2}", tok):
            continue  # years and small counts aren't models
        if re.fullmatch(r"\d+(?:-?bit|khz|hz|w|v|mm|in|ch|u|ft|feet|m|pc|pcs|pack)|\d+x\d+|\d+/\d+", tok):
            continue
        out.add(digits)
    return out


@functools.lru_cache(maxsize=100_000)
def is_bundle(title: Optional[str]) -> bool:
    """Several different products sold together ("Avalon VT-737SP + Focusrite
    Scarlett 18i20", "EVH 5150 amp & 2x12 cabinet", "mic, interface and
    headphones"). One unit described by its functions is not a bundle:
    "Stereo Compressor & EQ", "Preamp / Compressor / EQ", "Preamp and DI"."""
    t = title or ""
    if re.search(r"\b(?:bundle|package deal|full setup|studio setup|combo deal|lot of)\b", t, re.I):
        return True
    parts = [p for p in re.split(r"\s(?:&|\+|and|plus|w/|with)\s|,\s", t) if p.strip()]
    if len(parts) < 2:
        kinds = {_KIND.get(m.group(0).lower(), m.group(0).lower()) for m in _DEVICE.finditer(t)} - {"stand"}
        return len(_model_tokens(t)) >= 2 and len(kinds) >= 2
    # Two parts naming different model numbers = two products.
    seen, distinct = set(), 0
    for part in parts:
        toks = _model_tokens(part)
        if _ACCESSORY_WORDS.match(part):
            continue
        if toks and not toks & seen:
            distinct += 1
        seen |= toks
    if distinct >= 2:
        return True
    # Different kinds of devices in different parts ("amp & 2x12 cabinet",
    # "monitors + subwoofer", "mic, interface and headphones").
    kinds_seen, kind_parts = set(), 0
    for part in parts:
        kinds = {_KIND.get(m.group(0).lower(), m.group(0).lower()) for m in _DEVICE.finditer(part)} - {"stand"}
        if kinds - kinds_seen:
            kind_parts += 1
        kinds_seen |= kinds
    if kind_parts >= 2:
        return True
    # No separator but two models of two kinds ("Dynaudio BM15 Studio
    # Monitors Hafler P4000 Power Amp").
    kinds = {_KIND.get(m.group(0).lower(), m.group(0).lower()) for m in _DEVICE.finditer(t)} - {"stand"}
    return len(_model_tokens(t)) >= 2 and len(kinds) >= 2


def is_lot(title: Optional[str]) -> bool:
    return bool(title and _LOT_RE.search(title))


_PART_FOR = re.compile(
    r"\b(?:cables?|cords?|case|mount|shock ?mount|clip|tool|wrench|transformers?|tubes?|valves?|knobs?|"
    r"power suppl(?:y|ies)|psu|capsules?|grilles?|foam|windscreen|manual|schematics?|adapter|bracket|"
    r"rack ears?|faceplate|meters?|pcbs?|boards?|pots?|switch|jacks?|cover|bag|spider|screws?|feet|"
    r"lamp|bulb|fuse|parts?|motor|belt|head ?stack|pinch roller|remote|box|panels?|buttons?|"
    r"decals?|badges?|stickers?|labels?|faders?|caps?|dust covers?)\b.{0,40}?\b(?:for|fits|from|compatible with)\b",
    re.I)
_PART_WORDS = re.compile(r"\b(?:input|output|interstage|mic) transformers?\b|\bt4b\b|\bopto cell\b", re.I)
# Vacuum tubes sold on their own (12AX7, EL34, ECC83, 6072…) and parts named
# as the item ("GA-8000 Power Supply", "Ampex 300 VU Meter Bridge").
_TUBE_TYPES = re.compile(
    r"\b(?:12a[xtuy]7a?|12ay7|ecc8[1-3]|ecc88|e8[0-9]cc|el3[4-7]|el84|6l6\w*|6v6\w*|6ca7|kt\d{2}|ef86|ef14|"
    r"6072a?|5751|6sn7\w*|6sl7\w*|6922|7025|6267|5879|vf14|ac701|6au6|6as7|5ar4|gz3[2-4]|5y3|274b|300b|2a3|"
    r"6dj8|6bq5|6bq7|6cg7|6fq7|e88cc|cv4004|ec80\d\d|ec8[0-9]|ef8[0-9]|e180f|e288cc|pcc88|6sj7|6j5)\b"
    r"|\bgold pins?\b", re.I)
_PART_NAMED = re.compile(
    r"\b(?:power suppl(?:y|ies)|psu|meter bridge|vu meters?|capstan(?: motor)?|head ?stack|head ?block|"
    r"pinch roller|capsules?|tubes? only|valves? only|face ?plate|front panel|chassis only|pcb set|board set|"
    r"\(part\)|part only|grilles?|speaker grill|voice coils?|replacement diaphragm|"
    r"tube set|tube kit|valve set|tube replacement|retube kit|transformers?|part:|part #|part number|"
    r"insert jack|input jack|output jack|channel strip board|replacement (?:board|card|module)|card only|"
    r"knobs?|lamps?|bulbs?|rack ears?|input panel|break-?in panel|push ?button switch|switch caps?|"
    r"owners? manual|user manual|service manual|manuals?|brochure|schematics?|catalog|poster|"
    r"parts (?:original|lot|only|unit)|spare parts|parts\s*$)\b", re.I)
# "comes with" wording — not "&", which also joins words in a part's own name
# ("Microphone & Line Input Panel").
_WITH = re.compile(r"\b(?:with|incl\w*|plus|comes with)\b|\bw/", re.I)


def _is_named_part(title: str) -> bool:
    """A tube or part that is the item itself — not one that comes with it
    ("Neumann U67 with power supply" is the mic)."""
    for pattern in (_TUBE_TYPES, _PART_NAMED):
        m = pattern.search(title)
        if m and not _WITH.search(title[:m.start()]):
            # "Telefunken ELA M 251 tube microphone" names a tube type only in
            # passing; a tube listing doesn't call itself a microphone etc.
            if (pattern is _TUBE_TYPES and _MAIN_GEAR.search(title)
                    and not re.search(r"\b(?:vacuum tubes?|triode|pentode|nos tubes?|matched (?:pair|quad))\b", title, re.I)):
                continue
            return True
    return False


@functools.lru_cache(maxsize=100_000)
def is_partial(title: Optional[str]) -> bool:
    if not title:
        return False
    # "Swivel mount cable for a U87", "Sowter LA-2A output transformer",
    # "Allen wrench for EL8 Distressor": a part made for the model, not the model.
    if _PART_FOR.search(title) or _PART_WORDS.search(title) or _is_named_part(title):
        return True
    for m in _PARTIAL_RE.finditer(title):
        before = title[:m.start()].lower().rstrip()
        # "U87 with capsule" / "mic w/ head" / "body and capsule" describe a
        # complete unit, not a part sold on its own.
        if m.group(0).lower().startswith(("capsule", "head")) and before.endswith(("with", "w/", "and", "+", "&")):
            continue
        return True
    return False


def needs_repair(*texts: Optional[str]) -> bool:
    return any(t and _REPAIR_RE.search(t) for t in texts)


# Approximate — Kijiji prices are Canadian dollars; comparisons (typical
# price, deals, lowest price) are done in USD.
CAD_TO_USD = 0.73


def parse_price(price: Optional[str]) -> Optional[float]:
    """Numeric price in USD (Canadian "C$…"/"CAD" prices converted)."""
    if not price:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", price)
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    if re.search(r"C\$|CAD", price):
        value *= CAD_TO_USD
    return value


def display_price(price: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(price to show, original) — Canadian prices are shown in US dollars
    ("~$110"), with the seller's own "C$150" kept alongside."""
    if price and re.search(r"C\$|CAD", price):
        value = parse_price(price)
        if value:
            return f"~{format_price(value)}", price
    return price, None


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def source_family(url: Optional[str]) -> str:
    host = urlparse(url or "").netloc.lower()
    for fam in ("facebook", "craigslist", "reverb", "ebay", "vintageking", "guitarcenter",
                "sweetwater", "gearspace", "groupdiy", "thegearpage", "audiokarma"):
        if fam in host:
            return fam
    return host or "unknown"


def normalize_title(title: Optional[str]) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def dup_key(title: Optional[str], price: Optional[str]) -> Optional[str]:
    """Same wording + same price = same item cross-posted (e.g. a Facebook
    group post and the seller's Craigslist ad). Needs a price and a title
    long enough not to collide by chance."""
    norm = normalize_title(title)
    value = parse_price(price)
    if value is None or len(norm) < 12:
        return None
    return f"{norm}|{value:.0f}"


# Things that share a word with audio gear but aren't: air compressors,
# kitchen/cement mixers, computer/baby monitors, bike racks… Applied to every
# source on top of your own exclude words.
_NOT_AUDIO = re.compile(
    r"\b(?:air[\s-]?compressors?|pancake|psi|\d+\s?(?:gal|gallon)s?\b|tire inflator|jump starter|pneumatic|"
    r"nail(?:er| gun)|brad nailer|impact wrench|3[\s-]phase|hvac|a/?c compressor|refrigerat\w*|freezer|"
    r"porter[\s-]?cable|dewalt|craftsman|campbell hausfeld|ingersoll|bostitch|husky|ryobi|makita|milwaukee|"
    r"california air tools|kobalt|quincy|"
    r"kitchen\s?aid|stand mixer|hand mixer|cement mixer|concrete mixer|mortar mixer|dough|bread maker|blender|"
    r"baby monitor|computer monitor|gaming monitor|pc monitor|(?:lcd|led|oled|ips|4k|1080p|curved|ultrawide) monitor|"
    r"\d{2,3}\s?hz monitor|blood pressure|heart rate|"
    r"bike rack|bicycle rack|wine rack|roof rack|hitch rack|shoe rack|gun rack|drying rack|spice rack|"
    r"kitchen cabinet|bathroom cabinet|filing cabinet|file cabinet|china cabinet|curio|medicine cabinet|"
    r"inner tube|tube top|"
    r"(?:car|truck|boat|marine|rv) (?:amp|amplifier|stereo|subwoofer)|car audio)\b",
    re.I)


# Consumer/toy/media/radio items that match "mic", "microphone" etc.
_JUNK = re.compile(
    r"\b(?:karaoke|toddler|kids?|children'?s|toy|toys|xbox|playstation|ps[345]|nintendo|wii|rock band|"
    r"guitar hero|gaming (?:headset|mic|microphone)|webcam|for (?:iphone|android|phone|ipad|laptop|pc)|"
    r"phone (?:mic|microphone)|ham radio|cb radio|two[\s-]way radio|walkie|antennas?|scanner radio|"
    r"(?:reel to reel|cassette|vinyl|8[\s-]?track) (?:tapes?|music|records?)|records? lot|vinyl records?|"
    r"lp records?|album collection|cd collection|dvds?|blu-?ray|costume|halloween|party speaker|"
    r"bluetooth speaker|boombox|echo dot|alexa|smart speaker|vase|ceramic|pottery|figurine|microscopes?|"
    r"telescope|jewelry|necklace|earrings|bracelet|hair (?:ribbon|bow)|ribbon (?:bow|trim|spool)|"
    r"gift wrap|scrapbook|sewing|craft ribbon|decor|decoration|ornament|prop microphone|"
    # wireless systems that don't say "wireless", car audio, test gear
    r"wms[\s-]?\d+\w*|ism\d?|uhf|vhf|bodypack|phoenix gold|\d+(?:\.\d)?\s?(?:watts? )?rms|tube tester|"
    r"multimeter|oscilloscope|signal generator|bass trainer|metronome|"
    # hi-fi turntable parts ("Audio-Technica AT-ART9XI moving coil cartridge")
    r"phono cartridges?|cartridges?|stylus|styli|tonearms?|"
    # air / HVAC compressors
    r"r-?22|r-?410a?|\d+(?:\.\d+)?[\s-]?tons?|\d+(?:\.\d+)?\s?(?:hp|horse ?power)|head pump|pump head|"
    r"trane|carrier|lennox|goodman|copeland|rheem|bryant|hausf[ie]+ld|champion compressor|sears compressor|"
    r"compressor pump|shop compressor|garage compressor|tire compressor|sears|hrs? power|"
    # consumer / car / PA / hi-fi speakers
    r"bluetooth|helmet|bike|bicycle|car (?:speakers?|subwoofer|stereo)|subwoofer box|dd audio|kicker|"
    r"rockford|jl audio|skar|sundown audio|ultra boom|party ?box|partybox|pa system|pa speakers?|"
    r"dj speakers?|bookshelf|wharfedale|tower speakers?|floor ?standing|home audio|soundbar|sound bar|"
    r"rental only|for rent|rentals?|rent (?:only|per day|by the day)|"
    r"dryer|washer|washing machine|dishwasher|refrigerator|fridge|microwave|oven|stove|range hood|"
    r"kenmore|whirlpool|maytag|frigidaire|vacuum cleaner|lawn ?mower|chainsaw|generator)\b", re.I)

# A listing that is only an accessory: cables, stands, cases, pop filters…
# ("Neumann U87 with case" is the mic; "Microphone stand and pop filter" isn't).
_ACCESSORY_ONLY = re.compile(
    r"\b(?:cables?|cords?|snake|stands?|boom arms?|mic arms?|arms?|pop filters?|windscreens?|wind ?shields?|"
    r"blimp|dead ?cat|shock ?mounts?|clips?|cases?|road case|flight case|gig bag|bags?|covers?|"
    r"isolation pads?|foam|acoustic panels?|adapters?|holders?|straps?|mounts?|brackets?|rack ears|"
    r"rack rails|rack shelf|shelf|rack trays?|trays?|patch cables?|batteries|battery|pouch(?:es)?|"
    r"suspensions?|desktop enclosure|(?:usb|adat|dante|madi|option|expansion)(?:\s*/\s*\w+)?\s+cards?|"
    r"goosenecks?|gooseneck module|(?<!sub )(?<!sub-)woofers?|tweeters?|compression drivers?|speaker drivers?|speaker cones?|re-?cone kits?|diaphragm kits?)\b", re.I)
_MAIN_GEAR = re.compile(
    r"\b(?:microphones?|mics?|preamps?|pre-amps?|compressors?|limiters?|interfaces?|mixers?|consoles?|"
    r"monitors?|speakers?|recorders?|decks?|equalizers?|eqs?|amps?|amplifiers?|receivers?|transmitters?|"
    r"processors?|channel strips?|reverbs?|delays?|synths?|headphones?)\b", re.I)


_SAYS_SOLD = re.compile(r"^\W*(?:sold|unavailable|no longer available|sale pending|pending)\s*(?:[-:!|/–—]|$)|^\W*(?:unavailable|sale pending)\b|\(\s*(?:sold|unavailable|pending)\s*\)|"
                        r"\bsold out\b|\bhas been sold\b|\b(?:sorry,? )?sold\W*$", re.I)


def title_says_sold(title: Optional[str]) -> bool:
    """"(UNAVAILABLE) API 550a Pair", "SOLD - Neve 1073", "Sony C-38B …
    Sorry, Has Been Sold": the seller edited the title instead of removing
    the ad."""
    return bool(_SAYS_SOLD.search(title or ""))


@functools.lru_cache(maxsize=100_000)
def is_accessory_only(title: Optional[str]) -> bool:
    t = title or ""
    # "HOSA 15 ft. XLR-F to DB25": a cable, named by its length and plugs.
    if re.search(r"\b\d+\s?(?:ft|feet|foot|m)\b\.?", t, re.I) and re.search(r"\b(?:xlr|db-?25|trs|ts|rca|1/4|bantam|tt)\b", t, re.I) \
            and not _MAIN_GEAR.search(t) and not re.search(r"\bpatch ?bay\b", t, re.I):
        return True
    # "RCA BK-6B Clamp Part No 210221": a numbered spare part, whatever it fits.
    # "C414 Original Case Shock Mount Windscreen Accessory": says so itself.
    if re.search(r"\baccessor(?:y|ies)\b", t, re.I) and _ACCESSORY_ONLY.search(t) \
            and not re.search(r"\bwith\b|\bw/|\bincl|\bplus\b|&|,", t, re.I):
        return True
    if re.search(r"\bpart\s*(?:no\.?|number|#)\s*\d|\b(?:clamp|yoke|bracket|grille?|knob|switch)\s+(?:part|assembly)\b", t, re.I):
        return True
    m = _ACCESSORY_ONLY.search(t)
    if not m:
        return False
    before, after = t[:m.start()], t[m.end():]
    joiner = r"(?:\bwith\b|\bw/|\bw\b|\bincl\w*|\bcomes with\b|\bplus\b|\+|&)"
    # "Shure QLXD24 system w/ handheld mic, case", "Revox A77 recorder w/stand":
    # the accessory is extra, the listing is the gear.
    # Also a list of gear: "Apollo Twin X, Shure SM7B, M50x, Mic Arm",
    # "Blue microphone and Blue boom arm", "SM7B / Rode PSA1 arm".
    # (years and decades like "1930s / 20s" aren't model numbers)
    real_models = {m for m in _model_tokens(before) if not re.fullmatch(r"(?:19|20)?\d0|(?:19|20)\d\d", m)}
    if re.search(joiner, before, re.I) or (re.search(r",|\band\b|\s/\s|\||–", before) and real_models):
        return False
    # "Gator rack case with Lexicon PCM 70": gear comes with it.
    # (but "Boom Arm with Pop Filter - Mic Arm": "mic" there describes an accessory)
    gear_after = _MAIN_GEAR.search(after)
    if gear_after and (_ACCESSORY_ONLY.match(after[gear_after.end():].lstrip())
                       or not after[:gear_after.start()].strip()):  # "Boom Arm Microphone…"
        gear_after = None
    if re.search(joiner, after, re.I) and (gear_after or _model_tokens(after)
                                           or canonical_brand(after)):
        return False
    # "Pop filter for mic", "Case for Neumann U87": made for the gear.
    if re.match(r"\s*(?:\([^)]*\)\s*)?(?:for|fits)\b", after, re.I):
        return True
    # "Microphone stand", "XLR microphone cables", "Monitor isolation pads",
    # "Boom Arm Microphone Broadcast": the gear word only describes the accessory.
    return not gear_after


@functools.lru_cache(maxsize=100_000)
def is_not_audio(title: Optional[str]) -> bool:
    """True for listings that only share a word with audio gear (air
    compressors, toys, karaoke machines, ham radio, tapes/records…)."""
    t = title or ""
    if _JUNK.search(t):
        return True
    # "Viair compressors", "aAir Compressor": "air compressor" even inside a word.
    if _NOT_AUDIO.search(t) or re.search(r"air[\s-]?compressor|air tank|viair", t, re.I):
        return True
    # Guitars themselves ("Vintage V72 Reissued Electric Guitar") — but not a
    # mic or preamp that mentions recording guitar.
    if (re.search(r"\b(?:electric|acoustic|bass|semi-hollow|hollow ?body|lh|left[\s-]handed) guitars?\b|\bguitar (?:body|neck)\b", t, re.I)
            and not re.search(r"\b(?:mic|mics|microphones?|preamps?|pre-amps?|di|d\.i\.|amps?|amplifiers?|cabs?|cabinets?|pedals?|interfaces?|recording|tracking)\b", t, re.I)):
        return True
    # A bare "Compressor" / "Mixer" / "Monitor" with nothing else to go on
    # (typical of air compressors on OfferUp and Marketplace).
    return bool(re.fullmatch(r"\W*(?:an?\s+)?(?:compressors?|mixers?|monitors?|racks?)(?:\s+\w+){0,2}?\W*", t.strip(), re.I)
                and not re.search(r"\b(?:audio|studio|rack ?mount|stereo|mic|vocal|channel|limiter|opto|tube|fet|vca|dbx|api|ssl|neve|la-?2a|1176)\b", t, re.I))


# Wireless systems often only give the model: Shure BLX/GLXD/QLXD/ULXD/SLXD/
# PGX/SVX/PSM, Sennheiser ew 100/300/500, EW-D/EW-DX, XSW, AVX, Audio-Technica
# ATW / System 10 — excluded along with the word "wireless".
_WIRELESS_MODELS = re.compile(
    r"\b(?:blx|glxd?|qlxd?|ulxd?|slxd?|pgxd?|svx|psm\s?\d{3,4}|axient|"
    r"ew[\s-]?(?:100|300|500|d|dx|g[1-4])|xsw|avx|atw-?\w*|system 10|(?:true )?diversity receivers?|"
    r"em-?\d{3,4}\w*|sk-?\d{3,4}\w*)(?:[\d/-]\w*)?\b", re.I)


def exclude_match(title: Optional[str], exclude_words: list[str]) -> bool:
    from scrapers.base import keyword_match
    words = [str(w) for w in (exclude_words or []) if str(w).strip()]
    if not words:
        return False
    if "wireless" in (w.lower() for w in words) and _WIRELESS_MODELS.search(title or ""):
        return True
    return keyword_match(title or "", words)


# Words that describe a kind of gear rather than a particular one. Search
# terms made only of these ("mic", "condenser", "audio interface") match
# almost everything on the big marketplaces.
_GENERIC_WORDS = {
    "mic", "mics", "microphone", "microphones", "ribbon", "condenser", "dynamic", "tube", "valve",
    "preamp", "preamps", "pre", "amp", "compressor", "compressors", "limiter", "mixer", "console",
    "interface", "monitor", "monitors", "studio", "audio", "eq", "equalizer", "vintage", "pro",
    "recording", "rack", "rackmount", "outboard", "gear", "channel", "strip", "di", "box", "speaker",
    "speakers", "unit", "machine", "tape", "patchbay", "patch", "bay", "500", "series", "lunchbox",
    "stereo", "mono", "pair", "old", "german", "russian", "soviet", "japanese", "broadcast", "sdc",
    "ldc", "fet", "shotgun", "lavalier", "small", "large", "diaphragm",
}
# Brands whose everyday gear floods broad searches (podcast mics, USB
# interfaces, PA) — a brand match alone doesn't make these interesting.
_CONSUMER_BRANDS = {"blue", "samson", "numark", "bose", "pioneer", "technics", "boss", "zoom",
                    "m-audio", "peavey", "behringer", "fender", "evh", "marshall", "krk", "mackie",
                    "yamaha", "jbl", "presonus", "alesis", "rode", "audio-technica", "focusrite", "tascam",
                    "sony", "roland", "korg", "steinberg", "native-instruments", "arturia", "iloud", "kali",
                    "art", "cad", "lewitt", "warm-audio"}
_VINTAGE_SIGNS = re.compile(
    r"\b(?:vintage|antique|tube|valve|19[2-8]\d|[2-8]0'?s|nos|germany|german|ussr|soviet|west german|"
    r"telefunken|broadcast|rca|western electric|collins|gates|altec|langevin|ampex|restored|serviced)\b", re.I)


# Home stereo (hi-fi) gear — preamps and amps, but not studio gear.
_HIFI = re.compile(
    r"\b(?:tuner|am/?fm|fm stereo|receiver|phono|turntable|record player|integrated amp\w*|"
    r"stereo (?:power )?amp\w*|power amp\w* pair|hi-?fi|audiophile|home (?:theater|stereo)|"
    r"mcintosh|marantz|nikko|dynaco|sansui|kenwood|onkyo|denon|harman kardon|fisher|luxman|"
    r"sherwood|realistic|nad|rotel|cambridge audio|pioneer sx|sony str|klipsch|polk|cerwin[- ]vega)\b", re.I)


def is_generic_term(term: str) -> bool:
    words = re.findall(r"[a-z0-9]+", str(term).lower())
    return bool(words) and all(w in _GENERIC_WORDS for w in words)


@functools.lru_cache(maxsize=8)
def _specific_terms(terms: tuple) -> list:
    return [t for t in terms if not is_generic_term(t)]


def is_relevant(title: Optional[str], price: Optional[str], terms: tuple) -> bool:
    """Keeps a listing found only through a broad word ("mic", "compressor")
    when it looks like studio gear: a pro-audio brand, vintage clues, or a
    price of $200+. Anything matching a specific term always passes."""
    from scrapers.base import keyword_match
    t = title or ""
    specific = _specific_terms(terms)
    if specific and keyword_match(t, specific):
        return True
    if _HIFI.search(t):
        return False
    brand = canonical_brand(t)
    if brand and brand not in _CONSUMER_BRANDS:
        return True
    # Vintage clues — but a "vintage" consumer speaker or a "tube" in an
    # air compressor ad isn't studio gear (those are filtered separately).
    return bool(_VINTAGE_SIGNS.search(t))


def wants_pedals(cfg: dict) -> bool:
    """Pedals are left out unless one of your search / Telex terms asks for them."""
    terms = list(cfg.get("keywords") or []) + list(cfg.get("telex_list") or [])
    return any("pedal" in str(t).lower() for t in terms)


def drop_excluded(listings: list, cfg: dict, terms: Optional[list] = None) -> list:
    """Drops non-audio items, accessory-only listings (setting), exclude
    words — and, when the run's search terms are given (scheduled/hourly/
    manual scrapes, not a live search), listings found only through a broad
    word that don't look like studio gear (see is_relevant)."""
    words = cfg.get("exclude_words") or []
    hide_acc = cfg.get("hide_accessories", True)
    term_key = tuple(terms) if terms and len(terms) > 1 and cfg.get("relevance_check", True) else None
    hide_pedals = not wants_pedals(cfg)
    return [l for l in listings if not is_not_audio(l.title)
            and not (hide_pedals and item_form(l.title) == "pedal")
            and not (hide_acc and is_accessory_only(l.title))
            and not (words and exclude_match(l.title, words))
            and not (term_key and not is_relevant(l.title, l.price, term_key))]


# --- Typical price / deal context -------------------------------------------

# What form an item takes. A "UAFX LA-2A pedal" ($150) or an LA-2A kit is
# not a Teletronix LA-2A ($3,800) even though the model name matches, so
# these become part of the model's identity and of its price lookups.
_FORMS = (
    # Universal Audio's pedals are often listed without the word "pedal":
    # "Teletronix LA-2A Studio Compressor", "1176 Studio Compressor",
    # "Golden Reverberator", "Lion '68"…
    ("pedal", re.compile(
        r"\b(?:pedal|stomp ?box|uafx|(?:la-?2a|1176|teletronix)\b.{0,30}\bstudio compressor|"
        r"golden reverberator|starlight echo|galaxy '?74|astra modulation|max preamp|"
        r"(?:lion|ruby|dream|woodrow|enigmatic|anti|evermore|knuckles)\s*'?\d{2}|orange \w+ amp emulat\w*|"
        # Guitar/bass effects, named by type or by a pedal maker.
        r"overdrive|fuzz|distortion|wah|chorus|flanger|phaser|looper|tremolo|octaver|tube ?screamer|"
        r"whammy|bass (?:compressor|preamp|di pedal|overdrive)|guitar (?:compressor|effects?)|"
        r"mxr|electro-?harmonix|ehx|jhs|keeley|walrus audio|strymon|earthquaker|wampler|fulltone|"
        r"chase bliss|meris|source audio|xotic|catalinbread|dunlop|zvex|jackson audio|"
        r"boss\s+(?!br|dr|sp|mc|tr|ad|rc-?505|gt-?1000)[a-z]{2,3}-?\d{1,3}[a-z]*)\b", re.I)),
    ("plugin", re.compile(r"\b(?:plug-?ins?|software|licen[sc]e|vst|aax|download code|"
                          r"uad-?2 (?:plug|powered plug))\b|\(download\)|\bdigital download\b", re.I)),
    ("kit", re.compile(r"\b(?:diy|pcb|bare boards?|unbuilt|unassembled|(?:partial|build|clone|diy|project) kit|kit (?:build|form|only))\b", re.I)),
    ("500", re.compile(r"\b500[\s-]?series\b|\bapi[\s-]?500\b|\b500 (?:module|format)\b|\(500\)|\b(?:lunchbox|vpr)\b", re.I)),
)


_UA_OWN = {"universal-audio", "teletronix", "urei"}


@functools.lru_cache(maxsize=100_000)
def item_form(title: Optional[str]) -> Optional[str]:
    """'pedal', 'plugin', 'kit', '500' or None (a regular unit)."""
    for name, pattern in _FORMS:
        m = pattern.search(title or "")
        if not m:
            continue
        # "Apollo x8p + 33 UAD Plug-ins", "interface with plugins": software
        # that comes with the hardware.
        if name == "plugin" and re.search(r"(?:\+|&|\bwith\b|\bw/|\bincl\w*|\band\b)[^+&]{0,25}$",
                                          (title or "")[:m.start()], re.I):
            continue
        return name
    # "Universal Audio Empirical Labs Distressor", "UAD Neve 1073": UA's
    # software versions of other makers' gear (UA doesn't build those).
    text = (title or "").lower()
    first = canonical_brand(title)
    if first == "universal-audio":
        later = canonical_brand(_BRAND_RE.sub("", text, count=1))
        if later and later not in _UA_OWN:
            return "plugin"
    return None


# Known pro-audio brands and product lines, found anywhere in a title
# ("UA", "UAD", "Universal Audio", "Apollo" are all Universal Audio;
# "Distressor" is Empirical Labs). Much more reliable than "the first word
# before the model number", which split one item into several "models"
# ("Custom LA-2A", "Refurbished MD 421", "Patchbay PB-48").
_BRAND_ALIASES = {
    "universal-audio": ["universal audio", "uad", "ua", "apollo", "uafx", "universal-audio"],
    "teletronix": ["teletronix"], "urei": ["urei", "u.r.e.i"],
    "empirical-labs": ["empirical labs", "empirical", "distressor", "fatso"],
    "focusrite": ["focusrite", "scarlett", "clarett", "rednet", "isa"],
    "neumann": ["neumann"], "sennheiser": ["sennheiser"], "shure": ["shure"], "akg": ["akg"],
    "beyerdynamic": ["beyerdynamic", "beyer"], "electro-voice": ["electro-voice", "electrovoice", "ev"],
    "audio-technica": ["audio-technica", "audio technica", "audiotechnica"],
    "rode": ["rode", "røde"], "blue": ["blue microphones", "blue yeti", "yeti"],
    "telefunken": ["telefunken"], "schoeps": ["schoeps"], "dpa": ["dpa"], "royer": ["royer"],
    "coles": ["coles"], "aea": ["aea"], "gefell": ["gefell"], "oktava": ["oktava"], "lomo": ["lomo"],
    "soyuz": ["soyuz"], "wunder": ["wunder"], "flea": ["flea"], "mojave": ["mojave"],
    "warm-audio": ["warm audio"], "avantone": ["avantone"], "lauten": ["lauten"], "peluso": ["peluso"],
    "stam": ["stam audio", "stam"], "audioscape": ["audioscape", "audio-scape"],
    "golden-age": ["golden age"], "chandler": ["chandler"], "neve": ["neve", "ams neve"],
    "api": ["api"], "ssl": ["ssl", "solid state logic"], "manley": ["manley"],
    "avalon": ["avalon"], "tube-tech": ["tube-tech", "tube tech"], "pultec": ["pultec"],
    "great-river": ["great river"], "a-designs": ["a-designs", "a designs"], "vintech": ["vintech"],
    "heritage-audio": ["heritage audio"], "rupert-neve": ["rupert neve"], "bae": ["bae"],
    "dbx": ["dbx"], "drawmer": ["drawmer"], "tc-electronic": ["tc electronic", "t.c. electronic"],
    "lexicon": ["lexicon"], "eventide": ["eventide"], "yamaha": ["yamaha"], "roland": ["roland"],
    "tascam": ["tascam", "portastudio"], "teac": ["teac"], "otari": ["otari"], "ampex": ["ampex"],
    "studer": ["studer"], "revox": ["revox"], "mackie": ["mackie"], "soundcraft": ["soundcraft"],
    "allen-heath": ["allen & heath", "allen and heath", "allen heath"], "behringer": ["behringer"],
    "presonus": ["presonus", "eris", "audiobox", "studiolive"], "motu": ["motu"], "rme": ["rme"],
    "apogee": ["apogee"], "antelope": ["antelope audio", "antelope"], "lynx": ["lynx"],
    "genelec": ["genelec"], "adam": ["adam audio"], "krk": ["krk", "rokit"], "jbl": ["jbl"],
    "focal": ["focal"], "dynaudio": ["dynaudio"], "barefoot": ["barefoot"],
    "hedd": ["hedd"], "kali": ["kali audio"], "iloud": ["ik multimedia", "iloud"],
    "radial": ["radial"], "cloudlifter": ["cloudlifter", "cloud microphones"], "art": ["art pro audio"],
    "summit": ["summit audio"], "klark-teknik": ["klark teknik"],
    "capi": ["capi"], "hairball": ["hairball"], "sound-skulptor": ["sound skulptor"],
    "purple-audio": ["purple audio"], "inward-connections": ["inward connections"],
    "retro": ["retro instruments"], "thermionic": ["thermionic culture"], "spl": ["spl"],
    "elysia": ["elysia"], "maag": ["maag"], "dangerous": ["dangerous music"], "crane-song": ["crane song"],
    "mesa": ["mesa boogie"], "fender": ["fender"], "marshall": ["marshall"], "evh": ["evh"],
    "boss": ["boss"], "zoom": ["zoom"], "steinberg": ["steinberg"], "m-audio": ["m-audio", "m audio"],
    "native-instruments": ["native instruments"], "arturia": ["arturia"], "moog": ["moog"],
    "korg": ["korg"], "sony": ["sony"], "rca": ["rca"], "altec": ["altec"], "western-electric": ["western electric"],
    "langevin": ["langevin"], "gates": ["gates"], "collins": ["collins"], "ampeg": ["ampeg"],
    "furman": ["furman"], "samson": ["samson"], "peavey": ["peavey"], "crown": ["crown"], "qsc": ["qsc"],
    "bose": ["bose"], "numark": ["numark"], "pioneer": ["pioneer"], "technics": ["technics"],
    "aphex": ["aphex"], "audix": ["audix"], "earthworks": ["earthworks"], "bittree": ["bittree"],
    "symetrix": ["symetrix"], "rane": ["rane"], "midas": ["midas"], "ams": ["ams"], "fairchild": ["fairchild"],
    "gyraf": ["gyraf"], "tree-audio": ["tree audio"], "undertone": ["undertone audio", "unfairchild"],
    "black-lion": ["black lion"], "iron-age": ["iron age"], "mercury": ["mercury recording"],
    "slate": ["slate digital", "slate"], "trident": ["trident"], "harrison": ["harrison"],
    "daking": ["daking"], "true-systems": ["true systems"], "millennia": ["millennia"],
    "grace": ["grace design"], "benchmark": ["benchmark"], "prism": ["prism sound"], "lavry": ["lavry"],
    "burl": ["burl"], "dangerous-music": ["dangerous"], "sontec": ["sontec"], "massenburg": ["gml", "massenburg"],
    "summit-audio": ["summit"], "joemeek": ["joemeek", "joe meek"], "tl-audio": ["tl audio"],
    "focusrite-red": ["focusrite red"], "emt": ["emt"], "akai": ["akai"], "fostex": ["fostex"],
    "mci": ["mci"], "scully": ["scully"], "3m": ["3m m79", "3m m56"], "kerwax": ["kerwax"],
    "bock": ["bock audio", "bock"], "brauner": ["brauner"], "microtech-gefell": ["microtech"], "josephson": ["josephson"], "sanken": ["sanken"], "lewitt": ["lewitt"],
    "austrian-audio": ["austrian audio"], "cad": ["cad equitek"], "sony-c": ["sony c-37", "sony c37", "sony c-38", "sony c38"],
    "altec-lansing": ["altec lansing"], "atc": ["atc"], "pmc": ["pmc"],
    "amek": ["amek"],
}
_BRAND_DISPLAY = {"universal-audio": "universal audio", "empirical-labs": "empirical labs"}
_ALIAS_TO_BRAND = {alias: canon for canon, aliases in _BRAND_ALIASES.items() for alias in aliases}
# One pattern for every alias (longest first) — a single scan per title.
_BRAND_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in sorted(_ALIAS_TO_BRAND, key=len, reverse=True))
    + r")(?![a-z0-9])")


@functools.lru_cache(maxsize=100_000)
def canonical_brand(title: Optional[str]) -> Optional[str]:
    """The first known brand/product line in the title, before any "for" /
    "fits" (in "Cable for Neumann U87" the brand is whoever made the cable)."""
    text = (title or "").lower()
    cut = re.search(r"\b(?:for|fits|compatible with|replacement)\b", text)
    head = text[:cut.start()] if cut else text
    m = _BRAND_RE.search(head)
    return _ALIAS_TO_BRAND[m.group(1)] if m else None


def _canonical_model(brand: str, model: str) -> str:
    # Avalon writes the same unit as "VT-737SP", "VT 737 SP", "737sp", "737".
    if brand == "avalon":
        model = re.sub(r"^vt", "", model)
        model = re.sub(r"sp$", "", model)
    return model


_CLONE_WORD = r"(?:clone|replica|copy|style|inspired|tribute|based)"


# Makers whose products are mostly copies of classic gear, and say so by
# naming the original ("Stam SA-432D+ Sontec 432D9", "Stam Pultec MEQP-1A+").
_CLONE_MAKERS = {"stam", "warm-audio", "golden-age", "audioscape", "lindell", "klark-teknik", "behringer",
                 "drip", "heritage-audio", "black-lion", "chameleon", "soundskulptor", "sound-skulptor",
                 "wes-audio", "korneff", "aml", "capi", "hairball", "rupert-neve-designs"}


def strip_clone_reference(title: Optional[str]) -> str:
    """Takes out the gear a clone imitates, so "Warm Audio EQP-WA (Pultec
    EQP-1A clone)" is valued as the EQP-WA and "Clone Neve 1073 Chameleon
    Labs 7602" as the 7602, never as the real Pultec / Neve."""
    text = title or ""
    maker = canonical_brand(text)
    if maker in _CLONE_MAKERS:
        # Drop every other brand name (and the model right after it).
        def drop(m):
            return "" if _ALIAS_TO_BRAND.get(m.group(1).lower()) not in (None, maker) else m.group(0)
        text = re.sub(r"(?i)(?<![a-z0-9])(" + "|".join(re.escape(a) for a in sorted(_ALIAS_TO_BRAND, key=len, reverse=True))
                      + r")(?![a-z0-9])(?:[\s-]+[a-z]*\d[\w+-]*)?", drop, text)
    if not re.search(rf"\b{_CLONE_WORD}\b", text, re.I):
        return text
    # "(Pultec EQP-1A Tube Equalizer clone)", "[U47 style]"
    text = re.sub(rf"[(\[][^)\]]*\b{_CLONE_WORD}\b[^)\]]*[)\]]", " ", text, flags=re.I)

    def brandish(word: str) -> bool:
        return bool(canonical_brand(word))
    tokens = re.findall(r"\S+", text)
    drop = set()
    for i, tok in enumerate(tokens):
        if not re.fullmatch(rf"{_CLONE_WORD}[.,:;!-]*", tok, re.I):
            continue
        drop.add(i)
        nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if nxt in ("of", "to"):  # "clone of Neve 1073", "tribute to the 1176"
            j = i + 2
            if j < len(tokens) and tokens[j].lower() in ("a", "an", "the"):
                drop.add(j - 1)
                j += 1
            drop.update(k for k in (i + 1, j, j + 1) if k < len(tokens))
            if j < len(tokens) and not brandish(tokens[j]):
                drop.discard(j + 1)
        elif i == 0 or re.fullmatch(r"a|an|vintage|classic|retro|old|old-school|\d0'?s", tokens[i - 1].lower()):
            # "Clone Neve 1073 ...", "Vintage Style RCA 77": the imitated gear follows.
            j = i + 1
            if j < len(tokens):
                drop.add(j)
                if brandish(tokens[j]) and j + 1 < len(tokens):
                    drop.add(j + 1)
        else:
            # "Neve 1073 clone", "U47 style": the imitated gear comes before.
            j = i - 1
            drop.add(j)
            if j - 1 >= 0 and brandish(tokens[j - 1]):
                drop.add(j - 1)
    return " ".join(t for k, t in enumerate(tokens) if k not in drop)


@functools.lru_cache(maxsize=100_000)
def model_key(title: Optional[str]) -> Optional[str]:
    """The first model-number-looking token in a title, normalized and
    prefixed with the brand ("neumann:u87ai", "sennheiser:421",
    "tascam:portastudio414") — so listings of the same model group together
    even when written "WA-47" or "WA 47". A pedal/kit/plugin/500-series
    version gets its own key ("universal:la2a|pedal")."""
    title = strip_clone_reference(title)
    found = _model_match(title)
    if not found:
        return None
    brand, _, model = found[0].rpartition(":")
    known = canonical_brand(title)
    if known:
        # "Sennheiser 421" came back as brand "sennheiser" + model "421";
        # a known brand as the "model's letters" means the same thing.
        brand = known
    # UA's modern reissues are often titled "Teletronix LA-2A ... Universal
    # Audio"; those aren't the 1960s originals (half the price).
    reissue = False
    # A plain "Teletronix LA-2A" is almost always a reissue too: only one
    # that says vintage / original / 1960s is valued as an original.
    if brand == "teletronix" and not re.search(r"\b(?:19[56]\d'?s?|original|vintage|orig)\b", title or "", re.I):
        brand, reissue = "universal-audio", True
    model = _canonical_model(brand, model)
    variant = _model_variant(title, found[1])
    if reissue and variant == "reissue":
        variant = None
    if variant:
        model = f"{model}{variant}"
    key = f"{brand}:{model}" if brand else model
    form = item_form(title)
    return f"{key}|{form}" if form else key


_VARIANT = re.compile(r"[\s-]+(vr|xls|xlii|xl|eb|ai|reissue|mk\s?(?:i{1,3}|[2-5])|mark\s?(?:i{1,3}|[2-5])|ii|iii)\b", re.I)


def _model_variant(title: Optional[str], query: str) -> Optional[str]:
    """The version written after the model as its own word — "C12 VR",
    "C414 XLS" / "XLII", "U87 Ai", "Mk II" — which can change the price a
    lot (a C12 VR reissue vs. a vintage C12)."""
    shown = (query or "").split()[-1] if query else ""
    if not shown or not title:
        return None
    # (Built a new pattern per title before — thousands of regex compiles
    # every time the price history was rebuilt.)
    low, needle = title.lower(), shown.lower()
    i, m = low.find(needle), None
    while i != -1 and not m:
        m = _VARIANT.match(title, i + len(needle))
        i = low.find(needle, i + 1)
    if not m:
        return None
    v = re.sub(r"\W|mark|mk", "", m.group(1).lower())
    v = {"2": "ii", "3": "iii", "4": "iv", "5": "v"}.get(v, v)
    return v


@functools.lru_cache(maxsize=100_000)
def model_query(title: Optional[str]) -> Optional[str]:
    """The same model as readable search words ("Sennheiser 421",
    "Tascam Portastudio 414", "Universal LA-2A pedal") — used to look the
    model up on Reverb/eBay."""
    found = _model_match(title)
    if not found:
        return None
    query = found[1]
    known = canonical_brand(title)
    if known:
        old_brand = found[0].rpartition(":")[0]
        words = query.split()
        if old_brand and words and words[0].lower() == old_brand:
            words = words[1:]
        display = _BRAND_DISPLAY.get(known, known.replace("-", " "))
        if not " ".join(words).lower().startswith(display):
            words = display.split() + words
        query = " ".join(words)
    variant = _model_variant(title, found[1])
    if variant:
        query = f"{query} {variant}"
    form = item_form(title)
    return f"{query} {'500 series' if form == '500' else form}" if form else query


def is_ordinal(digits: str, suffix: str) -> bool:
    """"10th", "2nd", "4th" ("10th Anniversary", "4th Gen") — not a model number."""
    if not digits.isdigit() or suffix not in ("st", "nd", "rd", "th"):
        return False
    n = int(digits)
    want = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return suffix == want


def _model_match(title: Optional[str]) -> Optional[tuple[str, str]]:
    found = _model_match_inner(title)
    if found:
        return found
    # "Universal Audio 1176", "Neve 1073 DPA": a bare model number right after
    # a known brand (the word before it may be "Audio", which isn't a brand).
    brand = canonical_brand(title)
    if brand:
        text = (title or "").lower()
        for m in re.finditer(r"(?<![\w$.,/])(\d{3,4}[a-z]{0,3})(?![\w/])", text):
            tok = m.group(1)
            # Years and decades ("1968", "2010s" — Reverb adds them to titles)
            # aren't model numbers.
            num = re.match(r"\d+", tok).group(0)
            if (re.fullmatch(r"(?:19|20)\d\d'?s?", tok) or _COUNT_WORD_AFTER.match(text[m.end():])
                    or is_ordinal(num, tok[len(num):])):
                continue
            return f"{brand}:{tok}", f"{brand.replace('-', ' ')} {title[m.start():m.end()]}"
    return None


def _model_match_inner(title: Optional[str]) -> Optional[tuple[str, str]]:
    raw = title or ""
    text = raw.lower()
    # "LOMO 19A9", "19-A19": number-letter-number models (common on Soviet gear).
    m = re.search(r"\b(\d{1,3})-?((?!x\d)[a-z]{1,2})(\d{1,3})\b", text)
    if m:
        prior = [w for w in re.findall(r"[a-z][a-z0-9]+", text[:m.start()])
                 if w not in _NOT_BRAND_WORDS and w not in _NOT_MODEL_WORDS]
        if prior:
            model = "".join(m.groups())
            return f"{prior[0]}:{model}", f"{prior[0]} {raw[m.start():m.end()]}"
    for m in re.finditer(r"\b([a-z]{1,15})([\s-]?)(\d{1,4})([a-z]{0,3})\b", text):
        letters, sep, digits, suffix = m.groups()
        if letters in _NOT_MODEL_WORDS or letters in _NOT_BRAND_WORDS:
            continue
        if sep == " " and is_ordinal(digits, suffix):
            continue  # "Active 10th Anniversary", "Scarlett 4th Gen"
        # "LA-2A"/"LA2A" is a model; "model 7" or "channel 8" is not — a
        # word followed by a spaced number needs at least 2 digits.
        if sep == " " and len(digits) < 2:
            continue
        # "Presonus 16", "Behringer 24": a long word and a short number is a
        # size or count, not a model ("NS 10", "KM 184", "U 87" still are).
        if sep == " " and len(digits) < 3 and len(letters) > 3 and not suffix:
            continue
        # "circa 1965", "from 1972": a year, not a model number.
        if len(digits) == 4 and 1920 <= int(digits) <= 2035 and suffix in ("", "s"):
            continue
        # "12 channel", "16 track", "8 input": a count, not a model number.
        if not suffix and _COUNT_WORD_AFTER.match(text[m.end():]):
            continue
        # Brand = first real word before the model ("Vintage Neumann U87"
        # -> neumann), so an Audioscape "LA-2A" clone and a Teletronix LA-2A
        # don't share a price range.
        prior = [w for w in re.findall(r"[a-z][a-z0-9]+", text[:m.start()])
                 if w not in _NOT_BRAND_WORDS and w not in _NOT_MODEL_WORDS and w != letters]
        brand = prior[0] if prior else ""
        model = f"{letters}{digits}{suffix}"
        if not brand and len(letters) >= 3 and sep == " ":
            # "Sennheiser 421", "Mackie 1604", "Neve 1073": the word IS the
            # brand and the number is the model.
            brand, model = letters, f"{digits}{suffix}"
        key = f"{brand}:{model}" if brand else model
        shown = raw[m.start():m.end()]
        query = f"{brand} {shown}" if brand and not shown.lower().startswith(brand) else shown
        return key, query.strip()
    # "Electro-Voice Model 664": the number after "model"/"type" is the model.
    m = re.search(r"\b(?:model|type|mod)\s*#?\s*(\d{2,4}[a-z]{0,3})\b", text)
    if m:
        prior = [w for w in re.findall(r"[a-z][a-z0-9]+", text[:m.start()])
                 if w not in _NOT_BRAND_WORDS and w not in _NOT_MODEL_WORDS]
        if prior:
            return f"{prior[0]}:{m.group(1)}", f"{prior[0]} {m.group(1)}"
    return None


_COUNT_WORD_AFTER = re.compile(
    r"[\s-]*(?:ch|chan|channels?|tracks?|inputs?|outputs?|in|out|bands?|ways?|pieces?|pcs|pack|"
    r"space|spaces|slots?|units?|strings?|keys?|watts?|w|ohms?|ft|feet|inch|in\.|mm|lbs?|x)\b",
    re.I,
)


_NOT_BRAND_WORDS = {
    "refurbished", "custom", "patchbay", "patch", "bay", "handmade", "boutique", "reissue", "b-stock",
    "bstock", "black", "silver", "white", "gold", "studio", "recording", "channel", "strip", "amplifier",
    "limiter", "equalizer", "eq", "speaker", "speakers", "headphones", "pedal", "unit", "units",
    "listing", "item", "gear", "audio", "sound", "music", "used", "excellent", "tested", "wow",
    "vintage", "new", "mint", "used", "rare", "pair", "of", "the", "nos", "excellent",
    "great", "beautiful", "classic", "original", "pro", "professional", "authentic",
    "genuine", "like", "brand", "lot", "set", "two", "three", "four", "matched", "stereo",
    "and", "with", "for", "sale", "selling", "clean", "working", "tested", "near",
    "perfect", "condition", "good", "nice", "rack", "rackmount", "free", "shipping", "open",
    "box", "demo", "blemished", "clearance", "just", "serviced", "restored", "modded",
    "microphone", "microphones", "mic", "mics", "condenser", "dynamic", "ribbon", "tube",
    "preamp", "compressor", "mixer", "console", "interface", "monitor", "monitors",
    "speaker", "speakers", "amp", "amplifier", "equalizer", "studio", "audio", "pro",
    "analog", "digital", "channel", "input", "output", "pack", "bundle", "kit", "vtg",
}


_NOT_MODEL_WORDS = {
    "and", "for", "the", "of", "to", "in", "with", "only", "or", "mk", "rev", "ch",
    "size", "pair", "pairs", "qty", "set", "lot", "year", "years", "series", "model",
    "channel", "channels", "version", "gen", "v", "vol", "no", "ft", "feet", "inch",
    "pcs", "pieces", "units", "unit", "track", "tracks", "band", "bands", "way", "ohm",
    "watt", "watts", "hz", "khz", "db", "circa", "from", "late", "early", "mid",
}


def build_price_index(rows: list[dict]) -> dict[str, float]:
    """Median price per model key, from every stored listing that has one —
    only for models seen at least 4 times so one odd listing can't set the
    'typical' price."""
    by_model: dict[str, list[float]] = {}
    for r in rows:
        key = model_key(r.get("title"))
        value = parse_price(r.get("price"))
        if key and value and quantity(r.get("title")) > 1:
            value = value / quantity(r.get("title")) if not _EACH_RE.search(r.get("title") or "") else value
        if (key and value and value >= 20 and not is_partial(r.get("title"))
                and not is_bundle(r.get("title")) and not is_not_audio(r.get("title"))
                and not needs_repair(r.get("title"), r.get("description"))):
            by_model.setdefault(key, []).append(value)
    out = {}
    for k, v in by_model.items():
        if len(v) < 4:
            continue
        v.sort()
        # Prices all over the place (p75 > 2.5x p25) mean the "model" mixes
        # different things — no solid typical price; Reverb/eBay is used instead.
        if v[(len(v) * 3) // 4] > 2.5 * v[len(v) // 4]:
            continue
        out[k] = statistics.median(v)
    return out


# --- Quantity / "each" pricing -------------------------------------------

_WORD_NUMS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10, "twin": 2}
_EACH_RE = re.compile(
    r"\beach\b|\bea\.?(?=\s|$|[,;)])|/\s?ea\b|\bper (?:mic|microphone|unit|piece|one|item|channel|pc)\b|"
    r"\bapiece\b|\ba piece\b|\bpriced (?:individually|separately)\b|"
    r"\b(?:sold|sell|buy|available) (?:separately|individually)\b|\bprice is per\b",
    re.I,
)


def quantity(title: Optional[str], description: Optional[str] = None) -> int:
    """How many units an ad is for (1 when it doesn't say): "pair" -> 2,
    "2x SM57" / "SM57 x2" / "(2)" / "Two SM57s" / "4 Sennheiser 421s" /
    "lot of 3", or "3 available" / "qty 3" / "I have 3" in the description.
    Model names that contain numbers ("Apollo x8", "Deep Six", "BP-6AA")
    aren't counts, so the title patterns are deliberately narrow."""
    t = (title or "").lower()
    d = (description or "").lower()
    if re.search(r"\b(?:matched |stereo )?pairs?\b", t) and not re.search(r"\bpairs? of (?:headphones|cables)\b", t):
        m = re.search(r"\b(\d|two|three|four)\s+pairs\b", t)
        return 2 * (int(m.group(1)) if m and m.group(1).isdigit() else _WORD_NUMS.get(m.group(1), 1) if m else 1)
    title_pats = (
        r"(?:^\W*|\()([2-9]|1\d)\s?[x×]\s+[a-z]",               # "2x SM57" (not "Lindell 17X")
        # "SM57 x2", "KM184x2" — but not I/O counts like "2x2" / "16x16".
        r"\b[a-z0-9-]*[a-wyz][a-z0-9-]*\d\s?[x×]\s?([2-9]|1\d)\b(?!\s?\d)",
        r"\(\s*([2-9]|1\d)\s*\)",                              # "(2)"
        r"\b(?:lot|set|bundle|group) of ([2-9]|1\d)\b",
        r"\bqty:?\s?([2-9]|1\d)\b",
        r"\bhave ([2-9]|1\d)\b",
        r"^\W*([2-9]|1\d)\s+(?!ch\b|channel|track|space|band|way|input|output|piece|knob|string|button|fader|pin|foot|ft\b|inch|in\b|\")[a-z]",
    )
    desc_pats = (
        r"\b([2-9]|1\d)\s+(?:available|in stock|of them|units available)\b",
        r"\b(?:i )?have ([2-9]|1\d) (?:of (?:these|them)|available|for sale)\b",
        r"\bqty:?\s?([2-9]|1\d)\b",
        r"\bquantity:?\s?([2-9]|1\d)\b",
        r"\bselling (?:all )?([2-9]|1\d)\b",
    )
    for pat in title_pats:
        for m in re.finditer(pat, t):
            # "WA-412 preamp, (4) Ernie Ball XLR cables": a count after
            # "with" / "&" / a comma, or right before an accessory, counts the
            # extras thrown in, not the gear for sale.
            before, after = t[:m.start()], t[m.end():m.end() + 40]
            if (_model_tokens(before) and re.search(r",|\bw/|\bwith\b|\+|&|\band\b|\bplus\b|\bincl", before)) \
                    or _ACCESSORY_ONLY.search(" ".join(re.split(r"\s(?:with|w/|\+|&|and|plus)\s", " " + after.lstrip(" )x×"))[0].split()[:3])):
                continue
            return int(m.group(1))
    m = re.search(r"^\W*(two|three|four|five|six|eight|ten)\s+(?!channel|track|band|way)[a-z]", t)
    if m:
        return _WORD_NUMS[m.group(1)]
    for pat in desc_pats:
        m = re.search(pat, f"{t} {d}")
        if m:
            return int(m.group(1))
    m = re.search(r"\b(?:have|selling|sell) (two|three|four|five|six)\b", d)
    if m:
        return _WORD_NUMS[m.group(1)]
    return 1


def price_basis(title: Optional[str], description: Optional[str], value: float,
                typical: Optional[float] = None) -> tuple[str, int, float]:
    """(basis, quantity, unit_price) — basis is 'single', 'each' (the price is
    per piece) or 'set' (the price covers every piece). Explicit wording
    ("$600 each", "per mic", "priced separately") decides; otherwise, for a
    multi-piece ad, a price close to ONE unit's typical price means each."""
    qty = quantity(title, description)
    text = f"{title or ''} {description or ''}"
    if qty < 2:
        return ("each", 1, value) if _EACH_RE.search(title or "") else ("single", 1, value)
    if _EACH_RE.search(text):
        return "each", qty, value
    # No wording either way: a price near ONE unit's typical price is most
    # likely per piece — flagged as a guess, not stated as fact.
    if typical and typical * 0.6 <= value <= typical * 1.35:
        return "each?", qty, value
    return "set", qty, value / qty


# --- Title-based lookup key (for listings with no model number) ------------

# Sale / condition chatter only — product words ("mixer", "12 channel",
# "condenser") are kept, they're what makes a title lookup find the item.
_TITLE_FILLER = {
    "vintage", "new", "mint", "used", "rare", "pair", "pairs", "of", "the", "a", "an", "and", "with",
    "w", "for", "sale", "selling", "great", "excellent", "good", "nice", "clean", "works", "working",
    "tested", "perfect", "condition", "shape", "shipping", "ship", "shipped", "free", "local",
    "pickup", "obo", "firm", "offer", "offers", "price", "cash", "only", "trade", "trades", "each",
    "ea", "set", "lot", "plus", "includes", "including", "in", "box", "original", "like", "near",
    "fs", "wts", "sold", "as", "is", "not", "no", "or", "to", "from", "by", "my", "this", "very",
}


def title_query(title: Optional[str]) -> Optional[str]:
    """Up to 5 meaningful words from a title ("Vintage AKG D190 dynamic mic,
    works great" -> "akg d190 dynamic"), for looking up a market price when
    no model number is recognized. None if fewer than 2 remain."""
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", (title or "").lower())
             if w not in _TITLE_FILLER and (len(w) > 1 or w.isdigit())]
    return " ".join(words[:5]) if len(words) >= 2 else None


def value_key(title: Optional[str]) -> Optional[str]:
    """Market-value cache key: brand + model ("neumann:u87"), else the
    title's meaningful words ("t:soundcraft 12 channel analog mixer"). A
    bare model with no brand ("x32", "c38") is too ambiguous to look up on
    its own, so it falls back to the title words around it. Parts sold on
    their own ("KK 104 capsule head") get their own "p:" key so they're
    valued as the part, never as the whole mic."""
    q = title_query(strip_clone_reference(title))
    if is_partial(title):
        return f"p:{q}" if q else None
    key = model_key(title)
    if key and ":" in key:
        return key
    return f"t:{q}" if q else None


def value_query(title: Optional[str]) -> Optional[str]:
    key = value_key(title)
    if not key:
        return None
    return key[2:] if key[:2] in ("t:", "p:") else model_query(title)


def price_context(title: Optional[str], price: Optional[str], index: dict[str, float],
                  market: Optional[dict[str, float]] = None, description: Optional[str] = None) -> dict:
    """{'typical', 'label', 'deal', 'pct_under', 'source', 'basis', 'qty', 'unit', 'note'}.

    Local history (listings Gear Scout has seen) comes first, then the
    Reverb/eBay asking-price cache — asking prices run higher than sale
    prices, so a deal against them needs 35%+ under instead of 25%.
    Multi-piece ads are compared per piece. Ads with no price still get
    the market value (just no comparison); parts are valued as parts."""
    value = parse_price(price)
    partial = is_partial(title)
    if is_bundle(title):
        return {"basis": "single", "qty": 1, "unit": None, "unit_value": value,
                "note": "bundle of several items — no single-item comparison"}
    mkey, key = model_key(title), value_key(title)
    sold = (getattr(market, "sold", {}) or {}) if market else {}
    if mkey and mkey in sold and not partial:
        # What this model actually went for, from listings Gear Scout watched
        # sell — the most trustworthy comp there is.
        typical, source, threshold = sold[mkey], "sold", 0.75
    elif mkey and mkey in index and not partial:
        typical, source, threshold = index[mkey], "local", 0.70
    elif market and key and key in market:
        typical = market[key]
        source = (getattr(market, "sources", {}) or {}).get(key, "reverb")
        threshold = 0.60
    else:
        typical, source, threshold = None, None, None
    if value:
        basis, qty, unit = price_basis(title, description, value, typical)
    else:
        basis, qty, unit = "single", 1, None
    ctx = {"basis": basis, "qty": qty, "unit": format_price(unit) if unit and qty > 1 else None,
           "unit_value": unit, "note": basis_note(basis, qty, unit) if unit else None}
    # A rough gauge that's wildly off from the asking price is almost always
    # a comparison with the wrong thing — better no value than a wrong one.
    rough_guess = source != "local" and (
        bool(key and key[:2] in ("t:", "p:")) or key in (getattr(market, "rough", set()) or set()))
    if typical and rough_guess and unit and not (0.2 <= unit / typical <= 5):
        typical = None
    # 6x the typical price is a different variant ("SM57 Unidyne III" is a
    # vintage mic, not a $100 SM57) or a bundle — not a comparable item.
    if typical and unit and unit / typical > 6:
        typical = None
    if not typical:
        return ctx
    # A value found from title words (no model number) or from only one or
    # two listings is a rough gauge — shown, but never used to call a deal.
    rough = source not in ("local", "sold") and (
        bool(key and key[:2] in ("t:", "p:")) or key in (getattr(market, "rough", set()) or set()))
    # A comp you've disputed (hid two listings it called deals) stays a rough
    # estimate until it's been looked up again.
    disputed = getattr(market, "distrusted", set()) or set()
    if key in disputed or (mkey and mkey in disputed):
        rough = True
    ctx.update({
        "typical": format_price(typical),
        "rough": rough,
        # A real deal: 30%+ under what this model goes for (40%+ under
        # Reverb/eBay asking prices) — but more than 85% under is almost
        # always a comparison with the wrong thing (a reissue vs. a vintage
        # original, a part vs. the whole unit), not a deal.
        "deal": (bool(unit) and not rough and not partial and unit >= 20
                 and typical * 0.15 <= unit <= typical * threshold),
        "pct_under": round((1 - unit / typical) * 100) if unit else None,
        "source": source,
    })
    # "Worth a look": at least 10% under what a dealer B-stock / open-box
    # unit costs (or, for vintage gear with no such thing, under a solid
    # used value). Needs a comp to judge.
    bstock = (getattr(market, "bstock", {}) or {}).get(key or "") if market else None
    reference = bstock[0] if bstock else (None if rough else typical)
    if bstock:
        ctx["bstock"] = format_price(bstock[0])
        ctx["bstock_basis"] = bstock[1]
    if reference and unit:
        # Same guards as deals: no parts/bundles, no placeholder prices, and
        # more than 85% under is a mismatch (or a "$1, make an offer"), not
        # a real price.
        ctx["worth"] = (not partial and not is_bundle(title) and unit >= 20
                        and reference * 0.2 <= unit <= reference * 0.9)
        ctx["under_ref_pct"] = round((1 - unit / reference) * 100)
    if source == "sold":
        ctx["label"] = f"sold ~{ctx['typical']}"
        ctx["label_title"] = "What this model actually went for, from listings Gear Scout watched sell"
    elif source == "local":
        ctx["label"] = f"typically ~{ctx['typical']}"
        ctx["label_title"] = "Typical price of this model across listings Gear Scout has seen"
    else:
        site = source if source not in (None, "reverb") else "Reverb"
        ctx["label"] = f"{'est.' if rough else site} ~{ctx['typical']}"
        ctx["label_title"] = (
            f"Typical used asking price on {site} right now"
            + (" for similar items — a rough gauge (no exact model match, or only a few listings)" if rough else "")
            + " — real sale prices usually run a bit lower"
            + (", per piece" if qty > 1 else "")
        )
    return ctx


def basis_note(basis: str, qty: int, unit: float) -> Optional[str]:
    """Short note shown next to the price of a multi-piece ad."""
    if qty < 2:
        return None
    if basis == "each":
        return f"price is each · {qty} available"
    if basis == "each?":
        return f"likely each · {qty} listed"
    return f"for all {qty} · ~{format_price(unit)} each"
