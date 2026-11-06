"""LA-2A and clone parts radar (owner proposal 681726c5). The owner stars UTC and
Sowter transformers, T4B opto cells and NOS tubes next to the LA-2A clones, but the
normal finders drop parts ads as "partial". This reads those ads on purpose: for each
part family (UTC A-18, Sowter, T4B, NOS 12AX7 ...) the median asking price comes from
the same family's own ads over the last PARTS_DAYS days, and a fresh ad well under it
is listed on Home with a ready message.

  family   — pure: title -> (family key, label) or None
  pick     — pure: rows -> fresh ads at least MIN_UNDER_PCT under their family median
  find     — the Home list (one windowed read; cached finder)
Read-only: nothing is sent."""
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

PARTS_DAYS = 180        # baseline window
FRESH_DAYS = 21         # an ad has to be this new to be listed
MIN_N = 4               # ads behind a family median before it is trusted
MIN_UNDER_PCT = 25
MAX_UNDER_PCT = 60      # further under is a mislabelled or broken part
MIN_PRICE = 25
MIN_SAVED = 30
LIMIT = 6

_TUBES = r"12ax7|ecc\s?83|12at7|ecc\s?81|12au7|ecc\s?82|12bh7|6072|5751|5814|6386|6973|ef\s?86|6sn7|6sl7|6v6|el34|el84|6922|6dj8|ecc\s?88"
_NOS_BRAND = r"nos|n\.o\.s|telefunken|siemens|mullard|amperex|valvo|tungsram|rft|sylvania|ge\b|rca|philips|raytheon|bugle\s?boy|genalex"
_FAMILIES = [
    (re.compile(r"\b(?:t4b|t-4b|t4\s+opto|kenetek)\b|\bopto[\s-]?(?:cell|attenuator|isolator)s?\b|\bel[\s-]?panel\b", re.I), lambda m, t: ("t4b", "T4B opto cell")),
    (re.compile(r"\butc\b[^.\n]{0,12}?\b((?:a|o|ls|hga|c)[\s-]?\d{1,3})\b", re.I), lambda m, t: ("utc:" + re.sub(r"[\s-]", "", m.group(1).lower()), f"UTC {m.group(1).upper()}")),
    (re.compile(r"\butc\b[^.\n]{0,25}\btransformers?\b|\btransformers?\b[^.\n]{0,25}\butc\b", re.I), lambda m, t: ("utc", "UTC transformer")),
    (re.compile(r"\bsowter\b[^.\n]{0,12}?\b(\d{4}[a-z]?)\b", re.I), lambda m, t: ("sowter:" + m.group(1).lower(), f"Sowter {m.group(1)}")),
    (re.compile(r"\bsowter\b", re.I), lambda m, t: ("sowter", "Sowter transformer")),
    (re.compile(r"\b(?:carnhill|marinair|cinemag|lundahl|jensen|haufe|edcor)\b[^.\n]{0,25}\btransformers?\b|\btransformers?\b[^.\n]{0,25}\b(?:carnhill|marinair|cinemag|lundahl|jensen|haufe|edcor)\b"
                r"|\b(?:ll|jt|cmqe|cmmm)[\s-]?\d{3,4}\b", re.I),
     lambda m, t: ("xfmr:" + (re.search(r"carnhill|marinair|cinemag|lundahl|jensen|haufe|edcor|\bll\b|\bjt\b|cmqe|cmmm", t, re.I) or m).group(0).lower().replace(" ", ""),
                   "Studio transformer")),
    (re.compile(rf"\b(?:{_NOS_BRAND})\b[^.\n]{{0,40}}?\b({_TUBES})\b|\b({_TUBES})\b[^.\n]{{0,40}}?\b(?:{_NOS_BRAND})\b", re.I),
     lambda m, t: ("tube:" + re.sub(r"\s", "", (m.group(1) or m.group(2)).lower()), "NOS " + re.sub(r"\s", "", (m.group(1) or m.group(2)).upper()))),
]
_NOT_PART = re.compile(r"\b(?:wanted|iso|wtb|looking\s+for)\b|\blot\s+of\s+\d{2,}|\bpedal\b|\bamp(?:lifier)?\b(?!\s+tubes?)|\bguitar\b|\bplugin\b|"
                       r"\b(?:di|d\.i\.|direct\s+box|splitter|radial|jdi|box|mix\s+bus|preamp|pre-?amp|compressor|equalizer|eq|mixer|console|mic|microphone|speaker|monitor)\b", re.I)
