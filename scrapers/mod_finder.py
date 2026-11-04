"""Mod and upgrade value finder (owner proposal 23919bd7). The owner starred a $429
Apex 460 with a Peluso C12 capsule and a Lundahl transformer: a budget body priced
like a stock one while the upgrade parts alone are worth hundreds. Ads that say a
capsule or transformer was swapped get a "Modded: body + parts" tag and a Home list.

  parts_in   — pure: title (+ start of description) -> [(label, dollars)] upgrade parts named
  body_title — pure: the title with the mod wording removed, to look up the stock body
  assess     — pure: one ad -> {body, parts, worth, pct, labels} or None
  pick / find — the Home list (one windowed read; cached finder)
Part values are rough used resale prices, shown as an estimate. Read-only."""
import re
from datetime import datetime, timezone
from typing import Callable, Optional

DAYS = 30
MAX_BODY = 6000         # above this the stock value is too thin to trust (a rebuilt U47)
MIN_BODY = 150          # the stock body has to be a real mic with a known value
MIN_PARTS = 150         # the upgrades have to matter
MAX_PARTS_X_BODY = 2.0  # parts worth more than 2x the body: the model match is probably wrong
MIN_UNDER_PCT = 15      # ask at least this far under body + parts
MAX_UNDER_PCT = 60
MIN_ASK_X_BODY = 0.6    # ask at least this share of the stock body (below = a different thing / part)
LIMIT = 6

# (pattern, label, used resale $). Pattern names the part; a mod word must also be present.
_PARTS = [
    (re.compile(r"\bpeluso\b[^.\n]{0,25}\bcapsule\b|\bcapsule\b[^.\n]{0,25}\bpeluso\b|\bpeluso\s+(?:c[\s-]?12|ck[\s-]?12|22|u\s?47)\b", re.I), "Peluso capsule", 300),
    (re.compile(r"\b(?:ck[\s-]?12|c[\s-]?12|k[\s-]?67|k[\s-]?47|m7|t[\s-]?47|ela\s?m)\b[^.\n]{0,12}\bcapsule\b|\bcapsule\b[^.\n]{0,12}\b(?:ck[\s-]?12|c[\s-]?12|k[\s-]?67|k[\s-]?47)\b(?![\s-]?style)", re.I), "vintage-style capsule", 250),
    (re.compile(r"\b(?:new|upgraded|replacement|replaced|swapped|custom)\s+(?:\w+\s+){0,2}capsule\b|\bcapsule\s+(?:upgrade|swap|replaced|replacement)\b", re.I), "upgraded capsule", 150),
    (re.compile(r"\blundahl\b", re.I), "Lundahl transformer", 200),
    (re.compile(r"\bjensen\b", re.I), "Jensen transformer", 175),
    (re.compile(r"\bcinemag\b", re.I), "Cinemag transformer", 125),
    (re.compile(r"\b(?:haufe|carnhill|sowter|edcor|ocean\s?way|st[\s-]?\d{2,3}|ep[\s-]?\d{3})\b[^.\n]{0,15}\btransformer\b|\b(?:new|upgraded|custom)\s+transformer\b|\btransformer\s+upgrade\b", re.I), "upgraded transformer", 125),
    (re.compile(r"\b(?:telefunken|siemens|mullard|nos|rft)\b[^.\n]{0,20}\b(?:tube|valve|ef\s?86|ecc\s?8\d|12ax7)\b", re.I), "NOS tube", 100),
]
# The upgrade has to be claimed, not just the mic's stock spec ("Cascade ribbon with Lundahl" ships that way).
_MOD_WORD = re.compile(r"\b(?:mod(?:ded|ified|ification)?|upgrad\w*|replaced|replacement|swapped|retrofit\w*|rewired|re[\s-]?capsuled|"
                       r"installed|custom(?:ized|ised)?|rebuilt|hot[\s-]?rod\w*|improved|new\s+(?:capsule|transformer|trans))\b", re.I)
_MIC = re.compile(r"\b(?:mic|microphone|condenser|ribbon|apex|u\s?47|u\s?67|u\s?87|k[\s-]?47|k[\s-]?67|c[\s-]?12|ela\s?m|cmv|km\s?\d+|"
                  r"tlm|at\d{4}|ck\d+|rode|nt\d|mxl|cad|sterling|behringer|samson|audio-technica|akg|aea|royer|cascade)\b", re.I)
