"""Bundle breakdown: for an ad selling several items together, split it into
pieces, value each with the existing used-value index (market_value.value),
and report the total when the ask is 40%+ under it. Card tag only."""
import re
from typing import Optional

MIN_PIECES = 2
MIN_PCT_UNDER = 40
_SPLIT = re.compile(r"\s(?:&|\+|and|plus|w/|with)\s|\s*\+\s*|,\s|\n|;|\s/\s", re.I)
_NOISE = re.compile(r"\b(?:bundle|package deal|package|lot of|full setup|combo deal|includes?|included|for sale)\b", re.I)


def split_pieces(text: Optional[str]) -> list[str]:
    out = []
    for p in _SPLIT.split(text or ""):
        p = _NOISE.sub(" ", p)
        p = re.sub(r"\s+", " ", p).strip(" -:.*•()\"'")
        if 3 <= len(p) <= 80:
            out.append(p)
    return out


def _valued(pieces: list[str], seen: set, items: list) -> None:
    from scrapers import market_value as mv
    for p in pieces:
        if mv.skip_listing(p):
            continue
        k = mv._key(p)
        if not k or k in seen:
            continue
        v, _n = mv.value(p)
        if v and v > 0:
            seen.add(k)
            items.append({"name": p, "value": round(v)})


def breakdown(title: Optional[str], description: Optional[str], ask) -> Optional[dict]:
    """{"items":[{"name","value"}], "total", "ask", "pct_under"} or None."""
    try:
        from scrapers.enrich import is_bundle
        if not ask or float(ask) <= 0 or not is_bundle(title):
            return None
        ask = float(ask)
        items: list = []
        seen: set = set()
        _valued(split_pieces(title), seen, items)
        if len(items) < MIN_PIECES and description:
            _valued(split_pieces((description or "")[:300]), seen, items)
        if len(items) < MIN_PIECES:
            return None
        total = sum(i["value"] for i in items)
        pct = round((1 - ask / total) * 100)
        if total <= 0 or pct < MIN_PCT_UNDER:
            return None
        return {"items": items, "total": total, "ask": round(ask), "pct_under": pct}
    except Exception:
        return None
