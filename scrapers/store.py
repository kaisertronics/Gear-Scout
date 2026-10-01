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


_schema_ready = False


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Several background jobs (hourly refresh, Facebook post search, price
    # lookups, the dashboard) use the database at once: wait for a busy
    # database instead of failing, and use WAL so reads never block writes.
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    # Table setup and upgrades only need to happen once per process — this
    # function is called for nearly every database read.
    global _schema_ready
    if _schema_ready:
        return conn
    conn.execute("PRAGMA journal_mode = WAL")
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
    # hidden: dismissed by the user. duplicate: the same item already stored
    # from another source (same link, or same title + price cross-posted to a
    # different site). previous_price/price_dropped_at: set when a listing is
    # seen again cheaper; drop_notified tracks whether that was reported.
    for col, ddl in (
        ("hidden", "INTEGER NOT NULL DEFAULT 0"),
        ("duplicate", "INTEGER NOT NULL DEFAULT 0"),
        ("dup_key", "TEXT"),
        ("previous_price", "TEXT"),
        ("price_dropped_at", "TEXT"),
        ("drop_notified", "INTEGER NOT NULL DEFAULT 1"),
        ("location", "TEXT"),
        ("sold", "INTEGER NOT NULL DEFAULT 0"),
        ("pending", "INTEGER NOT NULL DEFAULT 0"),
        ("sold_at", "TEXT"),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE seen ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS seen_url ON seen(url)")
    conn.execute("CREATE INDEX IF NOT EXISTS seen_dup_key ON seen(dup_key)")
    # The lookups every page and job makes: newest first, per site, favorites.
    conn.execute("CREATE INDEX IF NOT EXISTS seen_first_seen ON seen(first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS seen_source ON seen(source_name, first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS seen_favorite ON seen(favorite) WHERE favorite = 1")
    conn.execute("PRAGMA optimize")
    conn.commit()
    if "dup_key" not in existing_cols:
        _backfill_duplicates(conn)
    _schema_ready = True
    return conn


def _find_original(conn, global_id: str, url: str, key: str, family: str) -> bool:
    """True if another stored row is the same item: identical link, or the
    same title+price posted on a different site (a cross-post)."""
    from scrapers.enrich import source_family
    if url and conn.execute(
        "SELECT 1 FROM seen WHERE url = ? AND global_id != ? AND duplicate = 0 LIMIT 1",
        (url, global_id),
    ).fetchone():
        return True
    # Title+price matching is only for private-seller cross-posts between
    # Facebook and Craigslist — dealer listings on Reverb/eBay often use the
    # bare product name, so the same title and price there can be two units.
    if key and family in _CROSSPOST_FAMILIES:
        for (other_url,) in conn.execute(
            "SELECT url FROM seen WHERE dup_key = ? AND global_id != ? AND duplicate = 0",
            (key, global_id),
        ):
            other = source_family(other_url)
            if other != family and other in _CROSSPOST_FAMILIES:
                return True
    return False


_CROSSPOST_FAMILIES = {"facebook", "craigslist"}


def _backfill_duplicates(conn):
    from scrapers.enrich import dup_key, source_family
    rows = conn.execute(
        "SELECT global_id, url, title, price FROM seen ORDER BY first_seen"
    ).fetchall()
    for gid, url, title, price in rows:
        key = dup_key(title, price)
        conn.execute("UPDATE seen SET dup_key = ? WHERE global_id = ?", (key, gid))
    conn.execute("UPDATE seen SET duplicate = 0")
    for gid, url, title, price in rows:
        if _find_original(conn, gid, url, dup_key(title, price), source_family(url)):
            conn.execute("UPDATE seen SET duplicate = 1 WHERE global_id = ?", (gid,))
    conn.commit()


def is_seen(global_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen WHERE global_id = ?", (global_id,)
        ).fetchone()
        return row is not None


def _record_status(conn, listing) -> None:
    """Pending/sold labels a source shows on the listing itself (Facebook
    Marketplace cards). None means the source doesn't say."""
    pending = getattr(listing, "pending", None)
    if pending is not None:
        conn.execute("UPDATE seen SET pending = ? WHERE url = ?", (1 if pending else 0, listing.url))
    if getattr(listing, "sold", None):
        conn.execute("UPDATE seen SET sold = 1, sold_at = COALESCE(sold_at, ?) WHERE url = ?",
                     (datetime.now(timezone.utc).isoformat(), listing.url))


def set_pending(url: str, pending: bool) -> None:
    with _conn() as conn:
        conn.execute("UPDATE seen SET pending = ? WHERE url = ?", (1 if pending else 0, url))
        conn.commit()


def mark_seen(listing, live_only: bool = False) -> str:
    """Stores a listing. Returns "new", "duplicate" (same item already
    stored from elsewhere), "price_drop" (seen before, now cheaper) or
    "seen"."""
    from scrapers.enrich import dup_key, parse_price, source_family

    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT price FROM seen WHERE global_id = ?", (listing.global_id,)
        ).fetchone()
        if existing:
            status = "seen"
            if getattr(listing, "location", None):
                conn.execute(
                    "UPDATE seen SET location = ? WHERE global_id = ? AND location IS NULL",
                    (listing.location, listing.global_id),
                )
            old_value, new_value = parse_price(existing[0]), parse_price(listing.price)
            if old_value and new_value and new_value < old_value * 0.99:
                conn.execute(
                    """UPDATE seen SET price = ?, previous_price = ?, price_dropped_at = ?,
                       drop_notified = 0 WHERE global_id = ?""",
                    (listing.price, existing[0], now, listing.global_id),
                )
                status = "price_drop"
            _record_status(conn, listing)
            if not live_only:
                # A standing-keyword scrape found something a live search
                # already stored — it's a real match, so show it on the
                # Dashboard now.
                conn.execute(
                    "UPDATE seen SET live_only = 0 WHERE global_id = ? AND live_only = 1",
                    (listing.global_id,),
                )
            conn.commit()
            return status

        key = dup_key(listing.title, listing.price)
        duplicate = _find_original(
            conn, listing.global_id, listing.url, key, source_family(listing.url)
        )
        conn.execute(
            """INSERT INTO seen
               (global_id, source_name, title, url, price, image_url,
                description, posted_at, first_seen, live_only, dup_key, duplicate,
                location)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                listing.global_id,
                listing.source_name,
                listing.title,
                listing.url,
                listing.price,
                listing.image_url,
                listing.description,
                listing.posted_at.isoformat() if listing.posted_at else None,
                now,
                1 if live_only else 0,
                key,
                1 if duplicate else 0,
                getattr(listing, "location", None),
            ),
        )
        _record_status(conn, listing)
        conn.commit()
        return "duplicate" if duplicate else "new"


def filter_new(listings) -> list:
    """Stores every listing and returns only the genuinely new ones — not
    previously seen, and not a duplicate of an item already stored from
    another source."""
    return [l for l in listings if mark_seen(l) == "new"]


def update_price(url: str, price: str) -> str:
    """Records a re-checked price for every stored row with this link.
    Returns "drop", "raise" or "same"."""
    from scrapers.enrich import parse_price

    new_value = parse_price(price)
    result = "same"
    with _conn() as conn:
        for gid, old in conn.execute("SELECT global_id, price FROM seen WHERE url = ?", (url,)).fetchall():
            old_value = parse_price(old)
            if not new_value or not old_value or abs(new_value - old_value) < 0.5:
                continue
            if new_value < old_value:
                conn.execute(
                    """UPDATE seen SET price = ?, previous_price = ?, price_dropped_at = ?,
                       drop_notified = 0 WHERE global_id = ?""",
                    (price, old, datetime.now(timezone.utc).isoformat(), gid),
                )
                result = "drop"
            else:
                conn.execute(
                    "UPDATE seen SET price = ?, previous_price = NULL WHERE global_id = ?",
                    (price, gid),
                )
                result = "raise" if result == "same" else result
        conn.commit()
    return result


def mark_sold(url: str):
    """Marks a listing sold/removed and remembers when it was noticed — how
    long gear takes to sell is something Gear Scout learns from."""
    with _conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(seen)")}
        if "sold_at" not in cols:
            conn.execute("ALTER TABLE seen ADD COLUMN sold_at TEXT")
        conn.execute("UPDATE seen SET sold = 1, sold_at = COALESCE(sold_at, ?) WHERE url = ?",
                     (datetime.now(timezone.utc).isoformat(), url))
        conn.commit()


def pending_favorite_price_drops() -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM seen WHERE favorite = 1 AND drop_notified = 0 AND sold = 0
               ORDER BY price_dropped_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


def mark_price_drops_notified(global_ids: list[str]):
    with _conn() as conn:
        conn.executemany(
            "UPDATE seen SET drop_notified = 1 WHERE global_id = ?",
            [(g,) for g in global_ids],
        )
        conn.commit()


def set_hidden(global_id: str, hidden: bool):
    with _conn() as conn:
        conn.execute(
            "UPDATE seen SET hidden = ? WHERE global_id = ?",
            (1 if hidden else 0, global_id),
        )
        conn.commit()


def count_hidden() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM seen WHERE hidden = 1").fetchone()[0]


def unhide_all() -> int:
    with _conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM seen WHERE hidden = 1").fetchone()[0]
        conn.execute("UPDATE seen SET hidden = 0 WHERE hidden = 1")
        conn.commit()
    return n


def all_priced_rows() -> list[dict]:
    """Title/price/description of every stored listing — input for the
    typical-price index."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT title, price, description FROM seen WHERE price IS NOT NULL AND duplicate = 0"
        )]