_SHELL = re.compile(r"\b(?:capsule|transformer|trans|tube|valve|psu|power supply|cable|mount|case)s?\s+only\b|\bfor\s+parts\b|\bparts\s+only\b|"
                    r"\b(?:head|capsule|transformer)\s+(?:assembly|kit)\b|\breplica\b|\bwarm\s+audio\b|\b-?style\b", re.I)
LIKE_TERMS = ["%modded%", "%modified%", "%mod %", "%mod)%", "%upgrade%", "%capsule%", "%transformer%", "%lundahl%",
              "%cinemag%", "%jensen%", "%peluso%", "%replaced%", "%swapped%", "%retrofit%", "%rewired%", "%custom%",
              "%rebuilt%", "%hot rod%"]


def _claimed(text: str, m: "re.Match") -> bool:
    """A mod word within ~70 characters of the part: the upgrade is claimed, not the mic's stock spec."""
    return bool(_MOD_WORD.search(text[max(0, m.start() - 70):m.end() + 70]))


def parts_in(title: Optional[str], description: Optional[str] = None) -> list:
    """[(label, dollars)] for each upgrade part named next to a mod word (a Peluso capsule needs none)."""
    text = f"{title or ''} . {(description or '')[:500]}"
    out, seen = [], set()
    for i, (rx, label, val) in enumerate(_PARTS):
        for m in rx.finditer(text):
            if label not in seen and (_claimed(text, m) or (i == 0 and m.start() < len(title or ""))):
                seen.add(label)
                out.append((label, val))
                break
    # one capsule line, one transformer line: take the most valuable of each family
    caps = [p for p in out if "capsule" in p[0]]
    trans = [p for p in out if "transformer" in p[0]]
    return ([max(caps, key=lambda p: p[1])] if caps else []) + ([max(trans, key=lambda p: p[1])] if trans else []) \
        + [p for p in out if "capsule" not in p[0] and "transformer" not in p[0]]


def body_title(title: Optional[str]) -> str:
    """The title without the upgrade wording, to look up the STOCK mic's value."""
    t = title or ""
    for rx, _l, _v in _PARTS:
        t = rx.sub(" ", t)
    t = _MOD_WORD.sub(" ", t)
    t = re.sub(r"\b(?:w/|with|and|capsule|transformer|trans|tube)\b|[+&,()|\-:/]+", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def assess(title: Optional[str], description: Optional[str], cost, value_of: Callable) -> Optional[dict]:
    """The body + parts math for one ad, or None when it doesn't qualify."""
    try:
        if not title or not cost or float(cost) <= 0 or not _MIC.search(title) or _SHELL.search(title):
            return None
        parts = parts_in(title, description)
        if not parts:
            return None
        cost = float(cost)
        body = value_of(body_title(title))
        if not body or body < MIN_BODY or body > MAX_BODY:
            return None
        extra = sum(v for _l, v in parts)
        if extra < MIN_PARTS or extra > body * MAX_PARTS_X_BODY:
            return None
        worth = body + extra
        pct = round((1 - cost / worth) * 100)
        if pct < MIN_UNDER_PCT or pct > MAX_UNDER_PCT or cost < body * MIN_ASK_X_BODY:
            return None
        return {"body": round(body), "parts": round(extra), "worth": round(worth), "cost": round(cost), "pct": pct,
                "labels": [l for l, _v in parts]}
    except Exception:
        return None


def pick(rows: list, value_of: Callable) -> list:
    """Pure. Fixed-price modded ads that clear the thresholds, biggest dollar gap first."""
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
        out.append(dict(r, modded=a, finder_note=(
            f"Modded: stock body ~${a['body']:,} + {' & '.join(a['labels'])} ~${a['parts']:,} = ~${a['worth']:,} "
            f"vs ${a['cost']:,} ask ({a['pct']}% under). Part values are estimates; confirm the upgrade in the photos.")))
    out.sort(key=lambda r: -(r["modded"]["worth"] - r["modded"]["cost"]))
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR, market_value as mv
    from scrapers.enrich import needs_repair
    now = now or datetime.now(timezone.utc)
    rows = FR.clean(FR.recent_rows(DAYS, LIKE_TERMS, now, limit=3000, priced=True), cfg)
    rows = [r for r in rows if parts_in(r["title"], r.get("description")) and not needs_repair(r["title"], r.get("description"))]
    return pick(rows, lambda t: mv.value(t)[0])
