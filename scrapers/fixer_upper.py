"""Fixer-upper finder (owner proposal 0c9eb2d1). The owner stars as-is vintage gear
(the $50 Beyer M55). Ads saying "for parts", "not working" or "needs tubes" on
high-value mics and LA-2A-style comps get their own Home section: a typical repair
cost is subtracted from the model's used (working) value, so the card shows the
profit if fixed.

  repair_cost — pure: wording + kind of gear -> (label, dollars)
  assess      — pure: one ad -> {used, cost, repair, repair_label, profit, pct} or None
  pick / find — the Home list (one windowed read; cached finder)
The repair costs are rough typical bench prices, shown as an estimate. Read-only."""
import re
from datetime import datetime, timezone
from typing import Callable, Optional

DAYS = 30
MIN_USED = 400          # high-value gear only: a repair has to be worth doing
MAX_USED = 8000         # above this the used value is too thin to trust for a profit estimate
MIN_PROFIT = 200
MIN_PROFIT_PCT = 25     # profit at least this share of the working value
MAX_PRICE_PCT = 60      # the ask must be at most this share of the working value
LIMIT = 6

# First match wins (most specific first): (pattern, label, typical repair $)
_WORDING = [
    (re.compile(r"\bcapsule\b[^.\n]{0,30}\b(?:damaged|dead|bad|torn|needs?|broken)|\b(?:damaged|dead|bad|torn|broken)\b[^.\n]{0,20}\bcapsule\b"
                r"|\bneeds?\s+(?:a\s+)?(?:new\s+)?(?:capsule|re[\s-]?skin|diaphragm)", re.I), "capsule / re-skin", 350),
    (re.compile(r"\bneeds?\s+(?:new\s+|a\s+)?(?:re)?caps?\b|\brecap\b|\bcap\s+job\b|\bbad\s+caps?\b|\bcaps?\s+(?:are\s+)?bad", re.I), "recap", 250),
    (re.compile(r"\bneeds?\s+(?:new\s+|a\s+|replacement\s+)?tubes?\b|\btubes?\s+(?:are\s+)?(?:bad|weak|dead|noisy|microphonic)\b"
                r"|\bno\s+tubes?\b|\bmissing\s+tubes?\b|\bwithout\s+tubes?\b", re.I), "tubes", 150),
    (re.compile(r"\b(?:power\s+supply|psu|transformer)\b[^.\n]{0,25}\b(?:bad|dead|blown|burn(?:ed|t)|needs?|broken|fail(?:ed|s)?)|"
                r"\bno\s+power\b|\bwon'?t\s+(?:power|turn)\s+on\b|\bdead\b", re.I), "power supply", 200),
    (re.compile(r"\bnot\s+working\b|\bnon[\s-]?working\b|\bdoesn'?t\s+work\b|\bdoes\s+not\s+work\b|\bfor\s+(?:parts|repair)\b|\bbroken\b|"
                r"\bfaulty\b|\bno\s+sound\b|\bno\s+output\b|\bblown\b|\bneeds?\s+(?:a\s+)?(?:repair|work|service|fix)\b|"
                r"\brepair\s+project\b|\bproject\b", re.I), "general repair", 300),
    (re.compile(r"\bas[\s-]is\b|\buntested\b|\bunknown\s+condition\b|\bsold\s+as\s+is\b", re.I), "check-over", 120),
]
_FIXABLE = re.compile(r"\bcrackl|\bscratch|\bnoisy\b|\bhum\b", re.I)
# Only whole units: not a part, a case, or a "for parts" shell of something tiny
_TARGET = re.compile(r"\b(?:mic|microphone|condenser|ribbon|ribbons|u\s?47|u\s?67|u\s?87|km\s?84|c[\s-]?12|c[\s-]?24|c[\s-]?414|414|"
                     r"m\s?49|m\s?50|m\s?55|ela\s?m|cmv|tube|valve|la[\s-]?2a|la[\s-]?3a|1176|opto|compressor|limiter|"
                     r"pultec|eqp[\s-]?1a|fairchild|660|670|pre[\s-]?amp|preamp|neve|1073|api|ssl|distressor|bt[\s-]?50)\b", re.I)