def purge_old(days: int = 30):
    """Remove entries older than `days` to keep the DB small."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM seen WHERE first_seen < ? AND favorite = 0", (cutoff,))
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
    query = ("SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND live_only = 0"
             " AND hidden = 0 AND duplicate = 0 AND sold = 0")
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

    # Coarse SQL prefilter, one clause per word (all must hit, any order) —
    # same rule as keyword_match for a single search phrase. Model-number
    # words ("c38", "km184") also compare with spaces/hyphens stripped from
    # the stored text, so "C-38B" and "KM 184" pass through to the real check.
    strip = lambda col: f"lower(replace(replace(coalesce({col},''),' ',''),'-',''))"
    clauses, params = [], []
    words = [w for w in query_text.split() if re.search(r'[a-z0-9]', w, re.I)] or [query_text]
    for word in words:
        like = f"%{word}%"
        clause = "title LIKE ? OR description LIKE ? OR tags LIKE ?"
        params += [like, like, like]
        compact = re.sub(r'[^a-z0-9]', '', word.lower())
        if re.search(r'[a-z]', compact) and re.search(r'\d', compact):
            clause += f" OR {strip('title')} LIKE ? OR {strip('description')} LIKE ?"
            params += [f"%{compact}%", f"%{compact}%"]
        clauses.append(f"({clause})")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT * FROM seen
               WHERE url IS NOT NULL AND url != '' AND hidden = 0 AND duplicate = 0 AND sold = 0
                 AND {' AND '.join(clauses)}
               ORDER BY first_seen DESC""",
            params,
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
               WHERE url IS NOT NULL AND url != '' AND favorite = 1 AND hidden = 0
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
