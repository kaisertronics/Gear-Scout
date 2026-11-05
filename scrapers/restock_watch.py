"""
Shop restock watcher.

A shop-stock listing (Shopify-type stores, Long & McQuade, used-shop and
liquidation sources) for a mic model you care about (a Mic Focus model, a Telex
wanted-list term, or something you starred) that was sold out / gone and is
now BACK shows up as a restock: one push per restock.

Database-only (no new network requests). Each pass compares the listings to the
state it remembered last time (restock_state table):
  gone = marked sold, or not seen for GONE_HOURS while its shop was still being scraped
  live = not sold and seen again after it was recorded gone
First sighting never alerts. One restock = one alert (alerted_gone_at).
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
GONE_HOURS = 30       # unseen this long while the shop itself was scraped = gone
SHOP_FRESH_HOURS = 6  # the shop counts as "being scraped" if any of its rows was seen this recently
RECENT_HOURS = 48     # "Back in stock" note window
MAX_PUSH = 6
SHOP_TYPES = ("shopify", "long_mcquade", "hibid", "liquidation")


def shop_names(cfg: dict) -> set[str]:
    srcs = list(cfg.get("sources") or [])
    try:
        from scrapers import liquidation, pawn_shops
        srcs = pawn_shops.with_defaults(liquidation.with_defaults(srcs))
    except Exception:
        logger.debug("restock: defaults unavailable", exc_info=True)
    return {s["name"] for s in srcs if s.get("name") and (s.get("type") in SHOP_TYPES)}


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS restock_state (url TEXT PRIMARY KEY, status TEXT, "
                 "gone_at TEXT, restocked_at TEXT, alerted_gone_at TEXT)")


def _cares(cfg: dict, row: dict, ms) -> bool:
    from scrapers import mic_focus, wanted
    title = row.get("title") or ""
    if row.get("favorite") and mic_focus.classify(title):
        return True
    if mic_focus.classify(title):
        return True
    return bool(wanted.match(title, row.get("price"), ms, row.get("source_name")))


def check(cfg: dict, now: datetime | None = None) -> list[dict]:
    """Updates the state and returns the listings that just restocked and
    haven't been announced (marks nothing announced; see alert())."""
    from scrapers.store import _conn
    from scrapers import wanted
    now = now or datetime.now(timezone.utc)
    names = shop_names(cfg)
    if not names:
        return []
    ms = wanted.matchers(cfg)
    iso = now.isoformat()
    stale = (now - timedelta(hours=GONE_HOURS)).isoformat()
    fresh = (now - timedelta(hours=SHOP_FRESH_HOURS)).isoformat()
    out = []
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        _ensure(conn)
        marks = ",".join("?" * len(names))
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM seen WHERE source_name IN ({marks}) AND url IS NOT NULL"
            " AND COALESCE(hidden, 0) = 0 AND COALESCE(duplicate, 0) = 0", tuple(names))]
        live_shops = {r["source_name"] for r in rows if (r.get("last_seen") or "") >= fresh}
        state = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM restock_state")}
        for r in rows:
            if not _cares(cfg, r, ms):
                continue
            last = r.get("last_seen") or r.get("first_seen") or ""
            sold = bool(r.get("sold"))
            gone = sold or (last < stale and r["source_name"] in live_shops)
            st = state.get(r["url"])
            if st is None:
                conn.execute("INSERT INTO restock_state (url, status, gone_at) VALUES (?, ?, ?)",
                             (r["url"], "gone" if gone else "live", iso if gone else None))
            elif st["status"] == "live" and gone:
                conn.execute("UPDATE restock_state SET status='gone', gone_at=? WHERE url=?", (iso, r["url"]))
            elif st["status"] == "gone" and not gone and last >= (st["gone_at"] or ""):
                conn.execute("UPDATE restock_state SET status='live', restocked_at=? WHERE url=?", (iso, r["url"]))
                if st["alerted_gone_at"] != st["gone_at"]:
                    out.append(r)
        conn.commit()
    return out


def recent(hours: int = RECENT_HOURS) -> dict:
    """{url: restocked_at} for restocks in the last `hours` (for a card note)."""
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with _conn() as conn:
            _ensure(conn)
            return dict(conn.execute("SELECT url, restocked_at FROM restock_state WHERE status='live' "
                                     "AND restocked_at IS NOT NULL AND restocked_at >= ?", (since,)).fetchall())
    except Exception:
        return {}


def alert(cfg: dict) -> int:
    from scrapers.notify import push_enabled, send_push
    from scrapers.store import _conn
    rows = check(cfg)
    if not rows:
        return 0
    rows = rows[:MAX_PUSH]
    src = lambda r: (r.get("source_name") or "").split(" — ")[0]
    line = lambda r: f"{(r.get('title') or '')[:70]} ({r.get('price') or '?'}, {src(r)})"
    title = "Back in stock: a mic you want" if len(rows) == 1 else f"Back in stock: {len(rows)} mics you want"
    sent = True
    if push_enabled(cfg):
        email_cfg = cfg.get("email") or {}
        sent = send_push(cfg, title, "\n".join(line(r) for r in rows),
                         url=rows[0]["url"] if len(rows) == 1 else email_cfg.get("dashboard_url"),
                         tags=["package"])
    if sent is False and push_enabled(cfg):
        return 0  # push failed: try again next pass
    with _conn() as conn:
        for r in rows:
            conn.execute("UPDATE restock_state SET alerted_gone_at = gone_at WHERE url = ?", (r["url"],))
        conn.commit()
    logger.info("Restock alerts: %d", len(rows))
    return len(rows)
