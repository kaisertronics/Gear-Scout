"""
What Gear Scout learns on its own, from your actions and its own runs:

  - Your taste: words and models in listings you favorite (and hide) are
    weighed against everything it sees, so new listings can be scored "for
    you" without any setup.
  - Search terms: models you favorite, items you track for lowest price,
    and live searches you repeat are added to the scheduled scrapes
    automatically ("learned terms"; removable, and a removed term stays
    removed).
  - Sources: every run records how long each source took, how many matches
    it gave and whether it failed. Runs start the slowest sources first
    (shorter total time with parallel workers); the hourly refresh skips
    sources that keep failing or never match anything, and the Learning
    page shows the scoreboard.
  - New sources: Facebook groups that keep showing up in for-sale post
    searches are suggested as sources (one click to add).
  - Noise: words that show up again and again in listings you hide, and
    never in ones you like, are suggested as exclude words.

Everything is stored in the listings database; nothing leaves the machine.
"""
import logging
import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from scrapers.store import _conn

logger = logging.getLogger(__name__)

MIN_FAVORITES_FOR_TASTE = 3
REPEAT_SEARCHES_TO_LEARN = 2
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*[a-z0-9]|[a-z0-9]")
_STOP = {
    "the", "and", "with", "for", "a", "an", "of", "in", "on", "to", "w", "or", "is", "new", "used",
    "sale", "vintage", "great", "excellent", "condition", "works", "working", "mint", "good", "very",
    "shipping", "free", "local", "pickup", "obo", "firm", "price", "only", "like", "pro", "audio",
    "ea", "each", "pair", "set", "lot", "black", "silver", "white", "original", "rare", "box",
    "my", "up", "has", "have", "had", "selling", "sell", "sold", "just", "this", "that", "it", "its",
    "not", "no", "yes", "all", "any", "one", "two", "are", "was", "will", "can", "from", "you",
    "your", "our", "get", "got", "per", "off", "out", "now", "too", "also", "some", "more", "co",
    "inc", "llc", "ltd", "program", "item", "items", "listing", "offer", "offers", "cash", "trade",
}


