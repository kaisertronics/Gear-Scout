"""Wanted-ad sniper (owner proposal 7df3165e). Sellers on Facebook, Craigslist,
OfferUp and Kijiji who post gear with NO price, or a placeholder price ($1, $0),
or a "must go / looking to sell / make an offer" cue are often the cheapest buys:
nobody anchored them to a number. Ranks the ones for buy-list gear first (by what
the model is worth used) and gives a ready message that asks the price.

  cues / placeholder / score — pure text and number checks
  pick / find               — the Home list (one windowed read; cached finder)
Read-only: never messages anyone."""
import re
from datetime import datetime, timezone
from typing import Callable, Optional

DAYS = 10
MIN_VALUE = 200         # worth a Home slot: the model's used value
PLACEHOLDER_MAX = 5     # "$1" / "$0" / "$2" asks are placeholders, not prices
LIMIT = 8

_CUES = [
    ("looking to sell", re.compile(r"\blooking\s+to\s+sell\b|\blts\b|\bready\s+to\s+sell\b|\bwilling\s+to\s+sell\b", re.I)),
    ("must go", re.compile(r"\bmust\s+(?:go|sell)\b|\bneeds?\s+(?:to\s+go|gone)\b|\bgotta\s+go\b|\bquick\s+sale\b|"
                           r"\bmoving\b|\bneed\s+(?:the\s+)?(?:cash|money|space)\b|\bdowns[iz]ing\b", re.I)),
    ("open to offers", re.compile(r"\b(?:make|send)\s+(?:me\s+)?an?\s+offer\b|\bobo\b|\bbest\s+offer\b|\bopen\s+to\s+offers\b|"
                                  r"\btaking\s+offers\b|\boffers?\s+welcome\b|\bname\s+your\s+price\b|\bpm\s+(?:me\s+)?for\s+price\b", re.I)),
]


def cues(title: Optional[str], description: Optional[str] = None) -> list:
    text = f"{title or ''}\n{(description or '')[:600]}"
    return [label for label, rx in _CUES if rx.search(text)]


def placeholder(price) -> str:
    """'none' (no price at all), 'token' ($0 / $1 ... a stand-in) or '' (a real price)."""
    from scrapers.enrich import parse_price
    if not price or not str(price).strip():
        return "none"
    if re.search(r"\bfree\b|\bcontact\b|\binquire\b|\bmake offer\b|\bobo\b", str(price), re.I) and not re.search(r"\d", str(price)):
        return "none"
    p = parse_price(price)
    if p is None:
        return "none"
    return "token" if p <= PLACEHOLDER_MAX else ""


def score(used: float, buylist: bool, n_cues: int) -> float:
    return used + (600 if buylist else 0) + 150 * n_cues


def _msg(title: str) -> str:
    from scrapers.finder_rows import short
    return (f"Hi! Is the {short(title)} still available? What price are you looking for? "
            f"I'm a serious buyer and can pay cash and pick up quickly. Thanks!")


def pick(rows: list, value_of: Callable, now: Optional[datetime] = None) -> list:
    """Pure. rows: local-site ads. value_of(title) -> used value or None."""
    from scrapers.finder_rows import BUYLIST, is_local
    out, seen = [], set()
    for r in rows:
        if r.get("url") in seen or not is_local(r.get("source_name")):
            continue
        kind = placeholder(r.get("price"))
        title = r.get("title") or ""
        cs = cues(title, r.get("description"))
        if not kind:
            continue            # a real price is shown: the deal finders cover it
        used = value_of(title)
        bl = bool(BUYLIST.search(title))
        if used and used < MIN_VALUE:
            continue
        if not used:
            if not bl:
                continue        # unknown model that isn't on the buy list: skip
            used = 0
        seen.add(r.get("url"))
        why = ("no price listed" if kind == "none" else "placeholder price") + (" · " + ", ".join(cs) if cs else "")
        note = f"Wanted-ad sniper: {why}" + (f" · worth ~${used:,.0f} used" if used else " · on your buy list")
        out.append(dict(r, finder_note=note, finder_msg=_msg(title), sniper={"score": score(used, bl, len(cs)), "used": round(used)}))
    out.sort(key=lambda r: -r["sniper"]["score"])
    return out[:LIMIT * 3]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR, market_value as mv
    from scrapers.enrich import is_partial, needs_repair
    now = now or datetime.now(timezone.utc)
    rows = FR.recent_rows(DAYS, [], now, limit=2500, local_only=True, no_real_price=True)
    rows = [r for r in FR.clean(rows, cfg) if not is_partial(r["title"])
            and not needs_repair(r["title"], r.get("description"))]
    return pick(rows, lambda t: mv.value(t)[0], now)
