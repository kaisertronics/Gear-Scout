"""Suggest sharper Telex terms: for each broad one-word term in the Telex list,
brand+model terms taken from the owner's starred listings. Pure logic, no I/O."""
import re
from typing import Callable, Optional

MAX_SUGGESTIONS = 6


def _default_term_for(title: str) -> Optional[str]:
    from scrapers.enrich import model_query, title_query
    q = model_query(title)
    if q:
        return q.strip()
    words = (title_query(title) or "").split()
    return " ".join(words[:4]) or None


def suggest_terms(telex_terms, starred_titles, term_for: Optional[Callable[[str], Optional[str]]] = None,
                  limit: int = MAX_SUGGESTIONS) -> list[dict]:
    """Returns [{broad, term, title}]. `title` is the starred title the term came
    from (the add-from-listing route derives the same term from it)."""
    term_for = term_for or _default_term_for
    have = {str(t).strip().lower() for t in (telex_terms or []) if str(t).strip()}
    broad_terms = [t for t in dict.fromkeys(str(t).strip() for t in (telex_terms or []))
                   if t and len(t.split()) == 1 and "," not in t]
    out, seen = [], set()
    for broad in broad_terms:
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(broad.lower()) + r"(?![a-z0-9])")
        for title in starred_titles or []:
            if not title or not pat.search(str(title).lower()):
                continue
            try:
                term = (term_for(str(title)) or "").strip()
            except Exception:
                continue
            key = term.lower()
            if not term or key in have or key in seen or key == broad.lower():
                continue
            if not pat.search(key):
                continue  # the specific term must still be about the broad word
            seen.add(key)
            out.append({"broad": broad, "term": term, "title": str(title)})
            if len(out) >= limit:
                return out
    return out
