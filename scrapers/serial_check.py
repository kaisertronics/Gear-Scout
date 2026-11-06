"""Serial-number age check. Very conservative: only the Neumann U87 (the one
range here that is coarse and widely documented). A serial under 100000 is the
original, pre-Ai production (1967 into the 1980s); the Ai series (1986+) runs
into six digits. The serial must follow an explicit "serial" / "s/n" / "sn"
marker. A claimed build year in the ad that contradicts the era is flagged.
Card tag only; returns None when not confident; never raises.

Add brands to _RULES only with ranges verified from the maker."""
import re
from typing import Optional

_SERIAL = re.compile(r"(?:serial(?:\s*(?:number|no\.?|#))?|s/n|\bsn)\s*[:#]?\s*(\d{3,7})\b", re.I)
_CLAIM = re.compile(
    r"(?:made|built|manufactured|produced|year|dated|circa|from)\s*(?:in|of|:)?\s*((?:19[4-9]|20[0-3])\d)\b", re.I)

# (model regex, max serial for vintage tier, vintage label, earliest era year, latest era year)
_RULES = (
    (re.compile(r"(?<![\w-])u[\s-]?87(?!\s*ai)(?![\w])", re.I), 99999, "U87 pre-Ai", 1967, 1989),
)


def check(title: Optional[str], description: Optional[str]) -> Optional[str]:
    """Short label such as 'Serial 12345: U87 pre-Ai (1967-80s, vintage)', or None."""
    try:
        text = f"{title or ''} {description or ''}"
        m = _SERIAL.search(text)
        if not m:
            return None
        serial = int(m.group(1))
        for model_re, max_serial, name, y0, y1 in _RULES:
            if not model_re.search(text):
                continue
            if serial > max_serial or serial < 100:
                return None
            claim = _CLAIM.search(text)
            if claim:
                year = int(claim.group(1))
                if year > y1 + 1 or year < y0:
                    return f"Serial {serial} is {name} era ({y0}-80s), ad says {year}"
            return f"Serial {serial}: {name} ({y0}-80s, vintage)"
        return None
    except Exception:
        return None
