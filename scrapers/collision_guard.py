"""Brand-collision guard.

Some brand names are also ordinary words ("Golden Age" clarinets, "Solid
State" hi-fi, "United" anything). An ad that only matched through one of
those is kept only when its title/description also carries a studio word
(mic, preamp, compressor, rack, XLR...) or a specific model name. Ads with a
specific model (U87, 1176, LA-2A, Distressor, GA-73...) or another real brand
are never affected.
"""
import re
from typing import Optional

_AMBIGUOUS = re.compile(r"(?<![\w-])(?:golden[\s-]?age(?:[\s-]project)?|solid[\s-]state|united)(?![\w-])", re.I)
# "Solid State Logic" is SSL, a specific brand, not the ambiguous phrase.
_SSL = re.compile(r"solid[\s-]state[\s-]logic", re.I)

_STUDIO = re.compile(
    r"(?<![\w-])(?:mics?|microphones?|preamps?|pre-?amps?|pres?|compressors?|limiters?|rack(?:[\s-]?mount)?|"
    r"racks|xlr|ribbons?|condensers?|channel[\s-]?strips?|outboard|500[\s-]?series|lunchbox|"
    r"equali[sz]ers?|eq|di[\s-]?box|d\.?i\.?|studio|recording|vocal|tube|fet|opto|vca|diaphragm|"
    r"ldc|sdc|console|mixer|interface|converter|summing|api|neve|ssl)(?![\w-])", re.I)
# Words that also appear in home hi-fi preamp ads: not enough on their own.
_WEAK = re.compile(r"(?<![\w-])(?:preamps?|pre-?amps?|pres?|tube|mixer|interface|console|rack)(?![\w-])", re.I)
_HIFI = re.compile(
    r"\b(?:hi-?fi|audiophile|phono|turntable|receiver|tuner|integrated|stereo (?:power )?amp\w*|"
    r"home (?:theater|stereo)|mcintosh|marantz|dynaco|sansui|kenwood|onkyo|denon|luxman|nad)\b", re.I)
# Specific model names that must never be dropped.
_MODELS = re.compile(
    r"(?<![\w-])(?:u[\s-]?(?:47|67|87)\w*|1176\w*|la[\s-]?2a|la[\s-]?3a|distressor|ga[\s-]?\d{2,3}\w*|"
    r"fc[\s-]?[34]|r1(?:\s?mk\s?ii)?|ut[\s-]?(?:twin|fet|tube|47|67|87)\w*|twin[\s-]?(?:47|48|87)|"
    r"fet47|pre[\s-]?73|1073|1084|c12|251|m49|km[\s-]?\d{2,3}\w*)(?![\w-])", re.I)


def collision_ok(title: Optional[str], description: Optional[str] = None) -> bool:
    """False when the ad matched only an ambiguous brand word and shows no
    sign of being studio gear."""
    t = title or ""
    base = _SSL.sub(" ", t)
    if not _AMBIGUOUS.search(base):
        return True
    if _MODELS.search(t):
        return True
    text = f"{t} {description or ''}"
    rest = _AMBIGUOUS.sub(" ", base)
    if _HIFI.search(text):
        return bool(_STUDIO.search(_WEAK.sub(" ", text)))
    if _STUDIO.search(text):
        return True
    try:  # another real brand in the title (e.g. "Neve ... United")
        from scrapers.enrich import canonical_brand
        return bool(canonical_brand(rest))
    except Exception:
        return False
