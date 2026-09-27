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


def parse_price(price: Optional[str]) -> Optional[float]:
    if not price:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", price)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


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


def exclude_match(title: Optional[str], exclude_words: list[str]) -> bool:
    from scrapers.base import keyword_match
    words = [w for w in (exclude_words or []) if str(w).strip()]
    return bool(words) and keyword_match(title or "", [str(w) for w in words])


def drop_excluded(listings: list, cfg: dict) -> list:
    words = cfg.get("exclude_words") or []
    if not words:
        return listings
    return [l for l in listings if not exclude_match(l.title, words)]


# --- Typical price / deal context -------------------------------------------

def model_key(title: Optional[str]) -> Optional[str]:
    """The first model-number-looking token in a title, normalized and
    prefixed with the brand ("neumann:u87ai", "sennheiser:421",
    "tascam:portastudio414") — so listings of the same model group together
    even when written "WA-47" or "WA 47"."""
    found = _model_match(title)
    return found[0] if found else None


def model_query(title: Optional[str]) -> Optional[str]:
    """The same model as readable search words ("Sennheiser 421",
    "Tascam Portastudio 414") — used to look the model up on Reverb."""
    found = _model_match(title)
    return found[1] if found else None


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
        if (key and value and value >= 20 and not is_partial(r.get("title")) and not is_lot(r.get("title"))
                and not needs_repair(r.get("title"), r.get("description"))):
            by_model.setdefault(key, []).append(value)
    return {k: statistics.median(v) for k, v in by_model.items() if len(v) >= 4}


def price_context(title: Optional[str], price: Optional[str], index: dict[str, float],
                  market: Optional[dict[str, float]] = None) -> dict:
    """{'typical': '$1,200', 'deal': bool, 'pct_under': int, 'source': 'local'|'reverb'}
    or {} when there's nothing reliable to compare against.

    Local history (what Gear Scout has seen listed) comes first. Otherwise,
    Reverb asking prices — those run higher than real sale prices, so a
    Reverb-based deal needs 35%+ under instead of 25%."""
    key = model_key(title)
    value = parse_price(price)
    if not key or not value or is_partial(title) or is_lot(title):
        return {}
    if key in index:
        typical, source, threshold = index[key], "local", 0.75
    elif market and key in market:
        typical, source, threshold = market[key], "reverb", 0.65
    else:
        return {}
    return {
        "typical": format_price(typical),
        "deal": value <= typical * threshold and value >= 20,
        "pct_under": round((1 - value / typical) * 100),
        "source": source,
    }