_REPAIR_TEXT = re.compile("|".join(rx.pattern for rx, _l, _c in _WORDING), re.I)
_SHELL = re.compile(r"\b(?:capsule|case|grille|tubes?|psu|power supply|cable|mount)s?\s+only\b|\b(?:outer\s+)?(?:shell|housing|body)\b|\bempty\b|\breplica\b", re.I)
LIKE_TERMS = ["%parts%", "%not working%", "%non working%", "%non-working%", "%needs%", "%as-is%", "%as is%", "%untested%",
              "%broken%", "%repair%", "%project%", "%faulty%", "%no power%", "%dead%", "%blown%", "%tubes%", "%recap%",
              "%doesn't work%", "%doesnt work%", "%no sound%", "%capsule%"]


def repair_cost(title: Optional[str], description: Optional[str] = None) -> Optional[tuple]:
    """(label, dollars) for the first repair wording found in the title, then the start of the description."""
    for text in (title or "", (description or "")[:600]):
        for rx, label, cost in _WORDING:
            if rx.search(text):
                return label, cost
    return None


def strip_repair(title: Optional[str]) -> str:
    """The title without the repair wording, to look up the WORKING unit's value."""
    t = re.sub(_REPAIR_TEXT, " ", title or "")
    t = re.sub(r"\b(?:for|parts|repair|project|untested|as[\s-]is|broken|dead)\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -,.:;/")


def assess(title: Optional[str], description: Optional[str], cost, value_of: Callable) -> Optional[dict]:
    """The profit-if-fixed math for one ad, or None when it doesn't qualify."""
    try:
        if not title or not cost or float(cost) <= 0 or not _TARGET.search(title):
            return None
        rep = repair_cost(title, description)
        if not rep:
            return None
        cost = float(cost)
        used = value_of(strip_repair(title))
        if not used or used < MIN_USED or used > MAX_USED:
            return None
        label, fix = rep
        profit = used - cost - fix
        pct = round(profit / used * 100)
        if profit < MIN_PROFIT or pct < MIN_PROFIT_PCT or cost > used * MAX_PRICE_PCT / 100:
            return None
        return {"used": round(used), "cost": round(cost), "repair": fix, "repair_label": label,
                "profit": round(profit), "pct": pct}
    except Exception:
        return None


def pick(rows: list, value_of: Callable, now: Optional[datetime] = None) -> list:
    """Pure. Fixed-price repair-wording ads that clear the thresholds, biggest profit first."""
    from scrapers.finder_rows import total_cost
    out, seen = [], set()
    for r in rows:
        if r.get("url") in seen or (r.get("description") or "").startswith("Auction") or "C$" in (r.get("price") or ""):
            continue
        cost = total_cost(r)
        a = assess(r.get("title"), r.get("description"), cost, value_of) if cost else None
        if not a:
            continue
        seen.add(r.get("url"))
        out.append(dict(r, fixer=a, finder_note=(
            f"Fixer-upper: worth ~${a['used']:,} working − ${a['cost']:,} ask − ~${a['repair']:,} typical {a['repair_label']} "
            f"= ~${a['profit']:,} profit if fixed ({a['pct']}%). Estimate; check the photos and ask what's wrong.")))
    out.sort(key=lambda r: -r["fixer"]["profit"])
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR, market_value as mv
    from scrapers.enrich import is_partial
    now = now or datetime.now(timezone.utc)
    rows = FR.clean(FR.recent_rows(DAYS, LIKE_TERMS, now, limit=3000, priced=True), cfg)
    rows = [r for r in rows if repair_cost(r["title"], r.get("description")) and not is_partial(strip_repair(r["title"]))
            and not _SHELL.search(r["title"])]
    return pick(rows, lambda t: mv.value(t)[0], now)
