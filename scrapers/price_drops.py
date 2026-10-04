"""
Price drops that turn an ad into a deal.

A seller who cuts the price is usually motivated (it hasn't sold, they want
it gone) — and a drop that takes a listing 15%+ under what the gear sells
for used is worth knowing about right away. Every hour: listings whose price
dropped in the last day, in the owner's budget, now 15%+ under a solid comp
→ one push + email. Each listing is announced once per price.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape

logger = logging.getLogger(__name__)
MIN_PCT = 15
BUDGET = (300, 2500)  # owner shops $500–$2,000; a little room either side


def recent_drops(cfg: dict, hours: int = 24, min_pct: int = MIN_PCT) -> list[dict]:
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form,
                                 needs_repair, title_says_sold)
    from scrapers.market import load_market
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE price_dropped_at >= ? AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND previous_price IS NOT NULL", (since,))]
    exclude = cfg.get("exclude_words") or []
    market, index, similar = load_market(), price_index(), similar_index()
    out, seen = [], set()
    for r in rows:
        title = r.get("title") or ""
        if r["url"] in seen or exclude_match(title, exclude) or is_not_audio(title) or is_accessory_only(title) \
                or is_partial(title) or title_says_sold(title) or item_form(title) in ("pedal", "plugin") \
                or needs_repair(title, r.get("description")) or (r.get("description") or "").startswith("Auction"):
            continue
        c = evaluate(title, r["price"], r.get("description"), r["url"], market, index, similar)
        if not c or c["est"] or not (BUDGET[0] <= c["value"] <= BUDGET[1]) or not (min_pct <= c["pct"] <= 80):
            continue
        seen.add(r["url"])
        r["comp"] = c
        out.append(r)
    return sorted(out, key=lambda r: -r["comp"]["pct"])


def alert(cfg: dict) -> int:
    from scrapers.store import _conn
    rows = recent_drops(cfg)
    if not rows:
        return 0
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS drop_alerted (url TEXT, price TEXT, at TEXT, PRIMARY KEY (url, price))")
        done = {(u, p) for u, p in conn.execute("SELECT url, price FROM drop_alerted")}
    rows = [r for r in rows if (r["url"], r["price"]) not in done][:10]
    if not rows:
        return 0
    from scrapers.emailer import send_html_email
    from scrapers.notify import push_enabled, send_push
    email_cfg = cfg.get("email") or {}
    line = lambda r: (f"{r['title'][:60]} — now {r['price']} (was {r['previous_price']}), "
                      f"{r['comp']['pct']}% under ~${r['comp']['ref']:,.0f}")
    title = f"Price drop{'s' if len(rows) > 1 else ''}: {len(rows)} now a deal"
    if push_enabled(cfg):
        send_push(cfg, title, "\n".join("• " + line(r) for r in rows[:6]),
                  url=rows[0]["url"] if len(rows) == 1 else email_cfg.get("dashboard_url"), tags=["chart_with_downwards_trend"])
    if email_cfg.get("from") and email_cfg.get("password"):
        items = "".join(
            f'<div style="border-top:1px solid #e2e8f0;padding:10px 0;"><a href="{escape(r["url"])}" '
            f'style="color:#1e3a5f;font-weight:700;text-decoration:none;">{escape(r["title"])}</a><br>'
            f'<span style="color:#166534;font-weight:700;">{escape(r["price"])}</span> '
            f'<span style="color:#94a3b8;text-decoration:line-through;">{escape(r["previous_price"])}</span>'
            f'<span style="color:#64748b;font-size:13px;"> · {r["comp"]["pct"]}% under {escape(r["comp"]["label"])} '
            f'~${r["comp"]["ref"]:,.0f} · {escape((r.get("source_name") or "").split(" — ")[0])}</span></div>' for r in rows)
        html = (f'<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,sans-serif;">'
                f'<div style="max-width:640px;margin:0 auto;padding:20px;"><div style="background:#1e3a5f;color:#fff;'
                f'padding:16px 20px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">📉 {escape(title)}</div>'
                f'<div style="background:#fff;padding:16px 20px;border-radius:0 0 12px 12px;color:#1e293b;">'
                f'<p>These sellers just cut their price, and it is now well under what the gear sells for used. '
                f'A seller who is dropping the price is usually open to an offer too.</p>{items}</div></div></body></html>')
        send_html_email(html, f"Gear Scout — {title}", email_cfg)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO drop_alerted VALUES (?, ?, ?)", [(r["url"], r["price"], now) for r in rows])
        conn.commit()
    logger.info("Price-drop alerts: %d", len(rows))
    return len(rows)
