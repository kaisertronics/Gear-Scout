"""
Which model a listing is, within one of your terms — and the cheapest
working one of each model (the gold "Lowest" tags, the Price Board).

One definition used by the Feed, the Telex page and the Price Board, so a
listing gets the same model and the same "cheapest" everywhere.

Model numbers are compared with spacing, hyphens and line prefixes taken out:
"Avalon VT-737SP", "VT 737 sp", "737sp" and "Avalon 737" are all the 737;
"EL8-X", "EL8 X" and "el8x" are all the Distressor EL8X; "D12", "D12E" and
"D12VR" stay different models.
"""
import re
import statistics
from typing import Optional

# Same unit, different spellings: {model as written (normalized): model}.
_SAME = {
    "737sp": "737", "737s": "737",   # Avalon VT-737 / VT-737SP
}
_FILLER = {
    "vintage", "used", "new", "rare", "mint", "excellent", "great", "good", "condition", "pair", "pairs",
    "of", "the", "and", "with", "w", "for", "sale", "tested", "working", "original", "audio", "pro",
    "professional", "studio", "microphone", "microphones", "mic", "mics", "series", "black", "silver", "blue",
    "green", "red", "grey", "gray", "engineering", "co", "labs", "electronics", "inc", "company", "two", "three",
    "four", "matched", "stereo", "mono", "single", "compressor", "compressors", "limiter",
    "channel", "strip", "tube", "condenser", "dynamic", "ribbon", "module", "version",
    "unit", "rack", "rackmount", "in", "box", "case", "only", "nice", "clean", "local", "pickup", "obo", "free",
    "shipping", "selling", "available", "excellent", "a", "an", "is", "this", "my",
}
_YEARS = re.compile(r"^(?:19|20)\d\ds?$|^\d0s$")
_SHORT = 3  # a piece this short is joined to the model number next to it ("VT 737", "737 SP", "EL8 - X")


def _tokens(text: str, keep: set = frozenset()) -> list[str]:
    """Lower-cased words with model numbers glued back together: "VT-737 SP"
    → ["vt737sp"], "PCM 70" → ["pcm70"], "D12 VR" → ["d12vr"], "EL8-X" →
    ["el8x"]. Brand names in `keep` ("akg", "ssl") are never glued on."""
    from scrapers.base import fix_brand_spelling
    text = re.sub(r"(\d0)['’]s\b", r"\1s", (text or "").lower())  # "70's" is a decade, not a model
    words = re.findall(r"[a-z0-9]+", fix_brand_spelling(text))
    out: list[str] = []
    for w in words:
        if out and out[-1] not in keep and w not in keep:
            prev = out[-1]
            # A short line prefix before a bare number: "VT 737", "PCM 70", "D 12".
            prefix = prev.isalpha() and len(prev) <= _SHORT and w[0].isdigit() and not _YEARS.match(w)
            # A short letter suffix after a model number: "737 SP", "EL8 X", "D12 VR", "U87 AI".
            suffix = (w.isalpha() and len(w) <= 2 and w not in ("eq", "mk", "of", "to", "in", "w")
                      and re.search(r"\d", prev) and not _YEARS.match(prev))
            if prefix or suffix:
                out[-1] = prev + w
                continue
        out.append(w)
    return out


def _model_tokens(tokens: list[str]) -> list[str]:
    return [t for t in tokens if re.search(r"\d", t) and not _YEARS.match(t)
            and (re.search(r"[a-z]", t) or len(t) >= 3) and not re.fullmatch(r"\d+(?:x\d+|hz|khz|v|w|ft|in|mm|pcs?)", t)
            and not re.fullmatch(r"x\d|\d+x", t)]


