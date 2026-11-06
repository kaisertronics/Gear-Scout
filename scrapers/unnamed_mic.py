"""Guess what an unnamed mic might be.

Pure function, no I/O. identify() only answers for a generic mic title
("vintage condenser microphone", "ribbon mic", "tube mic", "studio mic") that
names no brand or model. It then looks for clues in the title and description
(capsule type, "made in Germany", "U47 style", connector, logos) and lists
likely candidates. Conservative: anything unclear means None, and guesses are
hints, never a claim.
"""
import re
from typing import Optional

MAX_GUESSES = 3

# Generic mic wording the title must be made of.
_GENERIC = re.compile(r"\b(?:condenser|ribbon|tube|valve|studio|vintage|large[\s-]?diaphragm|ldc|dynamic)\b", re.I)
_MIC = re.compile(r"\bmic(?:rophone)?\b", re.I)
# Any brand or model in the title (or a model-ish token) means it is not unnamed.
_BRANDS = re.compile(
    r"\b(aea|akg|avantone|audio[\s-]?technica|at\d{3,4}|lauten|royer|sennheiser|shure|neumann|peluso|warm\s*audio|"
    r"golden\s*age|stam|bees\s?neez|telefunken|rode|rhode|mojave|blue|electro[\s-]?voice|ev|beyerdynamic|coles|cad|mxl|"
    r"soyuz|wunder|chandler|manley|ua|universal\s*audio|josephson|schoeps|sony|oktava|behringer|marshall|samson|"
    r"fame|apex|cascade|sterling|se\s*electronics|aston|slate|lewitt|sontronics|gefell|rca|altec|western\s*electric|"
    r"u\s?-?47|u\s?-?67|u\s?-?87|c\s?-?12|251|ela\s?m|sm\s?-?57|sm\s?-?7|km\s?-?84|c\s?-?414|44[\s-]?bx|77[\s-]?dx|"
    r"[a-z]{1,4}-?\d{2,4}[a-z]?)\b", re.I)
_NOT_MIC = re.compile(r"\b(?:pre|preamp|cable|stand|boom|shock\s?mount|case|pop\s?filter|capsule only|parts?|for parts|"
                      r"lot|bundle|kit|mixer|interface|speaker|headphones?)\b", re.I)

# (clue regex, candidate models, clue text). Candidates are ordered, best first.
_CLUES = [
    (re.compile(r"\b(?:u[\s-]?47|u[\s-]?67|u[\s-]?87|neumann)[\s-]*(?:style|type|like|inspired|clone)\b", re.I),
     ["Neumann U47 / U67 style clone", "Telefunken ELA M 251 style clone"], "'U47 style' wording"),
    (re.compile(r"\b(?:c[\s-]?12|251)[\s-]*(?:style|type|like|inspired|clone)\b", re.I),
     ["AKG C12 / Telefunken 251 style clone"], "'C12 / 251 style' wording"),
    (re.compile(r"\b(?:ck[\s-]?12|9[\s-]?pin|nine[\s-]?pin)\b", re.I),
     ["AKG C12 / Telefunken ELA M 251 (CK12 capsule, 9-pin)"], "CK12 capsule or 9-pin connector"),
    (re.compile(r"\bmade in (?:west\s+)?germany\b|\bgerman(?:y)?\b", re.I),
     ["Neumann (U47 / U67 / U87 / KM series)", "AKG C12 / C414", "Sennheiser MD / MKH", "Schoeps"], "made in Germany"),
    (re.compile(r"\bmade in (?:austria|vienna)\b|\bvienna\b|\baustria\b", re.I),
     ["AKG C12 / C24 / C414", "AKG D12 / D19"], "made in Austria"),
    (re.compile(r"\bmade in (?:russia|ussr)\b|\bussr\b|\bsoviet\b|\boktava\b", re.I),
     ["Oktava ML-19 / MK-319", "Soviet-era tube mic"], "Russian / Soviet origin"),
    (re.compile(r"\bmade in (?:czech|czechoslovakia)\b|\bczech\b", re.I),
     ["Microtech Gefell M 7xx", "Tesla / Meopta ribbon or condenser"], "Czech origin"),
    (re.compile(r"\bmade in (?:japan)\b|\bjapan(?:ese)?\b", re.I),
     ["Sony C-37 / C-800 era", "Audio-Technica", "Sanken / Beyer clone"], "made in Japan"),
    (re.compile(r"\bmade in (?:china)\b|\bchina|chinese\b", re.I),
     ["Budget Chinese large-diaphragm (Neewer / MXL / generic)"], "made in China"),
    (re.compile(r"\bribbon\b", re.I),
     ["RCA 44 / 77 style ribbon", "Royer R-121", "AEA R84 / R44", "Beyer M160 / Coles 4038"], "ribbon element"),
    (re.compile(r"\b(?:tube|valve)\b", re.I),
     ["Tube LDC (Telefunken / Neumann / AKG era)", "Chinese tube LDC clone"], "tube mic"),
]


def identify(title: Optional[str], description: Optional[str] = None) -> Optional[dict]:
    """{'guesses': [model, ...], 'reason': str} for a generic-titled mic, else None."""
    if not title:
        return None
    t = title.strip()
    if not (_MIC.search(t) and _GENERIC.search(t)) or _BRANDS.search(t) or _NOT_MIC.search(t):
        return None
    text = f"{t} {description or ''}"
    guesses, why = [], []
    for rx, models, label in _CLUES:
        if rx.search(text):
            why.append(label)
            for m in models:
                if m not in guesses:
                    guesses.append(m)
    if not guesses:
        return None
    return {"guesses": guesses[:MAX_GUESSES], "reason": ", ".join(why[:3])}
