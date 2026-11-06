"""Pair-up finder (owner proposal f1e6e340). The buy list wants matched pairs
(1176 x2, EQP-1A x2, BT-50 x2, Advent x2, Peluso x2, Hedd 20s ...). Sellers list
singles one at a time, on different sites or from different sellers. This matches
two separate single ads of the same model whose combined cost (price + shipping)
is under what a pair is worth used (2 x the single used value), and gives each
seller a ready message that mentions the pair.

  is_single / wanted — pure title checks
  pick               — pure: group, pick the cheapest two, test the combined price
  find               — the Home list (one windowed read; Home calls it through the cached finder)
Read-only: it never sends a message or changes a price rule."""
import re
from datetime import datetime, timezone
from typing import Callable, Optional

DAYS = 21
MIN_UNDER_PCT = 12       # combined cost at least this far under 2 x used value
MAX_UNDER_PCT = 50       # further under is a part, a variant or a mix-up (50%+ singles already show as hot deals)
MIN_OF_VALUE = 0.4       # each ad must cost at least this share of one used value (not a connector or a cable)
MAX_PRICE_RATIO = 1.8    # the two asks within this factor of each other = same kind/condition
MIN_SINGLE_VALUE = 150   # a pair of $60 items isn't worth a Home slot
LIMIT = 4

# Gear the owner wants two of (docs/owner_purchases.md) plus other things that naturally go in pairs.
PAIR_WANTED = re.compile(
    r"\b(?:1176|la[\s-]?2a|la[\s-]?3a|eqp[\s-]?1a|pultec|bt[\s-]?50|advent|peluso|u\s?47|u\s?67|u\s?87|km\s?84|km\s?184|"
    r"c[\s-]?56|c[\s-]?451|414|hedd|adam\s+a7|ns[\s-]?10|yamaha\s+hs|genelec|neve\s+1073|api\s+312|api\s+550|"
    r"vp26|v12|m\s?49|cmv|ribbon|r[\s-]?84|r[\s-]?121|sm[\s-]?81|d12|d\s?112|distressor|capi|vc528|"
    r"missing\s+link|a[\s-]?1176)\b", re.I)
# Already a pair, a set or several at one price, or a part
_MULTI = re.compile(r"\bpairs?\b|\bmatched\b|\bstereo\b|\bset\s+of\b|\blot\b|\bbundle\b|\(\s*[2-9]\s*\)|\b[2-9]\s?x\b|"
                    r"\bx\s?[2-9]\b|\b(?:two|2|both|three|four)\b\s+(?:mics?|units?|pieces?)|\bquad\b|\btrio\b|&", re.I)
LIKE_TERMS = ["%1176%", "%la-2a%", "%la2a%", "%la 2a%", "%la-3a%", "%eqp%", "%pultec%", "%bt-50%", "%bt50%", "%advent%",
              "%peluso%", "%u47%", "%u 47%", "%u67%", "%u87%", "%km84%", "%km 84%", "%km184%", "%c-56%", "%c56%",
              "%c-451%", "%414%", "%hedd%", "%adam a7%", "%ns-10%", "%genelec%", "%1073%", "%api 312%", "%api 550%",
              "%vp26%", "%v12%", "%ribbon%", "%r-84%", "%r-121%", "%d12%", "%distressor%", "%capi%", "%vc528%",
              "%missing link%"]


def wanted(title: Optional[str]) -> bool:
    return bool(PAIR_WANTED.search(title or ""))


def is_single(title: Optional[str]) -> bool:
    return not _MULTI.search(title or "")


def _compatible(a: dict, b: dict) -> bool:
    """Shipped ads pair with shipped ads; local-pickup ads only with ads from the same region."""
    from scrapers.finder_rows import is_local
    la, lb = is_local(a.get("source_name")), is_local(b.get("source_name"))
    if la or lb:
        return a.get("source_name") == b.get("source_name")
    return True


def _msg(title: str, amount: int, pair: bool) -> str:
    from scrapers.finder_rows import short
    return (f"Hi! Is the {short(title)} still available? I'm putting together a matched pair and can pay "
            f"${int(amount):,} shipped to Seattle and pay right away. Thanks!")


def pick(rows: list, value_of: Callable, key_of: Callable, now: Optional[datetime] = None) -> list:
    """Pure. rows need title, url, price (+ shipping, source_name, seller).
    value_of(title) -> used value of ONE (or None); key_of(title) -> model key.
    Returns [{"key","used_pair","total","pct","a","b","same_seller"}], best saving first."""
    from scrapers.finder_rows import total_cost
    from scrapers.instant_offer import suggest
    groups: dict = {}
    seen_urls = set()
    for r in rows:
        t = r.get("title") or ""
        if r.get("url") in seen_urls or not is_single(t) or not wanted(t):
            continue
        cost = total_cost(r)
        if not cost or "C$" in (r.get("price") or ""):
            continue
        k = key_of(t)
        if not k:
            continue
        seen_urls.add(r.get("url"))
        groups.setdefault(k, []).append((cost, r))
    out = []
    for k, items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0])
        best = None
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ca, ra), (cb, rb) = items[i], items[j]
                if not _compatible(ra, rb):
                    continue
                single = value_of(ra.get("title")) or value_of(rb.get("title"))
                if not single or single < MIN_SINGLE_VALUE:
                    continue
                if min(ca, cb) < single * MIN_OF_VALUE or max(ca, cb) > min(ca, cb) * MAX_PRICE_RATIO:
                    continue
                pair_val, total = single * 2, ca + cb
                if total > pair_val * (1 - MIN_UNDER_PCT / 100) or total < pair_val * (1 - MAX_UNDER_PCT / 100):
                    continue
                best = (ca, ra, cb, rb, pair_val, total)
                break
            if best:
                break
        if not best:
            continue
        ca, ra, cb, rb, pair_val, total = best
        a, b = dict(ra), dict(rb)
        pct = round((1 - total / pair_val) * 100)
        for x, c in ((a, ca), (b, cb)):
            x["finder_note"] = (f"Pair-up: this + the other = ${total:,.0f} vs ~${pair_val:,.0f} for a used pair "
                                f"({pct}% under)")
            x["finder_msg"] = _msg(x.get("title"), suggest(c) or c, True)
        out.append({"key": k, "used_pair": round(pair_val), "total": round(total), "pct": pct, "a": a, "b": b,
                    "same_seller": bool(ra.get("seller") and ra.get("seller") == rb.get("seller")
                                        and ra.get("source_name") == rb.get("source_name"))})
    out.sort(key=lambda p: -(p["used_pair"] - p["total"]))
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    """Up to LIMIT pairs. One windowed read of recent ads for pair-wanted gear."""
    from scrapers.enrich import is_partial, needs_repair
    from scrapers import finder_rows as FR, market_value as mv
    now = now or datetime.now(timezone.utc)
    rows = FR.clean(FR.recent_rows(DAYS, LIKE_TERMS, now, priced=True), cfg)
    rows = [r for r in rows if not is_partial(r["title"]) and not needs_repair(r["title"], r.get("description"))
            and not (r.get("description") or "").startswith("Auction") and not mv.skip_listing(r["title"])]
    return pick(rows, lambda t: mv.value(t)[0], mv._key, now)
