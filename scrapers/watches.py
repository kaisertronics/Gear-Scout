"""
Saved searches ("watches"): a search phrase plus an optional max price,
re-run on their own schedule. New matches trigger a push notification and/or
a short alert email.

The first run of a watch records everything already out there without
alerting, so saving a search you just looked through doesn't immediately
re-announce all of it.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from scrapers.enrich import needs_repair, parse_price
from scrapers.store import _conn

logger = logging.getLogger(__name__)

STATUS_PATH = Path("/data/watches_status.json")


def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_hits (
            watch_query TEXT NOT NULL,
            global_id TEXT NOT NULL,
            found_at TEXT NOT NULL,
            PRIMARY KEY (watch_query, global_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_state (
            watch_query TEXT PRIMARY KEY,
            seeded INTEGER NOT NULL DEFAULT 0
        )
    """)


def normalize_query(q: str) -> str:
    return " ".join((q or "").lower().split())


def get_watches(cfg: dict) -> list[dict]:
    out = []
    for w in cfg.get("watches") or []:
        q = normalize_query(w.get("query", ""))
        if not q:
            continue
        max_price = w.get("max_price")
        try:
            max_price = float(max_price) if max_price not in (None, "") else None
        except (TypeError, ValueError):
            max_price = None
        out.append({"query": q, "max_price": max_price, "enabled": w.get("enabled", True)})
    return out


def _within_price(price: str, max_price) -> bool:
    if max_price is None:
        return True
    value = parse_price(price)
    return value is None or value <= max_price


def seed_from_stored(query: str, max_price) -> int:
    """Marks every already-stored match as seen for this watch — used right
    after the user saves a search from the Search page, so only listings
    that appear after that point alert."""
    from scrapers.store import search_listings
    rows = [r for r in search_listings(query, limit=2000) if _within_price(r.get("price"), max_price)]
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _ensure_tables(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO watch_hits (watch_query, global_id, found_at) VALUES (?,?,?)",
            [(query, r["global_id"], now) for r in rows],
        )
        conn.execute(
            "INSERT OR REPLACE INTO watch_state (watch_query, seeded) VALUES (?, 1)", (query,)
        )
        conn.commit()
    return len(rows)


def run_watches(cfg: dict) -> dict[str, list[dict]]:
    """Runs every enabled watch. Returns {query: [new hit dicts]} for watches
    with new matches (empty on a watch's silent first run)."""
    from scrapers.live_search import run_live_search

    watches = [w for w in get_watches(cfg) if w["enabled"]]
    status = _load_status()
    new_by_watch: dict[str, list[dict]] = {}

    for w in watches:
        q = w["query"]
        try:
            results = run_live_search(q, cfg)
        except Exception as e:
            logger.exception("Watch %r failed", q)
            status[q] = {**status.get(q, {}), "last_run": _now(), "error": str(e)}
            continue

        hits = [l for r in results for l in r.listings if _within_price(l.price, w["max_price"])]
        now = _now()
        new_hits = []
        with _conn() as conn:
            _ensure_tables(conn)
            seeded = conn.execute(
                "SELECT seeded FROM watch_state WHERE watch_query = ?", (q,)
            ).fetchone()
            for l in hits:
                row = conn.execute(
                    "SELECT hidden, dup_key FROM seen WHERE global_id = ?", (l.global_id,)
                ).fetchone()
                if row and row[0]:
                    continue
                # The same item can come back under several sources (one FB
                # listing in multiple regions, or a FB/Craigslist cross-post)
                # — alert only if this watch hasn't already seen it.
                already = conn.execute(
                    """SELECT 1 FROM watch_hits h JOIN seen s ON s.global_id = h.global_id
                       WHERE h.watch_query = ? AND (s.url = ? OR (s.dup_key IS NOT NULL AND s.dup_key = ?))
                       LIMIT 1""",
                    (q, l.url, row[1] if row else None),
                ).fetchone()
                # Saved-search matches are wanted, so show them on the
                # Dashboard too (live searches alone stay Search-page-only).
                conn.execute("UPDATE seen SET live_only = 0 WHERE global_id = ?", (l.global_id,))
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO watch_hits (watch_query, global_id, found_at) VALUES (?,?,?)",
                    (q, l.global_id, now),
                ).rowcount
                if inserted and not already and seeded and seeded[0]:
                    new_hits.append({
                        "title": l.title, "price": l.price, "url": l.url,
                        "source_name": l.source_name, "image_url": l.image_url,
                        "needs_repair": needs_repair(l.title, l.description),
                    })
            conn.execute(
                "INSERT OR REPLACE INTO watch_state (watch_query, seeded) VALUES (?, 1)", (q,)
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM watch_hits WHERE watch_query = ?", (q,)
            ).fetchone()[0]
            conn.commit()

        status[q] = {"last_run": now, "last_new": len(new_hits), "total": total, "error": None}
        if new_hits:
            new_by_watch[q] = new_hits
        logger.info("Watch %r: %d matches, %d new", q, len(hits), len(new_hits))

    _save_status(status)
    return new_by_watch


def notify_watch_hits(cfg: dict, new_by_watch: dict[str, list[dict]]):
    from scrapers.notify import push_enabled, send_push

    if not new_by_watch:
        return
    email_cfg = cfg.get("email") or {}
    dashboard_url = email_cfg.get("dashboard_url", "http://localhost:8420").rstrip("/")
    ncfg = cfg.get("notifications") or {}

    if push_enabled(cfg):
        for q, hits in new_by_watch.items():
            lines = [f"{h['title'][:80]} — {h['price'] or 'no price'}" + (" (needs repair)" if h["needs_repair"] else "")
                     for h in hits[:6]]
            if len(hits) > 6:
                lines.append(f"+{len(hits) - 6} more")
            click = hits[0]["url"] if len(hits) == 1 else f"{dashboard_url}/search?q={quote_plus(q)}"
            send_push(cfg, f"{len(hits)} new for “{q}”", "\n".join(lines), url=click, tags=["mag"])

    if ncfg.get("watch_email", True) and email_cfg.get("from") and email_cfg.get("password"):
        from scrapers.emailer import build_watch_alert_html, send_html_email
        total = sum(len(h) for h in new_by_watch.values())
        names = ", ".join(f"“{q}”" for q in new_by_watch)
        send_html_email(
            build_watch_alert_html(new_by_watch, dashboard_url),
            f"Gear Scout alert — {total} new for {names}",
            email_cfg,
        )


def watch_status() -> dict:
    return _load_status()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _save_status(status: dict):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2))
