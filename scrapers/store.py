"""
SQLite-backed store for seen listings.
Prevents re-sending listings across multiple daily runs, and keeps enough
detail (price, url, image, etc.) for the dashboard to display them.
"""
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("/data/seen_listings.db")


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            global_id TEXT PRIMARY KEY,
            source_name TEXT,
            title TEXT,
            first_seen TEXT
        )
    """)
    # Migrate older databases (from before listing details were stored) —
    # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table, so
    # new columns have to be added explicitly.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)")}
    for col in ("url", "price", "image_url", "description", "posted_at"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE seen ADD COLUMN {col} TEXT")
    if "favorite" not in existing_cols:
        conn.execute("ALTER TABLE seen ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
    if "tags" not in existing_cols:
        conn.execute("ALTER TABLE seen ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
    # 1 = only ever found by a one-off live search (any phrase the user typed,
    # e.g. "octavia" pulls in furniture and books from FB Marketplace), not by
    # a scrape against the standing keyword list. Kept for the Search page but
    # hidden from the Dashboard's recent listings.
    if "live_only" not in existing_cols:
        conn.execute("ALTER TABLE seen ADD COLUMN live_only INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def is_seen(global_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen WHERE global_id = ?", (global_id,)
        ).fetchone()
        return row is not None


def mark_seen(listing, live_only: bool = False):
    with _conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO seen
               (global_id, source_name, title, url, price, image_url,
                description, posted_at, first_seen, live_only)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                listing.global_id,
                listing.source_name,
                listing.title,
                listing.url,
                listing.price,
                listing.image_url,
                listing.description,
                listing.posted_at.isoformat() if listing.posted_at else None,
                datetime.now(timezone.utc).isoformat(),
                1 if live_only else 0,
            ),
        )
        if not live_only:
            # A standing-keyword scrape found something a live search already
            # stored — it's a real match, so show it on the Dashboard now.
            conn.execute(
                "UPDATE seen SET live_only = 0 WHERE global_id = ? AND live_only = 1",
                (listing.global_id,),
            )
        conn.commit()


def filter_new(listings) -> list:
    """Return only listings not previously seen, and mark them."""
    new = []
    for listing in listings:
        if not is_seen(listing.global_id):
            new.append(listing)
        mark_seen(listing)
    return new


def purge_old(days: int = 30):
    """Remove entries older than `days` to keep the DB small."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        conn.commit()
    logger.info("Purged seen entries older than %d days", days)


def stats() -> dict:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM seen WHERE first_seen >= ?",
            (datetime.now(timezone.utc).date().isoformat(),)
        ).fetchone()[0]
    return {"total_seen": total, "seen_today": today}


def recent_listings(limit: int = 100, source_name: str = None) -> list[dict]:
    """Most recently seen listings, newest first, for the dashboard.

    Excludes rows with no URL — those all predate the columns added for
    listing details (url/price/image/etc.), from back when this table only
    tracked dedup fingerprints, and have nothing real to link to or show."""
    query = "SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND live_only = 0"
    params: tuple = ()
    if source_name:
        query += " AND source_name = ?"
        params = (source_name,)
    query += " ORDER BY first_seen DESC LIMIT ?"
    params = params + (limit,)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def search_listings(query_text: str, limit: int = 300) -> list[dict]:
    """Keyword search across title/description/tags, all sources at once,
    newest first.

    The SQL LIKE below is only a coarse prefilter (fast, uses the substring
    as-is) — the real match is keyword_match()'s whole-word check, applied
    in Python after. Without it, a query like "neve" would also match
    "never used" or "Never Installed", since "neve" is a plain substring of
    "never"; keyword_match requires it as a whole word (optionally plural),
    same as the scheduled run's keyword filter uses everywhere else.

    Same URL-not-null filter as recent_listings, for the same reason
    (legacy pre-migration rows with nothing to show)."""
    from scrapers.base import keyword_match

    like = f"%{query_text}%"
    # Model-number-style queries ("km184", "c38") should also find "KM 184" /
    # "KM-184" / "C38A" — same rule as keyword_match's model pattern — so widen the
    # prefilter to compare with spaces/hyphens stripped from the stored text.
    compact = re.sub(r'[^a-z0-9]', '', query_text.lower())
    strip = lambda col: f"lower(replace(replace(coalesce({col},''),' ',''),'-',''))"
    extra_sql, extra_params = "", ()
    if re.search(r'[a-z]', compact) and re.search(r'\d', compact):
        extra_sql = f" OR {strip('title')} LIKE ? OR {strip('description')} LIKE ?"
        extra_params = (f"%{compact}%", f"%{compact}%")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT * FROM seen
               WHERE url IS NOT NULL AND url != ''
                 AND (title LIKE ? OR description LIKE ? OR tags LIKE ?{extra_sql})
               ORDER BY first_seen DESC""",
            (like, like, like, *extra_params),
        ).fetchall()

    keywords = [query_text]
    matched = [
        dict(r) for r in rows
        if keyword_match(r["title"] or "", keywords)
        or keyword_match(r["description"] or "", keywords)
        or keyword_match(r["tags"] or "", keywords)
    ]
    return matched[:limit]


def favorite_listings(limit: int = 300) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM seen
               WHERE url IS NOT NULL AND url != '' AND favorite = 1
               ORDER BY first_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_favorite(global_id: str, favorite: bool):
    with _conn() as conn:
        conn.execute(
            "UPDATE seen SET favorite = ? WHERE global_id = ?",
            (1 if favorite else 0, global_id),
        )
        conn.commit()


def set_tags(global_id: str, tags: list[str]):
    """Stores tags as a comma-separated string; empty/blank entries dropped."""
    cleaned = ",".join(t.strip() for t in tags if t.strip())
    with _conn() as conn:
        conn.execute(
            "UPDATE seen SET tags = ? WHERE global_id = ?",
            (cleaned, global_id),
        )
        conn.commit()


def count_mismatched(keywords: list[str]) -> int:
    """How many stored listings don't match any of the given keywords —
    e.g. leftovers from a live search for a term that isn't in the
    standing keyword list. Used by the Settings page to show a count
    before you commit to deleting them."""
    from scrapers.base import keyword_match

    with _conn() as conn:
        rows = conn.execute("SELECT title FROM seen").fetchall()
    return sum(1 for (title,) in rows if not keyword_match(title or "", keywords))


def purge_mismatched(keywords: list[str]) -> int:
    """Deletes every stored listing that doesn't match any of the given
    keywords. Returns how many rows were deleted."""
    from scrapers.base import keyword_match

    with _conn() as conn:
        rows = conn.execute("SELECT global_id, title FROM seen").fetchall()
        to_delete = [gid for gid, title in rows if not keyword_match(title or "", keywords)]
        conn.executemany("DELETE FROM seen WHERE global_id = ?", [(gid,) for gid in to_delete])
        conn.commit()
    return len(to_delete)


def purge_all() -> int:
    """Deletes every stored listing, favorite, and tag. Does not touch
    config.yaml, the Facebook session, or anything outside this database.
    Returns how many rows were deleted."""
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        conn.execute("DELETE FROM seen")
        conn.commit()
    return count
