"""Price per channel for multi-channel preamps / mixers / interfaces.

Pure functions, no I/O. parse_channels() reads a channel count from a listing
title ("8 channel", "8-ch", "4ch", "16 channels", "ADAT 16 channel"); per_channel()
divides the price by it. Conservative: no match means None.
"""
import re
from typing import Optional

MIN_CH = 2
MAX_CH = 64
LOW_PER_CH = 75.0  # at or under this $/channel the chip is highlighted

# "8 channel(s)", "8-channel", "8 ch", "8-ch", "8ch", "8 chan", "8 chnl"
_CH = re.compile(r"(?<![\w.$/])(\d{1,3})\s*-?\s*(?:channels?|chan|chnl|ch)(?![a-z0-9])", re.I)
# "channel 8" is a selector, not a count; "8 channel" only. Junk words kill the match.
_JUNK = re.compile(r"\b(?:mics?(?!\s*-?pre)|microphones?|cables?|cords?|snakes?|cases?|bags?|stands?|"
                   r"boots?|covers?|trays?|caps?|kit)\b", re.I)


def parse_channels(title: Optional[str]) -> Optional[int]:
    """Channel count from a title, or None if unclear / out of range / junk."""
    if not title or _JUNK.search(title):
        return None
    found = set()
    for m in _CH.finditer(title):
        found.add(int(m.group(1)))
    if len(found) != 1:  # none, or conflicting ("4 channel ... 8 channel")
        return None
    n = found.pop()
    return n if MIN_CH <= n <= MAX_CH else None


def per_channel(price, title: Optional[str]) -> Optional[dict]:
    """{'channels': n, 'each': float, 'text': '$X per channel', 'low': bool} or None."""
    n = parse_channels(title)
    if not n:
        return None
    try:
        if isinstance(price, str):
            from scrapers.enrich import parse_price
            price = parse_price(price)
        value = float(price) if price else 0.0
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    each = value / n
    text = f"${each:,.0f}" if each >= 10 else f"${each:,.2f}"
    return {"channels": n, "each": each, "text": f"{text} per channel", "low": each <= LOW_PER_CH}
