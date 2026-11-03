"""Spec-sheet mismatch check.

An ad titled "Neumann U87" whose description says "this is actually a TLM 103"
or "U87 style clone" is a mislabeled (usually cheap) mic. check(title,
description) returns a short plain-English warning, or None.

Text only (no network, no AI) and deliberately conservative. It flags only:
  * the description says, in an "it is ..." way, that the item is a DIFFERENT
    known model (or a different brand) than the title names, or
  * the description calls the titled model/brand a clone / copy / replica /
    "style" / "not a real ...".
Comparisons ("compare to a U87"), fit lists ("fits", "works with"), accessory
ads, pairs of the same family and negations ("not a clone") are left alone.
"""
import re
from typing import Optional

_I = re.I

# key -> (brand or None, regex). Same key = same family (U87 / U87ai, 47 FET variants...).
_MODELS = {
    "u87": ("neumann", r"u[\s-]?87(?:[\s-]?ai)?"),
    "u47fet": ("neumann", r"(?:u[\s-]?)?47[\s-]?fet"),
    "u47": ("neumann", r"u[\s-]?47(?![\s-]?fet)"),
    "u67": ("neumann", r"u[\s-]?67"),
    "u89": ("neumann", r"u[\s-]?89(?:[\s-]?i)?"),
    "tlm49": ("neumann", r"tlm[\s-]?49"),
    "tlm67": ("neumann", r"tlm[\s-]?67"),
    "tlm102": ("neumann", r"tlm[\s-]?102"),
    "tlm103": ("neumann", r"tlm[\s-]?103"),
    "tlm107": ("neumann", r"tlm[\s-]?107"),
    "tlm170": ("neumann", r"tlm[\s-]?170(?:[\s-]?r)?"),
    "m49": ("neumann", r"m[\s-]?49"),
    "m149": ("neumann", r"m[\s-]?149"),
    "km84": ("neumann", r"km[\s-]?84"),
    "km184": ("neumann", r"km[\s-]?184"),
    "km185": ("neumann", r"km[\s-]?185"),
    "tlm": ("neumann", r"tlm[\s-]?(?:\d+)"),  # catch-all: never used directly (see _find)
    "c12": ("akg", r"c[\s-]?12(?:[\s-]?a)?"),
    "c414": ("akg", r"c[\s-]?414(?:[\s-]?[a-z]{1,4})?"),
    "c451": ("akg", r"c[\s-]?451(?:[\s-]?[a-z]{1,2})?"),
    "c800g": ("mojave", r"c[\s-]?800[\s-]?g"),
    "d12": ("akg", r"d[\s-]?12(?:[\s-]?vr)?"),
    "elam251": (None, r"ela[\s-]?m[\s-]?251(?:[\s-]?e)?"),
    "sm7b": ("shure", r"sm[\s-]?7[\s-]?(?:b|db)"),
    "sm57": ("shure", r"sm[\s-]?57"),
    "sm58": ("shure", r"sm[\s-]?58"),
    "sm81": ("shure", r"sm[\s-]?81"),
    "re20": ("electro-voice", r"re[\s-]?20"),
    "md421": ("sennheiser", r"md[\s-]?421(?:[\s-]?ii)?"),
    "e609": ("sennheiser", r"e[\s-]?609"),
    "mk4": ("sennheiser", r"mk[\s-]?4"),
    "r121": ("royer", r"r[\s-]?121"),
    "r122": ("royer", r"r[\s-]?122"),
    "4038": ("coles", r"4038"),
    "nt1": ("rode", r"nt[\s-]?1(?:[\s-]?a)?"),
    "nt2a": ("rode", r"nt[\s-]?2[\s-]?a"),
    "at4050": ("audio-technica", r"at[\s-]?4050"),
    "at2020": ("audio-technica", r"at[\s-]?2020"),
    "wa47": ("warm audio", r"wa[\s-]?47(?:[\s-]?(?:jr|f))?"),
    "wa67": ("warm audio", r"wa[\s-]?67"),
    "wa87": ("warm audio", r"wa[\s-]?87(?:[\s-]?r\d)?"),
    "wa251": ("warm audio", r"wa[\s-]?251"),
    "wa14": ("warm audio", r"wa[\s-]?14"),
    "wa84": ("warm audio", r"wa[\s-]?84"),
}
_MODELS.pop("tlm")

