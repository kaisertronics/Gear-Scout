"""
Gentle nudge for starred ads that are still up.

Ads the owner starred often sell before a decision is made. Once a starred ad
has been starred for 24+ hours and is still live (not sold, pending, hidden or
gone quiet), send ONE push: "Still thinking about <title>? It's still up at $X".
At most 2 nudges per (Pacific) day, only in the daytime, and each ad is nudged
once (nudge_alerted table).

The seen table keeps no "starred at" time, so a small side table
(star_noticed) records when this job first saw each ad starred. No existing
table is changed.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MIN_HOURS = 24
MAX_PER_DAY = 2
DAY_HOURS = (9, 20)  # Pacific: nudge from 9:00 up to 19:59
STALE_DAYS = 3  # not seen by any scrape for this long = probably gone
TZ = "America/Los_Angeles"


def _local_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ))


def is_daytime(now: datetime | None = None) -> bool:
    now = now or _local_now()
    return DAY_HOURS[0] <= now.hour < DAY_HOURS[1]


def _setup(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS nudge_alerted (url TEXT PRIMARY KEY, at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS star_noticed (url TEXT PRIMARY KEY, at TEXT)")


def track_stars() -> None:
    """Remember when each starred ad was first noticed as starred."""
    from scrapers.store import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _setup(conn)
        conn.execute("INSERT OR IGNORE INTO star_noticed SELECT url, ? FROM seen"
                     " WHERE favorite = 1 AND url IS NOT NULL AND url != ''", (now,))
        conn.commit()


def star_ages() -> dict[str, str]:
    """url -> ISO time the ad was first noticed starred (for the dashboard badge)."""
    from scrapers.store import _conn
    with _conn() as conn:
        _setup(conn)
        return {u: a for u, a in conn.execute("SELECT url, at FROM star_noticed")}


def due_nudges(now_utc: datetime | None = None) -> list[dict]:
    from scrapers.enrich import title_says_sold
    from scrapers.store import _conn
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = (now_utc - timedelta(hours=MIN_HOURS)).isoformat()
    stale = (now_utc - timedelta(days=STALE_DAYS)).isoformat()
    with _conn() as conn:
        _setup(conn)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT s.*, n.at AS starred_at FROM seen s JOIN star_noticed n ON n.url = s.url"
            " WHERE s.favorite = 1 AND s.hidden = 0 AND COALESCE(s.sold, 0) = 0"
            " AND COALESCE(s.pending, 0) = 0 AND n.at <= ?"
            " AND s.url NOT IN (SELECT url FROM nudge_alerted)"
            " AND COALESCE(s.last_seen, s.first_seen) >= ?"
            " ORDER BY n.at", (cutoff, stale))]
    return [r for r in rows if not title_says_sold(r.get("title") or "")]


def _sent_today(conn, now_local: datetime) -> int:
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    return conn.execute("SELECT COUNT(*) FROM nudge_alerted WHERE at >= ?", (start,)).fetchone()[0]


def alert(cfg: dict) -> int:
    from scrapers.store import _conn
    track_stars()
    now_local = _local_now()
    if not is_daytime(now_local):
        return 0
    with _conn() as conn:
        left = MAX_PER_DAY - _sent_today(conn, now_local)
    if left <= 0:
        return 0
    rows = due_nudges()[:left]
    if not rows:
        return 0
    from scrapers.notify import push_enabled, send_push
    sent = []
    for r in rows:
        title = (r.get("title") or "this ad")[:70]
        price = r.get("price") or "the same price"
        if push_enabled(cfg):
            send_push(cfg, "Still thinking about it?", f"Still thinking about {title}? It's still up at {price}.",
                      url=r["url"], tags=["star"])
        sent.append(r)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _setup(conn)
        conn.executemany("INSERT OR IGNORE INTO nudge_alerted VALUES (?, ?)", [(r["url"], now) for r in sent])
        conn.commit()
    logger.info("Starred-ad nudges: %d", len(sent))
    return len(sent)
