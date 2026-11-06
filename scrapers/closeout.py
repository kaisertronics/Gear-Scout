"""Closeout radar: ads that read like a studio closing, an estate, a liquidation,
a "whole studio" or a rack lot. Builds on the motivated-seller patterns
(studio closing / liquidating / estate) and adds a few closeout-only phrases.
Read-only text check; never scrapes estate-sale sites, never touches price rules.
"""
import re
from typing import Optional

from scrapers import motivated

# Reasons from motivated.py that already mean "closeout"
_FROM_MOTIVATED = ("Studio closing", "Liquidating", "Estate")

# Extra phrases motivated.py does not cover
_EXTRA = re.compile(
    r"\bwhole\s+studio\b|\bentire\s+studio\b|\bcomplete\s+studio\b|\bstudio\s+(?:closeout|close[\s-]out|clearance|liquidation)\b"
    r"|\bclose[\s-]?out\s+(?:sale|lot)\b|\bstudio\s+(?:gear\s+)?(?:lot|package)\b|\brack\s+lot\b|\blot\s+of\s+(?:studio\s+)?rack\b"
    r"|\bcontents\s+of\s+(?:a\s+|the\s+|my\s+|our\s+)?(?:recording\s+)?studio\b|\bstudio\s+(?:is\s+)?(?:being\s+)?(?:sold|dismantled)\b",
    re.I)


def label(r: dict) -> Optional[str]:
    """Short card label ("Studio closing", "Estate", "Whole studio"), None when the ad shows no closeout signal."""
    for why in motivated.reasons(r):
        if why in _FROM_MOTIVATED:
            return why
    if _EXTRA.search(motivated._text(r)):
        return "Whole studio / lot"
    return None
