"""Boutique clone makers the owner stars (Vanguard, sE, Peluso, AudioScape,
Advanced Audio, United Tech, Serrano; docs/owner_patterns.md).

Three small features share this list:
- feed(): the newest live ads from these makers, every source in one short list
  (Home card). Reads the listings table, so the dashboard calls it only through
  its cached/locked finder, pre-warmed in the background.
- sibling_terms(): the makers' other models (Vanguard V13 -> V4, V44S ...), added
  to the sweep as derived terms (wanted.derived_terms), searched like any term.
  Sellers underprice models they don't know. Not Wanted-push terms.
- fb_terms(): one broad Marketplace search per maker, for the sweep's Facebook
  maker lane (telex.run_wanted_sweep).
Nothing here sends anything or changes a price rule."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone

FEED_DAYS = 3
FEED_LIMIT = 12

# key -> (display name, title pattern, sibling model searches, Facebook search)
MAKERS = {
    "vanguard": ("Vanguard", r"\bvanguard\b", ["vanguard v1", "vanguard v4", "vanguard v44s", "vanguard v2"],
                 "vanguard microphone"),
    "se": ("sE Electronics", r"\bse\s*electronics\b|\bse\s*(?:vr[12]|voodoo|2200|4400|gemini|rnr1|t2)\b",
           ["se electronics vr1", "se electronics vr2", "se electronics 2200", "se electronics 4400", "se rnr1 ribbon"],
           "se electronics vr2"),
    "peluso": ("Peluso", r"\bpeluso\b", ["peluso 22 251", "peluso 22 67", "peluso 2247 se", "peluso p-87"],
               "peluso microphone"),
    "audioscape": ("AudioScape", r"\baudio\s?scape\b", ["audioscape la-2a", "audioscape as-2a", "audioscape 1176"],
                   "audioscape"),
    "advanced": ("Advanced Audio", r"\badvanced\s+audio\b", ["advanced audio cm12", "advanced audio cm49",
                                                              "advanced audio cm269", "advanced audio cm67", "advanced audio cm87"],
                 "advanced audio microphone"),
    "united": ("United Studio Tech", r"\bunited\s+(?:studio|audio|microphone|mic\b)|\bunited\s*ut\b|\but\s?(?:47|67|87)\b",
               ["united studio technologies ut47", "united studio technologies ut67", "united studio technologies ut87"],
               "united microphone"),
    "serrano": ("Serrano", r"\bserrano\b", ["serrano 47", "serrano 67", "serrano 87"], "serrano microphone"),
    "silentarts": ("Silent-Arts", r"\bsilent[\s-]?arts?\b", ["silent arts la-2a"], "silent arts"),
}
_PATTERNS = {k: re.compile(v[1], re.I) for k, v in MAKERS.items()}


def maker_of(title: str):
    """Key of the clone maker named in a title, or None."""
    for k, p in _PATTERNS.items():
        if p.search(title or ""):
            return k
    return None


def sibling_terms() -> list:
    return list(dict.fromkeys(t for v in MAKERS.values() for t in v[2]))


def fb_terms() -> list:
    """One broad Marketplace search per maker."""
    return list(dict.fromkeys(v[3] for v in MAKERS.values()))


def pick(rows: list, now=None) -> list:
    """Pure. Newest first (by when it was posted, else first seen), one per
    link, only ads from a clone maker. rows need title, url, first_seen."""
    now = now or datetime.now(timezone.utc)
    cut = now - timedelta(days=FEED_DAYS)

    def when(r):
        for k in ("posted_at", "first_seen"):
            try:
                d = datetime.fromisoformat(str(r.get(k)))
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
        return None
    out, seen = [], set()
    for r in rows:
        t = when(r)
        k = maker_of(r.get("title"))
        if not k or t is None or t < cut or t > now + timedelta(hours=2) or r.get("url") in seen:
            continue
        seen.add(r.get("url"))
        out.append((t, dict(r, maker=MAKERS[k][0])))
    out.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in out[:FEED_LIMIT]]


def find(cfg: dict, now=None) -> list:
    """The feed. One indexed-range read of the last FEED_DAYS days."""
    from scrapers.enrich import exclude_match, is_not_audio, title_says_sold
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=FEED_DAYS + 1)).isoformat()
    args = ["%vanguard%", "%peluso%", "%audioscape%", "%audio scape%", "%advanced audio%", "%united%", "%serrano%",
            "%silent%arts%", "%se electronics%", "%vr1%", "%vr2%", "%voodoo%", "%ut47%", "%ut 47%", "%ut67%", "%ut 67%",
            "%ut87%", "%ut 87%"]
    like = " OR ".join(["LOWER(title) LIKE ?"] * len(args))
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE first_seen >= ? AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
            f" AND COALESCE(pending, 0) = 0 AND (country IS NULL OR country IN ('US', 'CA')) AND ({like})"
            " ORDER BY first_seen DESC LIMIT 600", [since] + args)]
    exclude = cfg.get("exclude_words") or []
    rows = [r for r in rows if not exclude_match(r.get("title") or "", exclude) and not is_not_audio(r.get("title") or "")
            and not title_says_sold(r.get("title") or "")]
    return pick(rows, now)
