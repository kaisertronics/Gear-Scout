"""Ribbon mic card note: a ribbon element is fragile (wind, phantom power
mistakes, drops), so a ribbon mic's ad should say how the ribbon is. One short
line from the title + description. No AI, no network, never raises.

  "Ribbon: says re-ribboned"
  "Ribbon: sagging/stretched/damaged/blown (risk)"   (names the words found)
  "Ribbon: no ribbon condition mentioned, ask"
  None when it isn't a ribbon mic.
"""
import re
from typing import Optional

# A ribbon mic: the word "ribbon" next to mic words, or a well-known ribbon model.
_RIBBON_MIC = re.compile(
    r"\bribbon\s+(?:mic(?:rophone)?s?|element|velocity)\b|\bmic(?:rophone)?\s+ribbon\b|\bvelocity\s+mic"
    r"|\b(?:r[\s-]?(?:121|84|44|88|92)|ae[\s-]?(?:4038|44|88)|4038|vr[\s-]?[12]|rnr[\s-]?1|"
    r"royer|coles|fat\s?head|m[\s-]?160|m[\s-]?260|ribbon\s+(?:r|sf|ps)\d*)\b", re.I)
_RIBBON_WORD = re.compile(r"\bribbon\b", re.I)
_NOT_MIC = re.compile(r"hair|gift|satin|bow\b|cable|ribbon\s+cable|typewriter|printer|trim|spool|craft", re.I)

_RE_RIBBONED = re.compile(r"\bre[\s-]?ribbon(?:ed|ing)?\b|\bnew\s+ribbon\b|\bfresh\s+ribbon\b|\bribbon\s+(?:was\s+)?replaced\b", re.I)
_BAD = re.compile(
    r"\b(sag(?:ging|ged|s)?|stretch(?:ed)?|damaged|blown|torn|broken|ripped|bent|dented|crinkled|wrinkled|"
    r"needs?\s+(?:a\s+)?re[\s-]?ribbon(?:ing)?|dead\s+ribbon)\b", re.I)
_BAD_NEAR = re.compile(r"ribbon[^.\n]{0,40}\b(sag\w*|stretch\w*|damaged|blown|torn|broken|ripped|bent|dented|crinkled|wrinkled)\b"
                       r"|\b(sag\w*|stretch\w*|damaged|blown|torn|broken|ripped|bent|dented|crinkled|wrinkled)\b[^.\n]{0,40}ribbon", re.I)
_GOOD = re.compile(r"\bribbon\b[^.\n]{0,40}\b(?:good|great|fine|perfect|intact|straight|tested|ok|okay|healthy|clean)\b"
                   r"|\b(?:good|great|fine|perfect|intact|tested|healthy|clean)\b[^.\n]{0,25}\bribbon\b", re.I)
_NEGATED = re.compile(r"\b(?:no|not|isn'?t|without|never)\b[^.\n]{0,15}\b(?:sag\w*|stretch\w*|damage\w*|blown|torn|broken)\b", re.I)


def ribbon_note(title: Optional[str], description: Optional[str] = None) -> Optional[str]:
    """Short ribbon-condition note for a ribbon mic listing, else None."""
    try:
        text = f"{title or ''} {description or ''}"
        if not _RIBBON_MIC.search(text):
            return None
        if _RIBBON_WORD.search(text) and _NOT_MIC.search(title or "") and not re.search(r"\bmic", text, re.I):
            return None
        bad = _BAD_NEAR.search(text) or (_BAD.search(text) and not _NEGATED.search(text))
        if bad and not _NEGATED.search(text):
            word = next((g for g in (bad.groups() if bad.groups() else ()) if g), bad.group(0)).lower().strip()
            return f"Ribbon: {word} (risk)"
        if _RE_RIBBONED.search(text):
            return "Ribbon: says re-ribboned"
        if _GOOD.search(description or "") or _NEGATED.search(text):
            return "Ribbon: described as OK, confirm with photos"
        return "Ribbon: no ribbon condition mentioned, ask"
    except Exception:
        return None
