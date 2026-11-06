"""Bulk-lot splitter: for an ad that is a LOT of several different items
("Lot: SM57, SM58, AKG 414, cables $900"), value each recognisable item with
the used-value index (market_value.value), sum them, and report the total
against the ask: "Lot parts ~ $1,240 vs ask $900 (27% under)".

Reuses bundle_breakdown.split_pieces for splitting. Card chip only. Lower bar
than the Bundle tag (10%+ under, not 40%) because a lot word makes the intent
explicit; skipped when the Bundle tag already covers the ad. Never raises.
"""
import re
from typing import Callable, Optional

MIN_ITEMS = 2
MIN_PCT_UNDER = 10
MAX_QTY = 10

_LOT_WORD = re.compile(r"\b(?:lot|lots|job lot|bulk|collection|assorted|mixed)\b", re.I)
_LEAD = re.compile(r"^\s*(?:(?:job\s+)?lots?|bulk|collection|assorted|mixed)\s*(?:of)?\s*[:\-]?\s*", re.I)
_PRICE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\s?(?:usd|obo|firm)\b", re.I)


def is_lot(title: Optional[str]) -> bool:
    return bool(_LOT_WORD.search(title or ""))


def _qty_and_name(piece: str) -> tuple[int, str]:
    """'2x SM57' / 'SM57 x2' -> (2, 'SM57'). A bare leading number without an
    x ('414 XLS') is part of the model name, so it needs the x."""
    m = re.match(r"^\s*(\d{1,2})\s*[x×]\s*(.+)$", piece, re.I)
    if m:
        return min(int(m.group(1)), MAX_QTY), m.group(2).strip()
    m = re.match(r"^\s*([2-9])\s+([A-Za-z]{3,}.*)$", piece)  # "3 Shure SM58"
    if m:
        return int(m.group(1)), m.group(2).strip()
    m = re.match(r"^(.+?)\s[x×]\s?(\d{1,2})\s*$", piece, re.I)
    if m:
        return min(int(m.group(2)), MAX_QTY), m.group(1).strip()
    return 1, piece.strip()


def split_lot(title: Optional[str]) -> list[str]:
    """Item phrases from a lot title: lead word and prices removed."""
    from scrapers.bundle_breakdown import split_pieces
    t = _PRICE.sub(" ", title or "")
    t = _LEAD.sub("", t.strip())
    return split_pieces(t)


def value_parts(pieces: list[str], valuer: Optional[Callable] = None) -> list[dict]:
    """[{name, qty, each, value}] for pieces the index can value. Duplicate
    models are merged by name. valuer(name) -> price or None (injectable)."""
    if valuer is None:
        from scrapers import market_value as mv

        def valuer(name):
            if mv.skip_listing(name):
                return None
            return mv.value(name)[0]
    out, seen = [], set()
    for p in pieces:
        qty, name = _qty_and_name(p)
        if not name:
            continue
        try:
            v = valuer(name)
        except Exception:
            v = None
        if not v or v <= 0:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "qty": qty, "each": round(v), "value": round(v * qty)})
    return out


def lot_chip(title: Optional[str], description: Optional[str], ask,
             valuer: Optional[Callable] = None) -> Optional[dict]:
    """{'text','title','total','ask','pct_under','items'} or None."""
    try:
        if not is_lot(title) or not ask or float(ask) <= 0:
            return None
        ask = float(ask)
        items = value_parts(split_lot(title), valuer)
        if len(items) < MIN_ITEMS and description:
            items = value_parts(split_lot(title) + split_lot((description or "")[:300]), valuer)
        if len(items) < MIN_ITEMS:
            return None
        total = sum(i["value"] for i in items)
        if total <= 0:
            return None
        pct = round((1 - ask / total) * 100)
        if pct < MIN_PCT_UNDER:
            return None
        detail = "; ".join(
            "%s%s ~$%s" % ((str(i["qty"]) + "x ") if i["qty"] > 1 else "", i["name"], format(i["value"], ","))
            for i in items)
        return {
            "text": "Lot parts ≈ $%s vs ask $%s (%d%% under)" % (format(total, ","), format(round(ask), ","), pct),
            "title": "Used value of the recognisable items: " + detail + ". Unrecognised items (cables, cases) are not counted.",
            "total": total, "ask": round(ask), "pct_under": pct, "items": items,
        }
    except Exception:
        return None