_PRE = r"(?<![\w-])"
_POST = r"(?![\w-])"
_MODEL_RX = {k: re.compile(_PRE + rx + _POST, _I) for k, (_b, rx) in _MODELS.items()}

_BRANDS = ["neumann", "akg", "shure", "sennheiser", "telefunken", "warm audio", "rode", "audio-technica",
           "mojave", "lauten", "peluso", "royer", "aea", "electro-voice", "behringer", "mxl", "cad",
           "golden age", "stam", "soyuz", "schoeps", "coles", "beyerdynamic", "sontronics", "marshall",
           "samson", "alctron", "tonelux", "avantone", "bees neez", "universal audio"]
_BRAND_ALT = "|".join(re.escape(b).replace(r"\ ", r"[\s-]?") for b in sorted(_BRANDS, key=len, reverse=True))
_BRAND_RX = re.compile(_PRE + "(" + _BRAND_ALT + ")" + _POST, _I)


def _brand_key(s: str) -> str:
    s = re.sub(r"[\s-]+", " ", s.lower()).strip()
    return {"electro voice": "electro-voice", "audio technica": "audio-technica"}.get(s, s)


# Title says this is not the main item: skip the check.
_ACCESSORY_TITLE = re.compile(
    r"(?<![\w-])(?:shock\s?mounts?|capsules?|cables?|cases?|power\s?suppl(?:y|ies)|psu|parts?|for\s+parts|stands?|"
    r"pop\s?filters?|windscreens?|covers?|cradles?|clips?|manuals?|compatible|fits|replacement|vs\.?|versus)(?![\w-])", _I)
_TITLE_HONEST = re.compile(
    r"(?<![\w-])(?:clones?|replicas?|copy|copies|style|type|inspired|tribute|lookalike|knock-?off)(?![\w-])", _I)
_ACCESSORY_AFTER = re.compile(
    r"^\W*(?:shock\s?mounts?|mounts?|capsules?|cables?|cases?|power\s?suppl|psu|parts?|stands?|pop\s?filters?|"
    r"windscreens?|covers?|cradles?|clips?|manuals?|box|bag|grill|basket|transformer)", _I)

_NEGATE = re.compile(r"(?:\b(?:no|not|non|never|without|isn'?t|aren'?t|wasn'?t)\s+(?:a\s+|an\s+|any\s+|the\s+)?|\bnon-)$", _I)
_CLONE_WORD = re.compile(
    r"(?<![\w-])(?:clones?|replicas?|knock-?offs?|copy|copies|counterfeit|fake|look-?alike)(?![\w-])", _I)
_NOT_REAL = re.compile(
    r"\bnot\s+(?:a\s+|an\s+|the\s+)?(?:real|genuine|authentic|original|true|actual|official)\b", _I)
_COPY_PAPER = re.compile(r"^\W*(?:of\s+(?:the\s+)?(?:manual|receipt|invoice|paperwork|box|warranty|certificate|"
                         r"registration|instructions?|title|case)|right|writer|protected)", _I)
_STYLE_WORD = re.compile(
    r"^[\s-]*(?:style[ds]?|type|inspired|tribute)(?![\w-])(?!\s+(?:sound|tone|vibe|character|signal|vocals?))", _I)
_STYLE_OF = re.compile(
    r"(?<![\w-])(?:in\s+the\s+style\s+of|style\s+of|inspired\s+by|modou?ll?ed\s+(?:after|on)|based\s+on|"
    r"patterned\s+after|a\s+version\s+of|tribute\s+to)\s+(?:the\s+)?$", _I)

_IDENT = re.compile(
    r"(?:this\s+(?:one\s+)?is|it\s+is|it['’]s|its|is\s+actually|actually|really|"
    r"(?:mic(?:rophone)?|unit|item)\s+is|"
    r"(?:model|item)(?:\s+(?:number|no\.?|name))?\s*[:#=-])"
    r"\s*(?:a|an|the)?\s*(?:(?:brand\s+new|used|vintage|great|nice|genuine)\s+)?"
    r"(?:(?:" + _BRAND_ALT + r"|mic|microphone|large[\s-]diaphragm|condenser)\s+){0,2}$", _I)


