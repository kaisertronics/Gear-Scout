"""Motivated-seller radar: ads whose title or description says why the seller
wants it gone (moving, studio closing, estate, divorce, must sell, downsizing,
"gone this week"...). US phrasing. A hit means the seller is likely to take a
lower offer: the card gets a "Motivated seller" tag, the offer helper opens a
little lower, and Home lists the best current ones. Read-only; never touches
price rules or alerts.

  reasons / reason — pure text checks (unit-testable)
  pick / find      — the Home list: live ads, best discount vs used value first
"""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

MIN_UNDER_PCT = 10   # Home list: at least this far under used value
MAX_UNDER_PCT = 75   # further under is usually a scam or a mix-up (as on the hot deals list)
LIMIT = 6
RECENT_DAYS = 60     # Home list: ads first seen within this many days
DESC_CHARS = 1500    # past this it's usually shop boilerplate

# Not a life event: "moving coil", "moving fader", "moving magnet"...
_NOT_MOVE = re.compile(r"\bmoving[\s-]+(?:coil|fader|faders|magnet|iron|parts?|head|mass|average|picture|image|"
                       r"vehicle|target|element|ribbon|mechanism|platter)\b", re.I)
_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?"

# (label, pattern): first label wins as the headline reason
_PATTERNS = [
    ("Moving", re.compile(
        r"\b(?:i'?m|im|i\s+am|we'?re|we\s+are|i'?ll\s+be|we'?ll\s+be)\s+(?:currently\s+|now\s+)?(?:moving|relocating)\b"
        r"|\bmoving\s+(?:sale|out|away|soon|overseas|abroad|across|cross[\s-]country|next|this)\b"
        r"|\b(?:due\s+to|because\s+of)\s+(?:a\s+|my\s+|an\s+upcoming\s+)?(?:move|relocation)\b"
        r"|\brelocating\b|\brelocation\s+sale\b|\bbefore\s+(?:i|we)\s+move\b|\bmust\s+sell\s+before\s+moving\b", re.I)),
    ("Studio closing", re.compile(
        r"\bstudio\s+(?:is\s+)?(?:clos(?:ing|ed)|shutting\s+down|shut\s+down)\b"
        r"|\bclos(?:ing|ed)\s+(?:down\s+)?(?:my|our|the)\s+(?:\w+\s+)?(?:studio|shop|business)\b"
        r"|\b(?:clearing|cleaning)\s+out\s+(?:my|our|the)\s+(?:\w+\s+)?studio\b"
        r"|\bselling\s+(?:off\s+)?(?:my|our|the)\s+(?:whole\s+|entire\s+)?studio\b", re.I)),
    ("Liquidating", re.compile(r"\bgoing\s+out\s+of\s+business\b|\bliquidat(?:ing|ion)\b", re.I)),
    ("Estate", re.compile(
        r"\bestate\s+(?:sale|liquidation|clean[\s-]?out)\b|\b(?:from|of)\s+(?:an?|the|my|our)\s+(?:\w+\s+)?estate\b"
        r"|\bsettling\s+(?:an?\s+|the\s+|my\s+)?estate\b|\bdeceased\b|\bpassed\s+away\b|\bafter\s+(?:his|her)\s+passing\b", re.I)),
    ("Divorce", re.compile(r"\bdivorc(?:e|ed|ing)\b", re.I)),
    ("Must sell", re.compile(
        r"\bmust\s+sell\b|\bmust\s+be\s+sold\b|\bmust\s+go\b|\bhas\s+to\s+go\b|\bhave\s+to\s+sell\b|\bneed\s+to\s+sell\b"
        r"|\bneeds?\s+(?:it\s+|this\s+|them\s+)?gone\b|\bneed\s+(?:the\s+)?(?:cash|money|space|room)\b"
        r"|\bdesperate(?:ly)?\b|\burgent(?:ly)?\s+(?:sale|sell)|\bquick\s+sale\b|\bfire\s+sale\b"
        r"|\bmake\s+room\b|\bpay\s+(?:my\s+)?(?:rent|bills)\b", re.I)),
    ("Downsizing", re.compile(
        r"\bdownsiz(?:e|ed|ing)\b|\bthinning\s+(?:out|down)\s+(?:the\s+|my\s+)?(?:collection|gear|studio)\b"
        r"|\bpurging\b|\bgear\s+purge\b|\bquit(?:ting)?\s+(?:music|recording)\b|\bgetting\s+out\s+of\s+(?:music|recording)\b", re.I)),
    ("Needs gone soon", re.compile(
        r"\b(?:sell|sold|gone|go|need)\b[^.!?\n]{0,25}\b(?:this\s+week(?:end)?|by\s+(?:" + _DAY +
        r"|the\s+weekend|end\s+of\s+(?:the\s+)?(?:week|month)))\b", re.I)),
]
# Only a hint on its own (the card already says "Room to haggle"); adds to a reason
_OBO = re.compile(r"\bobo\b|\bor\s+best\s+offer\b", re.I)


def _text(r: dict) -> str:
    return f"{r.get('title') or ''}\n{(r.get('description') or '')[:DESC_CHARS]}"


def reasons(r: dict) -> list[str]:
    """Every motivated-seller reason the ad gives, in priority order ([] = none)."""
    text = _NOT_MOVE.sub(" ", _text(r))
    return [label for label, rx in _PATTERNS if rx.search(text)]


def reason(r: dict) -> Optional[str]:
    """One short card label ("Moving", "Estate + OBO"), None when the ad doesn't say."""
    rs = reasons(r)
    if not rs:
        return None
    return rs[0] + (" + OBO" if _OBO.search(_text(r)) else "")


def pick(rows: list[dict], used_of, now: Optional[datetime] = None) -> list[dict]:
    """Pure selection for Home. rows need title, price, description (+ optional
    shipping, first_seen); used_of(title) gives the used value. Fixed-price ads
    that give a reason and sit MIN_UNDER_PCT+ under used value, deepest first."""
    from scrapers.enrich import parse_price
    now = now or datetime.now(timezone.utc)
    out, seen = [], set()
    for r in rows:
        why = reason(r)
        if not why or r.get("url") in seen or (r.get("description") or "").startswith("Auction"):
            continue
        try:
            d = datetime.fromisoformat(r.get("first_seen") or "")
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            if now - d > timedelta(days=RECENT_DAYS):
                continue
        except (TypeError, ValueError):
            pass
        price = parse_price(r.get("price")) or 0
        if price <= 0 or "C$" in (r.get("price") or ""):  # used values are in US dollars
            continue
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        total = price + (ship or 0)
        used = used_of(r.get("title") or "")
        if not used or total > used * (1 - MIN_UNDER_PCT / 100) or total < used * (1 - MAX_UNDER_PCT / 100):
            continue
        seen.add(r.get("url"))
        r = dict(r)
        r["motivated_ad"] = {"reason": why, "used": round(used), "total": round(total, 2),
                             "pct": round((1 - total / used) * 100)}
        out.append(r)
    out.sort(key=lambda r: -r["motivated_ad"]["pct"])
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list[dict]:
    """Up to LIMIT live motivated-seller ads, best discount first."""
    from scrapers.enrich import is_partial, needs_repair
    from scrapers.market_value import value
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=RECENT_DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
            " AND COALESCE(pending, 0) = 0 AND first_seen >= ? AND (country IS NULL OR country = 'US')", (since,))]
    rows = [r for r in rows if reason(r) and not is_partial(r["title"])
            and not needs_repair(r["title"], r.get("description"))]
    return pick(rows, lambda t: value(t)[0], now)
