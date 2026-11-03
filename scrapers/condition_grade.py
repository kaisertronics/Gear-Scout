"""Condition grade from the ad text alone: 'Mint', 'Good', 'Worn' or
'For parts / repair'. Reuses the keyword lists in market_value (mint / rough)
and enrich.needs_repair. Card tag only; never raises. Returns None when the
text says nothing about condition (no tag is better than a guessed 'Good')."""
import re
from typing import Optional

_GOOD = re.compile(r"\bgood condition\b|\bgreat condition\b|\bvery good\b|\bworks (?:great|perfectly|fine)\b|"
                   r"\bfully (?:working|functional)\b|\bexcellent\b|\bwell[- ]kept\b|\bgently used\b", re.I)


def grade(title: Optional[str], description: Optional[str]) -> Optional[str]:
    try:
        from scrapers.enrich import needs_repair
        from scrapers.market_value import _MINT, _ROUGH
        if needs_repair(title, description):
            return "For parts / repair"
        text = (title or "") + "\n" + (description or "")[:300]
        if _ROUGH.search(text):
            return "Worn"
        if _MINT.search(text):
            return "Mint"
        if _GOOD.search(text):
            return "Good"
        return None
    except Exception:
        return None
