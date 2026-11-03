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


def family(term: str) -> Optional[dict]:
    """{"query", "pattern", "model"} for a term naming a Hosa DB25 snake, else None."""
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
    from scrapers.base import keyword_match
    return keyword_match(re.sub(r"<[^>]+>", " ", description), [term])
