"""Is the ad really the gear it names? Text checks (pure, no AI).

Gear Scout's filters already drop cases, cables, parts and shock mounts. Gear
Flip also has to drop things that can never be flipped as the unit itself:
  * packaging: "AKG C414 XLS Box", "Neumann U87 original box", "retail box and foam"
  * look-alikes: "C414 style", "like a U87", "clone", "vs C414", "DIY kit"
  * prices too far under value to be the real, complete unit
`problem()` returns a short reason, or None when nothing looks wrong."""
import re
from typing import Optional

_PACK = re.compile(r"\b(?:boxes|box|packaging|carton|cardboard|foam inserts?|dust ?covers?|owner'?s manual|manuals?|"
                   r"user guide|brochure|catalog(?:ue)?|poster|sticker|decal|t-?shirt|hat|mug|keychain|pin)\b", re.I)
# The word "box" is the product here (DI box, lunchbox...), or the box is extra (w/ box, boxed)
_BOX_PRODUCT = re.compile(r"\b(?:di|d\.i\.|direct|re-?amp|lunch|stomp|talk|fuzz|juke|beat|mixer|summing|reamp|power|"
                          r"splitter|isolation|patch|junction|breakout|tone|sound|fx|effects?|drum|cajon|music|"
                          r"speaker|squeeze|chatter)\s?box", re.I)
_EXTRA = re.compile(r"(?:\bwith\b|\bw/|\bw\b|\bincl\w*|\bin\b|\bcomes\b|\bplus\b|\band\b|\+|&|,|\bcomplete\b)[^,;|]{0,22}$", re.I)
_ONLY = re.compile(r"\b(?:box|packaging|carton|manual)\s+only\b|\bonly\s+(?:the\s+)?(?:box|packaging|carton|manual)\b|"
                   r"\bempty\s+(?:box|carton|packaging)\b|\b(?:box|carton)\s+for\b", re.I)
_LOOKALIKE = re.compile(r"\b(?:style|styled|type|clone|clones|copy|replica|knock-?off|inspired|tribute|homage|"
                        r"alternative|alternatives|similar|sounds? like|like(?!\s*-?\s*(?:new|mint))|vs\.?|versus|compared?|"
                        r"comparable|not an?|not the|diy|kit|build|project mic|fake|chinese|imitation|equivalent)\b", re.I)


def packaging_only(title: Optional[str]) -> bool:
    """True when the ad is the box, manual or other packaging, not the unit."""
    t = re.sub(r"\s+", " ", title or "").strip()
    if not t:
        return False
    if _ONLY.search(t):
        return True
    for m in _PACK.finditer(t):
        word = m.group(0).lower()
        if word.startswith("box") and (_BOX_PRODUCT.search(t[max(0, m.start() - 14):m.end()]) or re.match(r"boxed", t[m.start():], re.I)):
            continue
        before, after = t[:m.start()], t[m.end():]
        if word.startswith("box") and re.search(r"\bopen\s?$", before, re.I):
            continue  # "open box": a condition (opened, unused), not packaging
        if re.search(r"\bincl\w*|\bincluded\b", after, re.I):
            continue  # "...original box and manual included"
        # "U87 w/ original box", "TLM 103 in box", "1176, box, manual": the unit plus extras.
        if _EXTRA.search(before):
            continue
        # Box first, gear second ("Box: AKG C414") or gear then box ("AKG C414 XLS Box"): packaging.
        return True
    return False


def lookalike(title: Optional[str]) -> bool:
    """True when the title itself says it's a copy of / compared with the model."""
    return bool(_LOOKALIKE.search(title or ""))


def price_problem(ask: float, value: float, repair: Optional[str]) -> Optional[str]:
    """Far under value is rarely the real, complete unit (a box, a part, a scam)."""
    if not value:
        return None
    floor = 0.08 if repair else 0.25
    if ask < value * floor:
        return f"priced {round((1 - ask / value) * 100)}% under value"
    return None


def problem(title: Optional[str], ask: float = 0, value: float = 0, repair: Optional[str] = None) -> Optional[str]:
    if packaging_only(title):
        return "packaging, not the unit"
    if lookalike(title):
        return "a look-alike or comparison, not the model"
    return price_problem(ask, value, repair) if ask and value else None