def _ensure(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS source_runs (
            source_name TEXT, ran_at TEXT, kind TEXT, success INTEGER,
            found INTEGER, seconds REAL
        );
        CREATE INDEX IF NOT EXISTS source_runs_by_name ON source_runs (source_name, ran_at);
        CREATE TABLE IF NOT EXISTS search_history (query TEXT, searched_at TEXT);
        CREATE TABLE IF NOT EXISTS learned_terms (
            term TEXT PRIMARY KEY, reason TEXT, added_at TEXT, blocked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS group_sightings (
            url TEXT PRIMARY KEY, name TEXT, sale_posts INTEGER DEFAULT 0,
            last_seen TEXT, dismissed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS dismissed_suggestions (kind TEXT, value TEXT, PRIMARY KEY (kind, value));
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Source performance
# ---------------------------------------------------------------------------

def record_source_runs(results, kind: str) -> None:
    try:
        with _conn() as conn:
            _ensure(conn)
            conn.executemany(
                "INSERT INTO source_runs VALUES (?,?,?,?,?,?)",
                [(r.source_name, _now(), kind, 1 if (r.success or getattr(r, "blocked", False)) else 0,
                  len(r.listings), round(r.duration_seconds or 0, 1)) for r in results],
            )
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            conn.execute("DELETE FROM source_runs WHERE ran_at < ?", (cutoff,))
            conn.commit()
    except Exception:
        logger.exception("Couldn't record source run stats")


def source_scoreboard(days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        _ensure(conn)
        rows = conn.execute(
            "SELECT source_name, kind, success, found, seconds FROM source_runs"
            " WHERE ran_at >= ? ORDER BY ran_at", (cutoff,)).fetchall()
    board: dict[str, dict] = {}
    for name, kind, ok, found, secs in rows:
        b = board.setdefault(name, {"name": name, "runs": 0, "ok": 0, "found": 0, "seconds": 0.0,
                                    "sched_runs": 0, "sched_found": 0, "fail_streak": 0})
        b["runs"] += 1
        b["ok"] += ok
        b["seconds"] += secs or 0
        b["fail_streak"] = 0 if ok else b["fail_streak"] + 1
        if kind != "live":
            b["sched_runs"] += 1
            b["sched_found"] += found or 0
        b["found"] += found or 0
    out = []
    for b in board.values():
        b["avg_seconds"] = b["seconds"] / b["runs"] if b["runs"] else 0
        b["success_rate"] = round(100 * b["ok"] / b["runs"]) if b["runs"] else 0
        b["avg_found"] = round(b["sched_found"] / b["sched_runs"], 1) if b["sched_runs"] else None
        out.append(b)
    return sorted(out, key=lambda b: (-(b["avg_found"] or 0), b["avg_seconds"]))


def average_seconds() -> dict[str, float]:
    return {b["name"]: b["avg_seconds"] for b in source_scoreboard(days=14)}


def skip_on_light_runs(sources: list[dict]) -> tuple[list[dict], list[str]]:
    """Sources the hourly refresh leaves out: failed its last 5 runs in a
    row, or ran 12+ scheduled times without a single match. They still run
    in the full scheduled scrapes, so they're re-tested several times a
    day and come back on their own once they work or start matching."""
    board = {b["name"]: b for b in source_scoreboard(days=7)}
    # Dealer stores read in full (whole inventory every time) change slowly:
    # on hourly refreshes, only every 3 hours.
    slow_types = {"shopify", "long_mcquade"}
    recent_ok: dict[str, str] = {}
    with _conn() as conn:
        _ensure(conn)
        for name, ran_at in conn.execute(
                "SELECT source_name, MAX(ran_at) FROM source_runs WHERE success = 1 GROUP BY source_name"):
            recent_ok[name] = ran_at
    three_h_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    keep, skipped = [], []
    for s in sources:
        b = board.get(s["name"])
        if b and (b["fail_streak"] >= 5 or (b["sched_runs"] >= 12 and b["sched_found"] == 0)):
            skipped.append(s["name"])
        elif s.get("type") in slow_types and (recent_ok.get(s["name"]) or "") > three_h_ago:
            skipped.append(s["name"])
        else:
            keep.append(s)
    return keep, skipped


# ---------------------------------------------------------------------------
# Learned search terms
# ---------------------------------------------------------------------------

def record_search(query: str) -> None:
    q = (query or "").strip().lower()
    if not q:
        return
    with _conn() as conn:
        _ensure(conn)
        conn.execute("INSERT INTO search_history VALUES (?, ?)", (q, _now()))
        conn.commit()


def learned_terms(include_blocked: bool = False) -> list[dict]:
    with _conn() as conn:
        _ensure(conn)
        rows = conn.execute(
            "SELECT term, reason, added_at, blocked FROM learned_terms ORDER BY added_at DESC").fetchall()
    return [{"term": t, "reason": r, "added_at": a, "blocked": bool(b)}
            for t, r, a, b in rows if include_blocked or not b]


def active_learned_terms() -> list[str]:
    try:
        return [t["term"] for t in learned_terms()]
    except Exception:
        logger.exception("Couldn't read learned terms")
        return []


def block_term(term: str) -> None:
    with _conn() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO learned_terms (term, reason, added_at, blocked) VALUES (?, 'removed by you', ?, 1)"
            " ON CONFLICT(term) DO UPDATE SET blocked = 1", (term, _now()))
        conn.commit()


def update_learned_terms(cfg: dict) -> list[str]:
    """Adds terms from favorites, lowest-price trackers and repeated live
    searches that the keyword list doesn't already cover. Returns new ones."""
    from scrapers.base import keyword_match
    from scrapers.enrich import is_partial, model_query

    keywords = cfg.get("keywords") or []
    candidates: dict[str, str] = {}
    with _conn() as conn:
        _ensure(conn)
        for (title,) in conn.execute("SELECT title FROM seen WHERE favorite = 1"):
            q = model_query(title)
            if q and not is_partial(title):
                candidates.setdefault(q.lower(), "you favorited one")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        for q, n in conn.execute(
                "SELECT query, COUNT(*) FROM search_history WHERE searched_at >= ? GROUP BY query", (cutoff,)):
            if n >= REPEAT_SEARCHES_TO_LEARN:
                candidates.setdefault(q, f"you searched it {n} times")
        existing = {t for (t,) in conn.execute("SELECT term FROM learned_terms")}
    for t in cfg.get("price_trackers") or []:
        q = (t.get("query") if isinstance(t, dict) else str(t) or "").strip().lower()
        if q:
            candidates.setdefault(q, "you track its lowest price")

    added = []
    for term, reason in candidates.items():
        if term in existing or len(term) < 3:
            continue
        # Already caught by the keyword list? Then there's nothing to learn.
        if keyword_match(term, keywords):
            continue
        added.append((term, reason))
    if added:
        with _conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO learned_terms (term, reason, added_at, blocked) VALUES (?,?,?,0)",
                [(t, r, _now()) for t, r in added])
            conn.commit()
        logger.info("Learned %d new search terms: %s", len(added), ", ".join(t for t, _ in added))
    return [t for t, _ in added]


def keywords_with_learned(cfg: dict) -> list[str]:
    """Keyword list + Telex List terms + learned terms (no repeats)."""
    base = list(cfg.get("keywords") or [])
    lower = {str(k).lower() for k in base}
    for t in [str(t).strip() for t in cfg.get("telex_list") or []] + active_learned_terms():
        if t and t.lower() not in lower:
            base.append(t)
            lower.add(t.lower())
    return base


# ---------------------------------------------------------------------------
# Taste
# ---------------------------------------------------------------------------

def _tokens(title: str) -> set[str]:
    from scrapers.enrich import model_key
    words = {w for w in _WORD.findall((title or "").lower())
             if w not in _STOP and not w.isdigit() and len(w) >= 3}
    key = model_key(title)
    if key:
        words.add(f"model:{key}")
    return words


class Taste:
    """Log-odds weights: how much more often a word appears in listings you
    favorite than in everything Gear Scout sees (hidden listings pull the
    other way)."""

    def __init__(self, weights: dict[str, float], favorites: int):
        self.weights = weights
        self.favorites = favorites

    @property
    def ready(self) -> bool:
        return self.favorites >= MIN_FAVORITES_FOR_TASTE

    def score(self, title: str) -> float:
        return sum(self.weights.get(t, 0.0) for t in _tokens(title))

    def top(self, n: int = 20, positive: bool = True) -> list[tuple[str, float]]:
        items = [(t, w) for t, w in self.weights.items() if (w > 0) == positive]
        items.sort(key=lambda x: -abs(x[1]))
        return [(t.replace("model:", "").replace(":", " "), w) for t, w in items[:n]]


_taste_cache: dict = {"at": None, "taste": None}


def taste(max_age_seconds: int = 300) -> Taste:
    now = datetime.now(timezone.utc)
    if _taste_cache["taste"] and (now - _taste_cache["at"]).total_seconds() < max_age_seconds:
        return _taste_cache["taste"]
    fav, hid, allc = Counter(), Counter(), Counter()
    n_fav = n_hid = n_all = 0
    cutoff = (now - timedelta(days=60)).isoformat()
    with _conn() as conn:
        for title, f, h in conn.execute(
                "SELECT title, favorite, hidden FROM seen WHERE favorite = 1 OR hidden = 1 OR first_seen >= ?",
                (cutoff,)):
            toks = _tokens(title)
            allc.update(toks)
            n_all += 1
            if f:
                fav.update(toks)
                n_fav += 1
            elif h:
                hid.update(toks)
                n_hid += 1
    weights = {}
    for t, a in allc.items():
        f, h = fav.get(t, 0), hid.get(t, 0)
        if f < 2 and h < 2:
            continue
        base = (a + 1) / (n_all + 2)
        w = 0.0
        if f >= 2:
            w += math.log(((f + 0.5) / (n_fav + 1)) / base)
        if h >= 2:
            w -= math.log(((h + 0.5) / (n_hid + 1)) / base)
        if abs(w) > 0.3:
            weights[t] = round(w, 3)
    t = Taste(weights, n_fav)
    _taste_cache.update(at=now, taste=t)
    return t


# ---------------------------------------------------------------------------
# Suggestions: Facebook groups, exclude words
# ---------------------------------------------------------------------------

def record_group_sighting(url: Optional[str], name: Optional[str] = None) -> None:
    if not url:
        return
    url = url.split("?")[0].rstrip("/")
    if not url.startswith("http"):
        url = "https://www.facebook.com" + url
    with _conn() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO group_sightings (url, name, sale_posts, last_seen) VALUES (?,?,1,?)"
            " ON CONFLICT(url) DO UPDATE SET sale_posts = sale_posts + 1, last_seen = excluded.last_seen,"
            " name = COALESCE(excluded.name, group_sightings.name)", (url, name, _now()))
        conn.commit()


def group_suggestions(cfg: dict, min_posts: int = 2) -> list[dict]:
    have = {(s.get("url") or "").split("?")[0].rstrip("/") for s in cfg.get("sources") or []}
    with _conn() as conn:
        _ensure(conn)
        rows = conn.execute(
            "SELECT url, name, sale_posts, last_seen FROM group_sightings"
            " WHERE dismissed = 0 AND sale_posts >= ? ORDER BY sale_posts DESC LIMIT 20", (min_posts,)).fetchall()
    return [{"url": u, "name": n, "sale_posts": c, "last_seen": l} for u, n, c, l in rows if u not in have]


def dismiss(kind: str, value: str) -> None:
    with _conn() as conn:
        _ensure(conn)
        if kind == "group":
            conn.execute("UPDATE group_sightings SET dismissed = 1 WHERE url = ?", (value,))
        conn.execute("INSERT OR IGNORE INTO dismissed_suggestions VALUES (?, ?)", (kind, value))
        conn.commit()


def exclude_suggestions(cfg: dict, min_hidden: int = 3) -> list[dict]:
    """Words in 3+ listings you hid, never in a favorite, not part of any
    search term, and not already excluded."""
    hid, fav = Counter(), Counter()
    with _conn() as conn:
        _ensure(conn)
        dismissed = {v for (v,) in conn.execute("SELECT value FROM dismissed_suggestions WHERE kind = 'exclude'")}
        for title, f in conn.execute("SELECT title, favorite FROM seen WHERE hidden = 1 OR favorite = 1"):
            toks = {t for t in _tokens(title) if not t.startswith("model:") and len(t) > 2}
            (fav if f else hid).update(toks)
    excluded = {str(w).lower() for w in cfg.get("exclude_words") or []}
    keyword_words = {w for k in cfg.get("keywords") or [] for w in _WORD.findall(str(k).lower())}
    out = []
    for word, n in hid.most_common(60):
        if n < min_hidden or fav.get(word) or word in excluded or word in dismissed:
            continue
        if word in keyword_words:
            continue
        out.append({"word": word, "hidden": n})
    return out[:15]


# ---------------------------------------------------------------------------
# How fast gear sells, and for how much (learned from listings that sold)
# ---------------------------------------------------------------------------

FAST_SALE_DAYS = 4
_sales_cache: dict = {"at": None, "stats": None}


def sale_stats(max_age_seconds: int = 600) -> dict[str, dict]:
    """{model key: {'n', 'days' (median days listed before it sold),
    'price' (median price it was listed at when it went)}} for models with
    3+ listings Gear Scout watched sell. Days are counted from when the
    listing was posted (or first seen) to when it disappeared."""
    import statistics
    from scrapers.enrich import model_key, parse_price
    now = datetime.now(timezone.utc)
    if _sales_cache["stats"] is not None and (now - _sales_cache["at"]).total_seconds() < max_age_seconds:
        return _sales_cache["stats"]
    by_model: dict[str, list[tuple[float, float]]] = {}
    with _conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen)")}
        rows = [] if "sold_at" not in cols else conn.execute(
            "SELECT title, price, first_seen, posted_at, sold_at FROM seen WHERE sold = 1 AND sold_at IS NOT NULL"
            " AND COALESCE(duplicate, 0) = 0").fetchall()
    for title, price, first_seen, posted_at, sold_at in rows:
        key = model_key(title)
        value = parse_price(price)
        if not key or not value:
            continue
        try:
            start = datetime.fromisoformat(posted_at or first_seen)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            days = (datetime.fromisoformat(sold_at) - start).total_seconds() / 86400
        except (TypeError, ValueError):
            continue
        if 0 <= days <= 120:
            by_model.setdefault(key, []).append((days, value))
    stats = {}
    for key, sales in by_model.items():
        if len(sales) >= 3:
            stats[key] = {"n": len(sales), "days": statistics.median(d for d, _ in sales),
                          "price": statistics.median(v for _, v in sales)}
    _sales_cache.update(at=now, stats=stats)
    return stats


def fast_sellers(limit: int = 20) -> list[tuple[str, dict]]:
    items = [(k, v) for k, v in sale_stats().items() if v["days"] <= FAST_SALE_DAYS]
    return sorted(items, key=lambda kv: kv[1]["days"])[:limit]
