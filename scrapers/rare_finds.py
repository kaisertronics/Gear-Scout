"""Rare finds radar.

Pure functions, no I/O. rare_find(title, brand) returns a short label such as
"Neumann U47" when a listing title names one of a curated list of rare /
vintage pro-audio models, or None. Every pattern needs the model AND (the
brand in the title, the brand argument, or a model that is unmistakable on its
own), so "Neumann U87" or a plain "44" never match. Conservative: no match
means None, and clone / case / cable / record style titles are skipped.
"""
import re
from typing import Optional

# Titles that are about something other than the real vintage unit.
_JUNK = re.compile(
    r"(?<![\w-])(?:clones?|styles?|(?<!rca )types?|replicas?|repro(?:duction)?s?|inspired|kits?|diy|cases?|"
    r"cables?|cords?|posters?|records?|lps?|vinyl|cds?|cassettes?|books?|manuals?|schematics?|"
    r"modell?ed|after|like an?|inspired|mods?|upgrades?|replacements?|suspension|nuts?|sleeves?|"
    r"audioscape|soundelux|manley|fredenstein|warm audio|lindell|golden age|behringer|"
    r"capsules?|baskets?|grilles?|shock[\s-]?mounts?|stands?|bags?|covers?|knobs?|"
    r"transformers?|parts?|for parts|pcbs?|boards?|faceplates?|t-?shirts?|prints?)(?![\w-])", re.I)

_N = r"[\s-]?"  # optional separator between letters and digits
# (label, brand regex or None, model regex). A brand regex must match the title
# or the brand argument; None means the model is distinctive enough alone.
_RAW = [
    ("Neumann U47", r"neumann", r"u" + _N + r"47(?:\s?fet)?"),
    ("Neumann U48", r"neumann", r"u" + _N + r"48"),
    ("Neumann U67", r"neumann", r"u" + _N + r"67"),
    ("Neumann M49", r"neumann", r"m" + _N + r"49"),
    ("Neumann M269", r"neumann", r"m" + _N + r"269"),
    ("Neumann CMV3", r"neumann", r"cmv" + _N + r"3a?"),
    ("Neumann CMV563", r"neumann", r"cmv" + _N + r"563"),
    ("Neumann SM2", r"neumann", r"sm" + _N + r"2"),
    ("AKG C12", r"akg", r"c" + _N + r"12(?!\s?[ab]\b)(?!\d)"),
    ("AKG C24", r"akg", r"c" + _N + r"24"),
    ("AKG C28", r"akg", r"c" + _N + r"28"),
    ("Telefunken ELA M 250", r"telefunken", r"ela" + _N + r"m" + _N + r"250"),
    ("Telefunken ELA M 251", r"telefunken", r"ela" + _N + r"m" + _N + r"251[ae]?"),
    ("Telefunken V72", r"telefunken|siemens|eltro", r"v" + _N + r"72a?"),
    ("Telefunken V76", r"telefunken|siemens|eltro", r"v" + _N + r"76"),
    ("Telefunken W395", r"telefunken|siemens|eltro", r"w" + _N + r"395"),
    ("Telefunken U73", r"telefunken", r"u" + _N + r"73"),
    ("Sony C-37A", r"sony", r"c" + _N + r"37\s?a"),
    ("Sony C-800G", r"sony", r"c" + _N + r"800\s?g"),
    ("RCA 44", r"rca", r"(?:type" + _N + r")?44" + _N + r"(?:bx|b|a)?"),
    ("RCA 77", r"rca", r"(?:type" + _N + r")?77" + _N + r"(?:dx|d|c|b|a)?"),
    ("RCA BK-5", r"rca", r"bk" + _N + r"5" + _N + r"[ab]?"),
    ("Altec 633", r"altec", r"633" + _N + r"a?"),
    ("Siemens/Eltro", None, r"(?:siemens|eltro)\s+(?:eltro\s+)?(?:amplifier|mikrofon|microphone|mic|preamp|eq|equalizer)"),
    ("EMT plate reverb", r"emt", r"(?:140|plate)"),
    ("Pultec EQP-1A", r"pultec", r"eqp" + _N + r"1\s?a"),
    ("Pultec MEQ-5", r"pultec", r"meq" + _N + r"5"),
    ("Fairchild 660", r"fairchild", r"660"),
    ("Fairchild 670", r"fairchild", r"670"),
    ("Teletronix LA-2A", r"teletronix", r"la" + _N + r"2a"),
    ("Neve 1073 (vintage)", r"neve", r"1073"),
    ("Neve 1084 (vintage)", r"neve", r"1084"),
    ("API 550 (vintage)", r"api", r"550" + _N + r"[ab]?"),
    ("Neve 33609", r"neve", r"33609"),
    ("Universal Audio 610 (original)", r"universal audio|ua|urei|bill putnam", r"610" + _N + r"[ab]?"),
    ("Western Electric 639", r"western electric", r"639" + _N + r"[a-z]?"),
    ("Coles 4038", r"coles|stc", r"4038"),
    ("Beyer M160/M500 (vintage)", None, r"beyer(?:dynamic)?\s+m" + _N + r"(?:160|500)"),
]

# Vintage-only: these models also exist as modern reissues; require a vintage cue.
_NEEDS_VINTAGE = {"Neve 1073 (vintage)", "Neve 1084 (vintage)", "API 550 (vintage)",
                  "Teletronix LA-2A", "Universal Audio 610 (original)"}
_VINTAGE = re.compile(
    r"(?<![\w-])(?:vintage|original|orig|1960s?|1970s?|60s|70s|196\d|197\d|marconi|rupert|"
    r"original|oem|ex[\s-]?studio|bt[\s-]?\d+)(?![\w-])", re.I)
_MODERN = re.compile(
    r"^\s*new\b|(?<![\w-])(?:reissue|re-?issue|brand[\s-]new|new in box|20[0-2]\d|2000s|dpa|dpx|lunchbox|500[\s-]?series|1073lb|1073d|1073spx|"
    r"api[\s-]?550a|modern|mkii|mk2)(?![\w-])", re.I)

# Common modern models named in the title mean it is not about the rare one.
_COMMON = re.compile(r"(?<![\w-])(?:u[\s-]?87\w*|ea[\s-]?87|tlm[\s-]?\d+|km[\s-]?184)(?![\w-])", re.I)

_PATTERNS = [
    (label, re.compile(r"(?<![\w-])(?:" + b + r")(?![\w-])", re.I) if b else None,
     re.compile(r"(?<![\w-])(?:" + m + r")(?![\w-])", re.I))
    for label, b, m in _RAW
]


def rare_find(listing_title: Optional[str], brand: Optional[str] = None) -> Optional[str]:
    """Label of the rare/vintage model named in the title, or None."""
    title = listing_title if isinstance(listing_title, str) else ""
    if not title.strip() or _JUNK.search(title) or _MODERN.search(title) or _COMMON.search(title):
        return None
    brand_text = brand if isinstance(brand, str) else ""
    for label, brand_rx, model_rx in _PATTERNS:
        if not model_rx.search(title):
            continue
        if brand_rx is not None and not (brand_rx.search(title) or brand_rx.search(brand_text)):
            continue
        if label in _NEEDS_VINTAGE and (_MODERN.search(title) or not _VINTAGE.search(title)):
            continue
        return label
    return None
