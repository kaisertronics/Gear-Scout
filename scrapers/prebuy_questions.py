"""Pre-buy question drafter: 2-4 plain-English questions to ask the seller of a
used mic / gear listing before buying, picked by simple rules from the title,
description and category. No AI, no network.

Rules (in priority order, first MAX_Q kept): tube / vintage -> tube age and
power-supply cable; missing accessories -> ask if included; no working-condition
words -> ask if fully tested; shipped with no serial -> serial + capsule photos.
Card <details> only. Never raises; returns [] when nothing applies.
"""
import re
from typing import Optional

MIN_Q = 2
MAX_Q = 4

_TUBE = re.compile(r"\b(?:tube|valve|u47|u67|u87|c12|ela m 251|m49|m 49|251|ecc\d+|12ax7)\b", re.I)
_VINTAGE = re.compile(r"\b(?:vintage|19[3-7]\d|(?:30|40|50|60|70)s)\b", re.I)
_MIC = re.compile(r"\b(?:mic|microphone|condenser|ribbon|dynamic|capsule)\b", re.I)
_WORKING = re.compile(
    r"\b(?:works?|working|tested|fully functional|functional|perfect|mint|"
    r"like new|great condition|excellent|no issues|plays? great|sounds? great)\b", re.I)
_SERIAL = re.compile(r"\b(?:serial|s/n|sn\s?[:#]?\s?\d+)", re.I)
_PSU = re.compile(r"\b(?:psu|power supply|power cable|cable)\b", re.I)
_SHOCK = re.compile(r"\b(?:shock\s?mount|spider|cradle|clip)\b", re.I)
_CASE = re.compile(r"\b(?:case|box|pouch|bag)\b", re.I)
_PICKUP = re.compile(r"\b(?:local pickup|pickup only|pick up only|no shipping|cash)\b", re.I)


def _missing_accessories(text: str, is_mic: bool, is_tube: bool) -> Optional[str]:
    """Question naming the accessories the ad never mentions, or None."""
    want = []
    if is_mic and not _SHOCK.search(text):
        want.append("shockmount")
    if not _CASE.search(text):
        want.append("case")
    if is_tube and not _PSU.search(text):
        want.append("power supply")
    if not want:
        return None
    return "Does it come with the " + ", ".join(want) + "? What exactly is included in the sale?"


def prebuy_questions(title: Optional[str], description: Optional[str] = None,
                     category: Optional[str] = None, has_shipping: bool = False,
                     is_local: bool = False) -> list[str]:
    """2-4 questions for the seller, or [] if fewer than MIN_Q apply.
    has_shipping: the ad is shipped (not pickup-only); is_local: pickup-only."""
    try:
        text = f"{title or ''} {description or ''}"
        cat = (category or "").lower()
        is_mic = bool(_MIC.search(text)) or "mic" in cat
        is_tube = bool(_TUBE.search(text))
        is_vintage = bool(_VINTAGE.search(text))
        qs = []
        if is_tube or (is_vintage and is_mic):
            qs.append("How old are the tubes, and have they been replaced? "
                      "Is the original power supply and cable included and working?")
        acc = _missing_accessories(text, is_mic, is_tube)
        if acc:
            qs.append(acc)
        if not _WORKING.search(text):
            qs.append("Has it been fully tested, and does everything work with no noise or crackle?")
        shipped = has_shipping and not is_local and not _PICKUP.search(text)
        if shipped and not _SERIAL.search(text):
            qs.append("Can you share the serial number and close photos of the "
                      + ("capsule and grille?" if is_mic else "front, back and any wear?"))
        return qs[:MAX_Q] if len(qs) >= MIN_Q else []
    except Exception:
        return []
