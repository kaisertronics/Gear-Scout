"""Cable part-number terms ("Hosa DTP-802") searched by what the cable IS.

Sellers rarely type the model number: "HOSA 6 ft DB25 to 8 TRS cables (2)" is a
DTP-802-style snake whose title, description and eBay specs never say "DTP-802",
so a search for the number can't find it. A Hosa DT_-8xx term also looks for
the brand plus the connectors ("hosa db25 trs") and keeps a listing whose title
names that brand + cable type (and no *other* model number)."""
import re
from typing import Optional

# Hosa DB25 snakes: DTP = TRS, DTM = XLR male, DTF = XLR female.
_HOSA = re.compile(r"\b(?:hosa\s+)?dt([pmf])[-\s]?(\d{3})\b", re.I)
_CONNECTOR = {"p": r"(?:trs|1/4|¼|quarter|ts)", "m": r"xlr", "f": r"xlr"}
_QUERY = {"p": "hosa db25 trs", "m": "hosa db25 xlr", "f": "hosa db25 xlr"}
_OTHER_MODEL = re.compile(r"\bdt[pmf][-\s]?\d{3}\b", re.I)


# Hosa 8-channel snakes: STX = XLR <-> TRS (F/M suffix), CSS = TRS <-> TRS, CPP = TS <-> TS,
# XLR = XLR <-> XLR. The last digit is the length in metres (802 = 2 m, 805 = 5 m).
_SNAKE = re.compile(r"\b(?:hosa\s+)?(stx|css|cpp|xlr)[-\s]?(8\d{2})([fm])?\b", re.I)
_SNAKE_QUERY = {"stx": "hosa 8 channel xlr trs snake", "css": "hosa 8 channel trs snake",
                "cpp": "hosa 8 channel ts snake", "xlr": "hosa 8 channel xlr snake"}
_SNAKE_CONNECTOR = {"stx": r"(?:xlr)", "css": r"(?:trs|1/4|¼)", "cpp": r"(?:ts|1/4|¼)", "xlr": r"(?:xlr)"}
_OTHER_SNAKE = re.compile(r"\b(?:stx|css|cpp|xlr)[-\s]?8\d{2}[fm]?\b", re.I)
SNAKE_NUMBERS = {"stx": ("803f", "805f", "807f", "805m", "807m"), "css": ("802", "803", "805", "807"),
                 "cpp": ("802", "803", "805"), "dtp": ("802", "803", "805"), "xlr": ("803", "805", "807")}


def _snake_family(term: str) -> Optional[dict]:
    m = _SNAKE.search(term or "")
    if not m:
        return None
    kind, num = m.group(1).lower(), m.group(2)
    model = kind + num
    pattern = re.compile(r"(?=.*\bhosa\b)(?=.*(?:\b8\b|\b8[- ]?(?:ch|channel)|eight))(?=.*\b" + _SNAKE_CONNECTOR[kind] +
                         r"\b)(?=.*(?:snake|multicore|cable))", re.I | re.S)
    own = re.compile(r"(?<![a-z0-9])" + kind + r"[-\s]?" + num + r"[fm]?(?![a-z0-9])", re.I)  # "STX-805F" also finds "STX-805"
    return {"query": _SNAKE_QUERY[kind], "pattern": pattern, "model": model, "own": own}


def family(term: str) -> Optional[dict]:
    """{"query", "pattern", "model"} for a term naming a Hosa snake, else None."""
    snake = _snake_family(term)
    if snake:
        return snake
    m = _HOSA.search(term or "")
    if not m:
        return None
    kind, num = m.group(1).lower(), m.group(2)
    pattern = re.compile(r"(?=.*\bhosa\b)(?=.*\bdb-?25\b)(?=.*\b" + _CONNECTOR[kind] + r"\b)", re.I | re.S)
    return {"query": _QUERY[kind], "pattern": pattern, "model": f"dt{kind}{num}"}


def title_matches(term: str, title: Optional[str]) -> bool:
    """Title names the same kind of cable (brand + connectors) as a part-number
    term, with no different model number of its own."""
    fam = family(term)
    if not fam or not title:
        return False
    other = {re.sub(r"\W", "", x).lower() for x in _OTHER_MODEL.findall(title)}
    if fam.get("own"):  # a snake: the sibling-model check ignores the F/M suffix
        other = {re.sub(r"[fm]$", "", x) for x in other | {re.sub(r"\W", "", x).lower() for x in _OTHER_SNAKE.findall(title)}}
        if fam["own"].search(title):
            return not (other - {fam["model"]})
    if other - {fam["model"]}:
        return False
    return bool(fam["pattern"].search(title))


def alt_query(term: str) -> Optional[str]:
    fam = family(term)
    return fam["query"] if fam else None


def in_description(term: str, description: Optional[str]) -> bool:
    """A model-number term (letters + digits) written in the listing's
    description rather than its title."""
    if not description or not re.search(r"\d", term or ""):
        return False
    fam = _snake_family(term)
    if fam and fam["own"].search(re.sub(r"<[^>]+>", " ", description)):
        return True
    from scrapers.base import keyword_match
    return keyword_match(re.sub(r"<[^>]+>", " ", description), [term])


def siblings(terms: list) -> list:
    """Common sibling model terms for each Hosa snake family named in the Telex
    List ("Hosa STX-807F" -> Hosa STX-803F / 805F / 805M / 807M), so a search for
    one length catches the others. Terms already listed are left out."""
    have = {re.sub(r"[^a-z0-9]", "", t.lower()) for t in terms}
    out = []
    for t in terms:
        m = _SNAKE.search(t) or _HOSA.search(t)
        if not m:
            continue
        kind = m.group(1).lower() if m.re is _SNAKE else "dt" + m.group(1).lower()
        key = "dtp" if kind == "dtp" else kind
        for n in SNAKE_NUMBERS.get(key, ()):
            term = f"Hosa {kind.upper()}-{n.upper()}"
            if not {kind + n, "hosa" + kind + n} & have and term not in out:
                out.append(term)
    return out
