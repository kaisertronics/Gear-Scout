"""Missing-accessory check: a large-diaphragm condenser or ribbon mic normally
ships with a shock mount / case. If the ad has a real description but never
mentions any accessory wording, flag it. Card tag only; never raises."""
import re
from typing import Optional

MIN_DESC = 40
_MODELS = re.compile(
    r"(?<![\w-])(?:u87|u ?47|tlm|c414|c12|tf11|tf47|ela m 251|251|67|47|ribbon|r44|r-?121|"
    r"ma-300|ma-200|cv-12|da-87|stam|soyuz|mojave)(?![\w])", re.I)
_MENTIONS = (
    "mount", "case", "box", "cradle", "clip", "power supply", "psu", "cable",
    "accessor", "complete", "everything", "as shown", "original",
)
PHRASE = "shock mount / case"


def check(title: Optional[str], description: Optional[str]) -> Optional[str]:
    """The missing item phrase, or None when nothing looks missing."""
    try:
        desc = (description or "").strip()
        if len(desc) < MIN_DESC:
            return None
        text = f"{title or ''} {desc}".lower()
        if not _MODELS.search(text):
            return None
        if any(w in text for w in _MENTIONS):
            return None
        return PHRASE
    except Exception:
        return None
