"""
Sold-out alert for starred listings.

When a listing the owner has starred turns sold/gone (seen.sold = 1, with the
time it was noticed in seen.sold_at), send one push + one short email. Only
listings that turned sold in the last 48 hours count, so the first run never
announces old history. Each listing is announced once (sold_alerted table).
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape

logger = logging.getLogger(__name__)
WINDOW_HOURS = 48


def recent_sold_favorites(hours: int = WINDOW_HOURS) -> list[dict]:
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE favorite = 1 AND COALESCE(sold, 0) = 1"
            " AND sold_at IS NOT NULL AND sold_at >= ? ORDER BY sold_at", (since,))]


def alert(cfg: dict) -> int:
    from scrapers.store import _conn
    rows = recent_sold_favorites()
    if not rows:
        return 0
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sold_alerted (url TEXT PRIMARY KEY, at TEXT)")
        done = {u for (u,) in conn.execute("SELECT url FROM sold_alerted")}
    rows = [r for r in rows if r["url"] not in done][:10]
    if not rows:
        return 0
    from scrapers.emailer import send_html_email
    from scrapers.notify import push_enabled, send_push
    email_cfg = cfg.get("email") or {}
    src = lambda r: (r.get("source_name") or "").split(" — ")[0]
    line = lambda r: f"Sold: {(r.get('title') or '')[:70]} (was {r.get('price') or '?'}, {src(r)})"
    title = "Sold: a starred listing is gone" if len(rows) == 1 else f"Sold: {len(rows)} starred listings are gone"
    if push_enabled(cfg):
        send_push(cfg, title, "\n".join(line(r) for r in rows[:6]),
                  url=rows[0]["url"] if len(rows) == 1 else email_cfg.get("dashboard_url"), tags=["no_entry"])
    if email_cfg.get("from") and email_cfg.get("password"):
        items = "".join(
            f'<div style="border-top:1px solid #e2e8f0;padding:10px 0;"><a href="{escape(r["url"])}" '
            f'style="color:#1e3a5f;font-weight:700;text-decoration:none;">{escape(r.get("title") or "")}</a><br>'
            f'<span style="color:#64748b;font-size:13px;">was {escape(str(r.get("price") or "?"))} · '
            f'{escape(src(r))}</span></div>' for r in rows)
        html = (f'<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,sans-serif;">'
                f'<div style="max-width:640px;margin:0 auto;padding:20px;"><div style="background:#1e3a5f;color:#fff;'
                f'padding:16px 20px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">&#128683; {escape(title)}</div>'
                f'<div style="background:#fff;padding:16px 20px;border-radius:0 0 12px 12px;color:#1e293b;">'
                f'<p>A listing you starred sold or was taken down.</p>{items}</div></div></body></html>')
        send_html_email(html, f"Gear Scout — {title}", email_cfg)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO sold_alerted VALUES (?, ?)", [(r["url"], now) for r in rows])
        conn.commit()
    logger.info("Sold-favorite alerts: %d", len(rows))
    return len(rows)
