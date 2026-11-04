"""Lot hunter (owner proposal 316a4e7d). The biggest buys were lots (CAPI modules
x8, studio gear from one seller). Finds live multi-item ads (a lot / bundle /
"8x" of one model) whose ask is well under the sum of the parts' used values and
lists them on Home, biggest dollar saving first.

Reuses lot_splitter (split + value the pieces) and the used-value index; adds
single-model quantity lots ("CAPI VC528 x8 $2400" -> 8 x the used value).
  assess — pure: one ad -> {parts, total, ask, pct, saved, items} or None
  pick   — pure: choose the best ads
  find   — the Home list (one windowed read; Home calls it through the cached finder)
Read-only."""
import re
from datetime import datetime, timezone
from typing import Callable, Optional

DAYS = 21
MIN_UNDER_PCT = 25     # ask at least this far under the sum of the parts
MAX_UNDER_PCT = 70     # further under is usually "only some of it is real" / a scam
MIN_ASK = 150
MIN_SAVED = 150        # dollars under the parts total
LIMIT = 6

_LOTISH = re.compile(r"\blots?\b|\bbundle\b|\bcollection\b|\bassorted\b|\bbulk\b|\bset\s+of\s+\d|\bjob\s+lot\b|"
                     r"\(\s*[2-9]\s*\)|\b[2-9]\s?x\b|\bx\s?[2-9]\b|\b(?:two|three|four|five|six|eight)\b|[+&]|,", re.I)
LIKE_TERMS = ["%lot%", "%bundle%", "%collection%", "%bulk%", "%set of%", "% x2%", "% x3%", "% x4%", "% x5%", "% x6%",
              "% x8%", "%2x%", "%3x%", "%4x%", "%5x%", "%6x%", "%8x%", "%(2)%", "%(3)%", "%(4)%", "%(6)%", "%(8)%",
              "%+%", "% & %", "%, %", "%pair%"]


_X_QTY = re.compile(r"^(.*?)\s*\b[x×]\s?(\d{1,2})\b\s*(.*)$", re.I)


def _lead_qty(piece: str) -> str:
    """'CAPI VC528 x8 modules' -> '8x CAPI VC528 modules' (the splitter reads a leading 'Nx')."""
    m = _X_QTY.match(piece)
    if m and not re.match(r"^\s*\d{1,2}\s*[x×]", piece):
        return "%sx %s" % (m.group(2), re.sub(r"\s+", " ", (m.group(1) + " " + m.group(3))).strip())
    return piece


def assess(title: Optional[str], description: Optional[str], ask, valuer: Optional[Callable] = None) -> Optional[dict]:
    """Value the recognisable pieces of a multi-item ad against its ask. The
    quantity of a single model counts ("8x VC528"). Never raises."""
    from scrapers import lot_splitter as LS
    try:
        if not title or not ask or float(ask) <= 0 or not _LOTISH.search(title):
            return None
        ask = float(ask)
        pieces = [_lead_qty(p) for p in LS.split_lot(title)]
        items = LS.value_parts(pieces, valuer)
        if not items and description:
            items = LS.value_parts(LS.split_lot((description or "")[:300]), valuer)
        units = sum(i["qty"] for i in items)
        if units < 2:
            return None
        total = sum(i["value"] for i in items)
        if total <= 0:
            return None
        pct = round((1 - ask / total) * 100)
        return {"items": items, "total": total, "ask": round(ask), "pct": pct, "saved": round(total - ask), "units": units}
    except Exception:
        return None


def _note(a: dict) -> str:
    parts = "; ".join(("%dx " % i["qty"] if i["qty"] > 1 else "") + "%s ~$%s" % (i["name"][:28], format(i["value"], ","))
                      for i in a["items"][:5])
    return "Lot parts ≈ $%s vs ask $%s (%d%% under, %d items): %s" % (
        format(a["total"], ","), format(a["ask"], ","), a["pct"], a["units"], parts)


def pick(rows: list, valuer: Optional[Callable] = None, now: Optional[datetime] = None) -> list:
    """Pure. Fixed-price multi-item ads that clear the thresholds, biggest saving first."""
    from scrapers.finder_rows import total_cost
    out, seen = [], set()
    for r in rows:
        if r.get("url") in seen or (r.get("description") or "").startswith("Auction") or "C$" in (r.get("price") or ""):
            continue
        cost = total_cost(r)
        if not cost or cost < MIN_ASK:
            continue
        a = assess(r.get("title"), r.get("description"), cost, valuer)
        if not a or a["pct"] < MIN_UNDER_PCT or a["pct"] > MAX_UNDER_PCT or a["saved"] < MIN_SAVED:
            continue
        seen.add(r.get("url"))
        r = dict(r, lot_hunt=a, finder_note=_note(a))
        out.append(r)
    out.sort(key=lambda r: -r["lot_hunt"]["saved"])
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR, market_value as mv
    from scrapers.enrich import is_partial, needs_repair
    now = now or datetime.now(timezone.utc)
    rows = FR.clean(FR.recent_rows(DAYS, LIKE_TERMS, now, limit=3000, priced=True), cfg)
    rows = [r for r in rows if _LOTISH.search(r["title"]) and not needs_repair(r["title"], r.get("description"))]

    def valuer(name):
        return None if mv.skip_listing(name) or is_partial(name) else mv.value(name)[0]
    return pick(rows, valuer, now)
