"""
Wanted-term hit-rate report (read-only).

For each Wanted term: how many stored listings it matched in the last DAYS
days versus how many of those ended up starred or alerted. A term that
matches plenty but never leads to a star or alert is noise. This only
reports; it never changes the Wanted list, config or any threshold.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
DAYS = 30
NOISY_MIN_MATCHES = 20   # fewer matches than this is too little to judge
NOISY_MAX_RATE = 0.02    # 2% or less of matches starred / alerted = noisy

# Tables (url column first) that record a listing was alerted on.
ALERT_TABLES = ("wanted_fast", "steal_alerted", "mic_alerted", "target_alerted",
                "shoplist_alerted", "seller_watch_alerted", "drop_alerted", "nudge_alerted")


def hit_rates(rows, match_fn, good_urls, min_matches=NOISY_MIN_MATCHES, max_rate=NOISY_MAX_RATE) -> list[dict]:
    """Pure. rows: listing dicts (url, title, ...). match_fn(row) -> list of
    terms the row matches. good_urls: urls that were starred or alerted.
    Returns [{term, matched, good, rate, noisy}], noisiest first."""
    stats: dict[str, list[int]] = {}
    for r in rows:
        try:
            terms = match_fn(r) or []
        except Exception:
            continue
        good = (r.get("url") in good_urls) or bool(r.get("favorite"))
        for t in set(terms):
            s = stats.setdefault(t, [0, 0])
            s[0] += 1
            s[1] += 1 if good else 0
    out = []
    for t, (m, g) in stats.items():
        rate = g / m if m else 0.0
        out.append({"term": t, "matched": m, "good": g, "rate": round(rate, 3),
                    "noisy": m >= min_matches and rate <= max_rate})
    out.sort(key=lambda e: (not e["noisy"], e["rate"], -e["matched"], e["term"]))
    return out


def noisy_terms(report: list[dict]) -> list[dict]:
    """Just the noisy entries of a hit_rates() result."""
    return [e for e in report if e.get("noisy")]


def build_report(cfg: dict) -> list[dict]:
    """Reads the database (read-only) and returns hit_rates() for the last DAYS days. Never raises."""
    try:
        from scrapers import wanted
        from scrapers.store import DB_PATH
        ms = wanted.matchers(cfg)
        since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                "SELECT url, title, price, source_name, favorite FROM seen "
                "WHERE first_seen > ? AND url IS NOT NULL AND COALESCE(duplicate, 0) = 0 "
                "ORDER BY first_seen DESC LIMIT ?", (since, MAX_ROWS))]
            good = set()
            for tbl in ALERT_TABLES:
                try:
                    good.update(u for (u,) in conn.execute(f"SELECT url FROM {tbl}"))
                except sqlite3.Error:
                    continue
        finally:
            conn.close()
        return hit_rates(rows, lambda r: wanted.match(r.get("title"), r.get("price"), ms, r.get("source_name")), good)
    except Exception:
        logger.debug("term_hit_rate: report failed", exc_info=True)
        return []


_cache: dict = {"at": 0.0, "lines": [], "running": False}
CACHE_SECONDS = 6 * 3600
MAX_ROWS = 3000  # newest listings only: matching every stored title against every term is slow


def cached_noisy_lines(cfg: dict) -> list[str]:
    """What the Health page shows. Never blocks: the (slow) report is built in a
    background thread at most every CACHE_SECONDS; until it's ready, nothing shows."""
    import threading
    import time
    if not _cache["running"] and time.time() - _cache["at"] > CACHE_SECONDS:
        _cache["running"] = True

        def work():
            try:
                _cache["lines"] = noisy_lines(build_report(cfg))
            except Exception:
                pass
            finally:
                _cache["at"] = time.time()
                _cache["running"] = False
        threading.Thread(target=work, daemon=True, name="term-hit-rate").start()
    return list(_cache["lines"])


def noisy_lines(report: list[dict]) -> list[str]:
    """['term (3 of 140)', ...] for the Health page."""
    return [f"{e['term']} ({e['good']} of {e['matched']})" for e in noisy_terms(report)]