def model_of(title: str, term: str) -> tuple[str, str]:
    """(key, display name) of the model a listing is, within `term`."""
    from scrapers.enrich import canonical_brand
    brand = (canonical_brand(title) or canonical_brand(term) or "").replace("-", " ")
    brand_words = set(brand.split())
    from scrapers.enrich import _ALIAS_TO_BRAND
    aliases = [a for a, c in _ALIAS_TO_BRAND.items() if c.replace("-", " ") == brand]
    keep = brand_words | {a for a in aliases if " " not in a}
    brand_words |= {w for a in aliases for w in a.split()}
    term_tokens = _tokens(term, keep)
    term_models = _model_tokens(term_tokens)
    title_tokens = _tokens(title, keep)
    found = _model_tokens(title_tokens)
    # A number right after the brand is the model even when it's short ("SSL 2", "AKG 414").
    found += [a + b for a, b in zip(title_tokens, title_tokens[1:]) if a in keep and b[0].isdigit()
              and not _YEARS.match(b) and a + b not in found]
    model = None
    if term_models:
        for tm in term_models:
            digits = re.search(r"\d+", tm).group(0)
            prefix = tm[:tm.index(digits)]
            hit = next((t for t in found if digits in t), None)
            if hit:
                # Drop a line prefix the term doesn't use ("VT-737" for "737").
                start = hit.index(digits)
                pre = hit[:start]
                model = (pre if pre.endswith(prefix) and prefix else "") + hit[start:]
                if prefix and not pre.endswith(prefix):
                    model = pre + hit[start:]
                break
    elif found:
        model = found[0]
    shown_brand = " ".join(w.upper() if len(w) <= 3 else w.title() for w in brand.split())
    if model:
        model = _SAME.get(model, model)
        alias = next((a for a in sorted(keep, key=len, reverse=True) if model.startswith(a) and len(model) > len(a)), None)
        if alias:  # "ssl2" → "SSL 2"
            return model, f"{alias.upper() if len(alias) <= 3 else alias.title()} {model[len(alias):].upper()}"
        return model, f"{shown_brand} {model.upper()}".strip()
    # No model number. A term that names one product ("Distressor") — not a
    # brand — means that product: every such listing is the same model.
    # For a brand ("daking"), the words after it name the model ("Pre IV").
    term_brand = canonical_brand(term)
    if not term_brand or term.strip().lower().replace(" ", "-") != term_brand:
        return "w:", term.title()
    term_words = set(re.findall(r"[a-z0-9]+", term.lower()))
    words = [w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
             if w not in _FILLER and w not in brand_words and w not in term_words and not _YEARS.match(w)
             and not w.isdigit()]
    name = " ".join(words[:2])
    label = (term.title() if not name else f"{shown_brand} {name.title()}").strip()
    return f"w:{name}", label


def landed(r: dict) -> float:
    """What it costs delivered, per piece: price + known US shipping (split
    across the pieces of a pair / lot). Local pickup and unknown shipping
    count as no shipping."""
    unit = r.get("unit") or 0
    ship = r.get("shipping")
    n = r.get("qty") or 1
    return unit + (ship / max(n, 1) if ship else 0)


def picks(term: str, rows: list[dict], exclude_words: Optional[list] = None) -> list[dict]:
    """The cheapest working one of each model among `rows` (each with
    'title', 'price', 'description', 'url', 'unit' = price per piece and,
    when known, 'shipping' and 'qty'), cheapest delivered first (price +
    known shipping): [{'url', 'model', 'unit', 'total', 'count'}]. The first is the
    term's overall cheapest. Leaves out repair jobs, add-ons, copies (unless
    the term asks for them), auctions, and prices far under that model's
    usual (a part, or a typo)."""
    from scrapers.board import _is_addon
    from scrapers.enrich import exclude_match, is_not_audio, needs_repair, title_says_sold
    from scrapers.steals import _CLONE
    wants_copy = bool(_CLONE.search(term))
    cands = []
    for r in rows:
        t = r.get("title") or ""
        unit = r.get("unit")
        if not unit or unit < 20 or (r.get("description") or "").startswith("Auction"):
            continue
        if (needs_repair(t, r.get("description")) or _is_addon(t) or is_not_audio(t) or title_says_sold(t)
                or (exclude_words and exclude_match(t, exclude_words)) or (_CLONE.search(t) and not wants_copy)):
            continue
        cands.append(r)
    by_model: dict[str, list] = {}
    names: dict[str, str] = {}
    for r in cands:
        key, name = model_of(r["title"], term)
        by_model.setdefault(key, []).append(r)
        names.setdefault(key, name)
    # A one-off spelling with stray letters ("737vt sp", "VT-737 SP Hi-End",
    # "D12 régi") belongs to the main model it starts with.
    for key in [k for k, v in by_model.items() if len(v) == 1 and not k.startswith("w:")]:
        base = max((k for k in by_model if k != key and not k.startswith("w:") and key.startswith(k)
                    and len(by_model[k]) >= 2), key=len, default=None)
        if base:
            by_model[base] += by_model.pop(key)
    out = []
    for key, offers in by_model.items():
        # Junk priced far under what THIS model usually asks (a part with a
        # vague title, a typo) — judged per model, never across the term (a
        # PCM60 is rightly much cheaper than a PCM96).
        if len(offers) >= 3:
            usual = statistics.median(r["unit"] for r in offers)
            offers = [r for r in offers if r["unit"] >= usual * 0.3] or offers
        best = min(offers, key=landed)  # price + shipping decides
        out.append({"url": best["url"], "model": names[key], "unit": best["unit"], "total": landed(best),
                    "count": len(offers)})
    out.sort(key=lambda m: m["total"])
    return out
