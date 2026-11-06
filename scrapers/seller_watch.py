"""Seller watchlist: the owner taps "Watch seller" on a listing card to follow a
seller (site + seller name). When the scraper first sees a NEW listing from a
watched seller, one push goes out. It only reads listings already in the
database (no extra requests to any site) and each listing is announced once
(seller_watch_alerted table, same idea as sold_alerts). Watched sellers live
in /data/watched_sellers.json (never touches config); WATCHED_SELLERS_PATH
overrides the file for tests."""
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PATH = Path(os.environ.get("WATCHED_SELLERS_PATH") or "/data/watched_sellers.json")
WINDOW_HOURS = 48     # only listings first seen this recently are announced
ALERT_MAX_SINGLE = 6  # more than this in one go -> one summary push

_cache: dict = {"mtime": None, "path": None, "items": []}


def _src(source) -> str:
    """Site without the region/search suffix, so a seller is followed across it."""
    return str(source or "").split(" — ")[0].strip()[:60]


def load() -> list[dict]:
    try:
        items = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [w for w in items if isinstance(w, dict) and w.get("source") and w.get("seller")] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PATH)


def _same(w: dict, source, seller) -> bool:
    return w["source"] == _src(source) and w["seller"].lower() == str(seller or "").strip().lower()


def add(source, seller) -> Optional[dict]:
    """Starts watching. None if the seller is blank or a generic name."""
    from scrapers.base import usable_seller
    seller = re.sub(r"\s+", " ", str(seller or "")).strip()[:60]
    if not _src(source) or not usable_seller(seller):
        return None
    items = load()
    for w in items:
        if _same(w, source, seller):
            return w
    w = {"source": _src(source), "seller": seller, "added": datetime.now(timezone.utc).isoformat()}
    items.append(w)
    _save(items)
    return w


def remove(source, seller) -> bool:
    items = load()
    kept = [w for w in items if not _same(w, source, seller)]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def is_watched(source, seller) -> bool:
    """For the card button. Cached on the file's mtime; never raises."""
    try:
        m = PATH.stat().st_mtime
        if _cache["mtime"] != m or _cache["path"] != str(PATH):
            _cache.update(mtime=m, path=str(PATH), items=load())
        return any(_same(w, source, seller) for w in _cache["items"])
    except Exception:
        return False


def latest(w: dict, limit: int = 5) -> list[dict]:
    """The watched seller's newest listings (from the database only)."""
    from scrapers.store import _conn
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT title, url, price, source_name, first_seen, sold FROM seen WHERE seller = ? COLLATE NOCASE"
            " AND source_name LIKE ? ESCAPE '\\' AND url IS NOT NULL AND COALESCE(hidden, 0) = 0"
            " AND COALESCE(duplicate, 0) = 0 ORDER BY first_seen DESC LIMIT ?",
            (w["seller"], w["source"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit)).fetchall()
    return [dict(r) for r in rows]


def alert(cfg: dict) -> int:
    """One push per new listing from a watched seller (a summary when many).
    Only listings first seen after the seller was added, and in the last
    WINDOW_HOURS, so adding a seller never announces old history."""
    watched = load()
    if not watched:
        return 0
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS seller_watch_alerted (url TEXT PRIMARY KEY, at TEXT)")
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE first_seen > ? AND seller IS NOT NULL AND url IS NOT NULL"
            " AND COALESCE(hidden, 0) = 0 AND COALESCE(duplicate, 0) = 0 AND COALESCE(sold, 0) = 0"
            " ORDER BY first_seen", (since,))]
        done = {u for (u,) in conn.execute("SELECT url FROM seller_watch_alerted")}
    hits = []
    for r in rows:
        if r["url"] in done:
            continue
        if any(_same(w, r.get("source_name"), r["seller"]) and r["first_seen"] > w["added"] for w in watched):
            hits.append(r)
    if not hits:
        return 0
    from scrapers.notify import push_enabled, send_push
    if push_enabled(cfg):
        if len(hits) <= ALERT_MAX_SINGLE:
            for r in hits:
                send_push(cfg, f"{r['seller'][:40]} listed: {(r.get('title') or '')[:70]}",
                          f"{r.get('price') or 'no price'} · {_src(r.get('source_name'))}",
                          url=r["url"], tags=["eyes"], image=r.get("image_url"))
        else:
            lines = [f"• {(r.get('title') or '')[:60]} — {r.get('price') or '?'}" for r in hits[:12]]
            more = f"\n…and {len(hits) - 12} more" if len(hits) > 12 else ""
            send_push(cfg, f"{len(hits)} new listings from sellers you watch", "\n".join(lines) + more,
                      url=(cfg.get("email") or {}).get("dashboard_url"), tags=["eyes"])
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO seller_watch_alerted VALUES (?, ?)", [(r["url"], now) for r in hits])
        conn.commit()
    logger.info("Watched-seller alerts: %d", len(hits))
    return len(hits)
