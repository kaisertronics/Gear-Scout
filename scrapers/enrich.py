"""
Per-listing helpers shared by the scrape cycle, the dashboard and the email:
price parsing, "needs repair" detection, cross-post duplicate keys, the
user's exclude-words list, and typical-price / deal context.
"""
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


def is_bundle(title: Optional[str]) -> bool:
    """Several pieces of gear sold together ("EVH 5150 amp & 2x12 cabinet",
    "ADAM A8H monitors + Sub12 + stands") — one item's market price doesn't
    fit. A mic "with case and shock mount" isn't a bundle."""
    t = title or ""
    if re.search(r"\b(?:bundle|package deal|full setup|studio setup|combo deal)\b", t, re.I):
        return True
    parts = [p for p in re.split(r"\s(?:&|\+|and|plus)\s|\s/+\s|\s\|\s", t) if p.strip()]
    if len(parts) < 2:
        return False
    gear = [p for p in parts if not _ACCESSORY_WORDS.match(p) and (_GEAR_NOUN.search(p) or _MODELISH.search(p))]
    return len(gear) >= 2


def is_lot(title: Optional[str]) -> bool:
    return bool(title and _LOT_RE.search(title))


def is_partial(title: Optional[str]) -> bool:
    if not title:
        return False
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


def is_not_audio(title: Optional[str]) -> bool:
    """True for listings that only share a word with audio gear."""
    t = title or ""
    # "Viair compressors", "aAir Compressor": "air compressor" even inside a word.
    if _NOT_AUDIO.search(t) or re.search(r"air[\s-]?compressor|air tank|viair", t, re.I):
        return True
    # A bare "Compressor" / "Mixer" / "Monitor" with nothing else to go on
    # (typical of air compressors on OfferUp and Marketplace).
    return bool(re.fullmatch(r"\W*(?:an?\s+)?(?:compressors?|mixers?|monitors?|racks?)(?:\s+\w+){0,2}?\W*", t.strip(), re.I)
                and not re.search(r"\b(?:audio|studio|rack ?mount|stereo|mic|vocal|channel|limiter|opto|tube|fet|vca|dbx|api|ssl|neve|la-?2a|1176)\b", t, re.I))


def exclude_match(title: Optional[str], exclude_words: list[str]) -> bool:
    from scrapers.base import keyword_match
    words = [w for w in (exclude_words or []) if str(w).strip()]
    return bool(words) and keyword_match(title or "", [str(w) for w in words])


def drop_excluded(listings: list, cfg: dict) -> list:
    words = cfg.get("exclude_words") or []
    return [l for l in listings if not is_not_audio(l.title) and not (words and exclude_match(l.title, words))]


# --- Typical price / deal context -------------------------------------------

# What form an item takes. A "UAFX LA-2A pedal" ($150) or an LA-2A kit is
# not a Teletronix LA-2A ($3,800) even though the model name matches, so
# these become part of the model's identity and of its price lookups.
_FORMS = (
    ("pedal", re.compile(r"\b(?:pedal|stomp ?box|uafx)\b", re.I)),
    ("plugin", re.compile(r"\b(?:plug-?in|software|licen[sc]e|vst|aax)\b", re.I)),
    ("kit", re.compile(r"\b(?:diy|pcb|bare boards?|unbuilt|unassembled|(?:partial|build|clone|diy|project) kit|kit (?:build|form|only))\b", re.I)),
    ("500", re.compile(r"\b500[\s-]?series\b|\bapi[\s-]?500\b|\b500 (?:module|format)\b|\(500\)|\b(?:lunchbox|vpr)\b", re.I)),
)


def item_form(title: Optional[str]) -> Optional[str]:
    """'pedal', 'plugin', 'kit', '500' or None (a regular unit)."""
    for name, pattern in _FORMS:
        if pattern.search(title or ""):
            return name
    return None


def model_key(title: Optional[str]) -> Optional[str]:
    """The first model-number-looking token in a title, normalized and
    prefixed with the brand ("neumann:u87ai", "sennheiser:421",
    "tascam:portastudio414") — so listings of the same model group together
    even when written "WA-47" or "WA 47". A pedal/kit/plugin/500-series
    version gets its own key ("universal:la2a|pedal")."""
    found = _model_match(title)
    if not found:
        return None
    form = item_form(title)
    return f"{found[0]}|{form}" if form else found[0]


def model_query(title: Optional[str]) -> Optional[str]:
    """The same model as readable search words ("Sennheiser 421",
    "Tascam Portastudio 414", "Universal LA-2A pedal") — used to look the
    model up on Reverb/eBay."""
    found = _model_match(title)
    if not found:
        return None
    form = item_form(title)
    return f"{found[1]} {'500 series' if form == '500' else form}" if form else found[1]


def _model_match(title: Optional[str]) -> Optional[tuple[str, str]]:
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
        # "LA-2A"/"LA2A" is a model; "model 7" or "channel 8" is not — a
        # word followed by a spaced number needs at least 2 digits.
        if sep == " " and len(digits) < 2:
            continue
        # "Presonus 16", "Behringer 24": a long word and a short number is a
        # size or count, not a model ("NS 10", "KM 184", "U 87" still are).
        if sep == " " and len(digits) < 3 and len(letters) > 3 and not suffix:
            continue
        # "circa 1965", "from 1972": a year, not a model number.
        if len(digits) == 4 and 1920 <= int(digits) <= 2035 and not suffix:
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
                and not needs_repair(r.get("title"), r.get("description"))):
            by_model.setdefault(key, []).append(value)
    return {k: statistics.median(v) for k, v in by_model.items() if len(v) >= 4}


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
        m = re.search(pat, t)
        if m:
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
    q = title_query(title)
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
    if mkey and mkey in index and not partial:
        typical, source, threshold = index[mkey], "local", 0.75
    elif market and key and key in market:
        typical = market[key]
        source = (getattr(market, "sources", {}) or {}).get(key, "reverb")
        threshold = 0.65
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
    rough = source != "local" and (
        bool(key and key[:2] in ("t:", "p:")) or key in (getattr(market, "rough", set()) or set()))
    ctx.update({
        "typical": format_price(typical),
        "rough": rough,
        "deal": bool(unit) and not rough and unit <= typical * threshold and unit >= 20,
        "pct_under": round((1 - unit / typical) * 100) if unit else None,
        "source": source,
    })
    if source == "local":
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