def _label(m: re.Match) -> str:
    return re.sub(r"\s+", " ", m.group(0)).strip().upper()


def _title_models(title: str) -> dict:
    return {k: _label(m) for k, rx in _MODEL_RX.items() for m in [rx.search(title)] if m}


def _ident_before(text: str, start: int) -> bool:
    return bool(_IDENT.search(text[max(0, start - 60):start].rstrip() + " ")) if start else False


def _near_title(text: str, s: int, e: int, tokens: list, before: int = 40, after: int = 40) -> bool:
    win = text[max(0, s - before): e + after]
    return any(rx.search(win) for rx in tokens)


def check(title: Optional[str], description: Optional[str]) -> Optional[str]:
    """Short warning when the ad text names a different model/brand than the
    title, or calls the titled item a clone/copy/replica. None otherwise."""
    title = title or ""
    desc = re.sub(r"\s+", " ", description or "").strip()
    if not title or len(desc) < 8:
        return None
    if _ACCESSORY_TITLE.search(title) or _TITLE_HONEST.search(title):
        return None
    t_models = _title_models(title)
    t_brands = {_brand_key(m.group(1)) for m in _BRAND_RX.finditer(title)}
    t_brands |= {_MODELS[k][0] for k in t_models if _MODELS[k][0]}
    if not t_models and not t_brands:
        return None
    tokens = [_MODEL_RX[k] for k in t_models]
    tokens += [re.compile(_PRE + re.escape(b).replace(r"\ ", r"[\s-]?") + _POST, _I) for b in t_brands]

    # 1) a different known model, stated as what the item is
    if t_models:
        for k, rx in _MODEL_RX.items():
            if k in t_models:
                continue
            for m in rx.finditer(desc):
                if _ACCESSORY_AFTER.search(desc[m.end(): m.end() + 20]):
                    continue
                if _STYLE_WORD.search(desc[m.end(): m.end() + 14]):
                    continue  # "TLM 103 style" is a comparison, handled below
                if _ident_before(desc, m.start()):
                    return f"the ad text says it's a {_label(m)}, but the title says {next(iter(t_models.values()))}"

    # 2) clone / replica / copy / "style" / "not a real ..."
    for m in _CLONE_WORD.finditer(desc):
        if _NEGATE.search(desc[max(0, m.start() - 14):m.start()]) or _COPY_PAPER.search(desc[m.end(): m.end() + 30]):
            continue
        if _near_title(desc, m.start(), m.end(), tokens):
            return f"the ad text calls it a \"{m.group(0).lower()}\" of the {next(iter(t_models.values()), None) or title[:30]}"
    for m in _NOT_REAL.finditer(desc):
        if _near_title(desc, m.start(), m.end(), tokens, before=15, after=45):
            return "the ad text says it's not the real thing named in the title"
    for k, tm in t_models.items():
        for m in _MODEL_RX[k].finditer(desc):
            if _STYLE_WORD.search(desc[m.end(): m.end() + 14]) and not _NEGATE.search(desc[max(0, m.start() - 14):m.start()]):
                return f"the ad text calls it \"{_label(m)} style\", not the real {tm}"
            if _STYLE_OF.search(desc[max(0, m.start() - 30):m.start()]):
                return f"the ad text says it's based on / inspired by the {_label(m)}, not the real thing"
    for b in t_brands:
        for m in re.finditer(_PRE + re.escape(b).replace(r"\ ", r"[\s-]?") + _POST, desc, _I):
            if _STYLE_OF.search(desc[max(0, m.start() - 30):m.start()]):
                return f"the ad text says it's based on / inspired by {m.group(0).title()}, not the real thing"

    # 3) a different brand, stated as what the item is ("this is a Behringer B-2")
    if t_brands:
        for m in _BRAND_RX.finditer(desc):
            if _brand_key(m.group(1)) in t_brands:
                continue
            if _ACCESSORY_AFTER.search(desc[m.end(): m.end() + 20]):
                continue
            tail = desc[m.end(): m.end() + 30]
            if not re.match(r"\s+(?:[\w-]*\d[\w-]*|mic(?:rophone)?\b)", tail, _I):
                continue
            if _ident_before(desc, m.start()):
                return f"the ad text says it's a {m.group(1).title()}, but the title says {', '.join(sorted(b.title() for b in t_brands))}"
    return None