LIKE_TERMS = ["%t4b%", "%t-4b%", "%kenetek%", "%opto cell%", "%opto-cell%", "%optocell%", "%attenuator%", "%utc%", "%sowter%",
              "%carnhill%", "%marinair%", "%cinemag%", "%lundahl%", "%jensen%", "%haufe%", "%edcor%", "%transformer%",
              "%nos%", "%telefunken%", "%siemens%", "%mullard%", "%amperex%", "%valvo%", "%tungsram%", "%rft%",
              "%raytheon%", "%12ax7%", "%ecc83%", "%12at7%", "%12bh7%", "%6072%", "%5751%", "%ef86%", "%6386%"]


def family(title: Optional[str]) -> Optional[tuple]:
    """(family key, label) for a part ad, or None. First pattern that matches wins."""
    t = title or ""
    if _NOT_PART.search(t):
        return None
    for rx, mk in _FAMILIES:
        m = rx.search(t)
        if m:
            return mk(m, t)
    return None


def pick(rows: list, now: Optional[datetime] = None) -> list:
    """Pure. rows = priced ads from the baseline window (title, url, price, first_seen ...).
    Fresh ads at least MIN_UNDER_PCT under their family's median, biggest saving first."""
    from scrapers.enrich import parse_price
    from scrapers.finder_rows import total_cost
    from scrapers.instant_offer import suggest
    now = now or datetime.now(timezone.utc)
    fresh_cut = (now - timedelta(days=FRESH_DAYS)).isoformat()
    fams: dict = {}
    seen = set()
    for r in rows:
        if "C$" in (r.get("price") or "") or (r.get("description") or "").startswith("Auction"):
            continue
        f = family(r.get("title"))
        p = parse_price(r.get("price"))
        if not f or not p or p < MIN_PRICE:
            continue
        ad = (re.sub(r"\s+", " ", (r.get("title") or "").lower()), round(p))
        if ad in seen:
            continue
        seen.add(ad)
        fams.setdefault(f[0], []).append((p, f[1], r))
    out = []
    for key, items in fams.items():
        if len(items) < MIN_N:
            continue
        med = statistics.median(p for p, _l, _r in items)
        for p, label, r in items:
            if (r.get("first_seen") or "") < fresh_cut:
                continue
            cost = total_cost(r) or p
            pct = round((1 - cost / med) * 100)
            if not (MIN_UNDER_PCT <= pct <= MAX_UNDER_PCT) or med - cost < MIN_SAVED:
                continue
            ask = suggest(cost) or cost
            out.append(dict(r, part={"label": label, "median": round(med), "n": len(items), "pct": pct, "cost": round(cost)},
                            finder_note=f"Part: {label} usually asks ~${med:,.0f} (median of {len(items)} ads); this is ${cost:,.0f}, {pct}% under.",
                            finder_msg=(f"Hi! Is the {re.sub(chr(32) + '+', ' ', r.get('title') or 'part')[:70]} still available? "
                                        f"I can pay ${int(ask):,} shipped to Seattle and pay right away. Thanks!")))
    out.sort(key=lambda r: -(r["part"]["median"] - r["part"]["cost"]))
    deduped, urls = [], set()
    for r in out:
        if r.get("url") not in urls:
            urls.add(r.get("url"))
            deduped.append(r)
    return deduped[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR
    now = now or datetime.now(timezone.utc)
    rows = FR.clean(FR.recent_rows(PARTS_DAYS, LIKE_TERMS, now, limit=6000, priced=True), cfg)
    return pick(rows, now)
