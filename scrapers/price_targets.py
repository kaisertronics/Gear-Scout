"""
Own-price targets.

The owner sets, once per model, the most they will pay ("Neumann U87", $1,800).
Every hour any current listing of that model at or under that price (listed
price plus shipping when known) gets one push + email, even when it is not 50%
under used value. Targets live in /data/price_targets.json (not config.yaml).
"""
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

logger = logging.getLogger(__name__)
PATH = Path("/data/price_targets.json")
FRESH_HOURS = 24  # only listings seen in the last day, so a new target can't flood you with old ads


def load() -> list[dict]:
    try:
        items = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [t for t in items if isinstance(t, dict) and t.get("id") and t.get("text") and t.get("max_price")] \
        if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PATH)


def add(text: str, max_price) -> dict | None:
    """Adds (or updates the price of) a target. None if text/price unusable."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:80]
    try:
        price = int(float(re.sub(r"[^\d.]", "", str(max_price or "")) or 0))
    except ValueError:
        return None
    if not _words(text) or price <= 0:
        return None
    items = load()
    for t in items:
        if _words(t["text"]) == _words(text):
            t["max_price"] = price
            _save(items)
            return t
    t = {"id": uuid.uuid4().hex[:8], "text": text, "max_price": price}
    items.append(t)
    _save(items)
    return t


def remove(tid: str) -> bool:
    items = load()
    kept = [t for t in items if t["id"] != tid]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()


def _matches(target_words: list[str], title: str) -> bool:
    have = set(_words(title))
    joined = "".join(_words(title))
    # "TLM-103" / "TLM 103" / "TLM103" all count: a word also matches inside the squashed title
    return all(w in have or (len(w) >= 3 and w in joined) for w in target_words)


def _candidates(cfg: dict, hours: int | None = None) -> list[dict]:
    """Live, in-country, non-junk listings (optionally only seen in the last `hours`)."""
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial,
                                 needs_repair, parse_price, title_says_sold)
    from scrapers.store import _conn
    sql = ("SELECT * FROM seen WHERE hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
           " AND COALESCE(pending, 0) = 0 AND title IS NOT NULL AND price IS NOT NULL"
           " AND url NOT LIKE '%search/posts%' AND (country IS NULL OR country = 'US')")
    args: tuple = ()
    if hours:
        sql += " AND last_seen >= ?"
        args = ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, args)]
    exclude = cfg.get("exclude_words") or []
    out = []
    for r in rows:
        title, src = r["title"], (r.get("source_name") or "").lower()
        if "kijiji" in src or "mcquade" in src or "C$" in str(r.get("price") or ""):
            continue  # no Canadian listings
        if exclude_match(title, exclude) or is_not_audio(title) or is_accessory_only(title) or is_partial(title) \
                or title_says_sold(title) or needs_repair(title, r.get("description")) \
                or (r.get("description") or "").startswith("Auction"):
            continue
        p = parse_price(r["price"])
        if not p:
            continue
        ship = r.get("shipping")
        r["total"] = p + (ship if isinstance(ship, (int, float)) else 0)
        out.append(r)
    return out


def hits(cfg: dict, targets: list[dict] | None = None, hours: int | None = None) -> dict[str, list[dict]]:
    """target id -> matching listings at/under its max price, cheapest first."""
    targets = load() if targets is None else targets
    if not targets:
        return {}
    rows = _candidates(cfg, hours)
    out = {}
    for t in targets:
        w = _words(t["text"])
        found = sorted((r for r in rows if _matches(w, r["title"]) and r["total"] <= t["max_price"]),
                       key=lambda r: r["total"])
        out[t["id"]] = found
    return out


_status_cache: dict = {"at": 0.0, "key": None, "out": None}
STATUS_TTL = 420  # the scan reads every stored ad (~10 s); the cache warm-up refreshes it every 5 min


def status(cfg: dict) -> list[dict]:
    """For the Mics page: each target with how many live listings match its
    words (at any price) and the cheapest one. Cached a few minutes: it used to
    rescan the whole table on every Mics page load."""
    targets = load()
    if not targets:
        return []
    key = json.dumps(targets, sort_keys=True)
    if _status_cache["key"] == key and time.time() - _status_cache["at"] < STATUS_TTL:
        return _status_cache["out"]
    out = _status(cfg, targets)
    _status_cache.update(at=time.time(), key=key, out=out)
    return out


def _status(cfg: dict, targets: list[dict]) -> list[dict]:
    rows = _candidates(cfg)
    out = []
    for t in targets:
        w = _words(t["text"])
        m = sorted((r for r in rows if _matches(w, r["title"])), key=lambda r: r["total"])
        out.append(dict(t, n=len(m), cheapest=m[0] if m else None,
                        n_under=sum(1 for r in m if r["total"] <= t["max_price"])))
    return out


def alert(cfg: dict) -> int:
    from scrapers.store import _conn
    targets = load()
    if not targets:
        return 0
    found = hits(cfg, targets, hours=FRESH_HOURS)
    by_id = {t["id"]: t for t in targets}
    rows, seen = [], set()
    for tid, ls in found.items():
        for r in ls:
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(dict(r, target=by_id[tid]))
    if not rows:
        return 0
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS target_alerted (url TEXT, price TEXT, at TEXT, PRIMARY KEY (url, price))")
        done = {(u, p) for u, p in conn.execute("SELECT url, price FROM target_alerted")}
    rows = sorted((r for r in rows if (r["url"], r["price"]) not in done), key=lambda r: r["total"])[:10]
    if not rows:
        return 0
    from scrapers.emailer import send_html_email
    from scrapers.notify import push_enabled, send_push
    email_cfg = cfg.get("email") or {}
    ship_txt = lambda r: ("" if not isinstance(r.get("shipping"), (int, float)) else
                          (" + free ship" if r["shipping"] == 0 else f" + ${r['shipping']:,.0f} ship"))
    line = lambda r: (f"{r['title'][:60]} — {r['price']}{ship_txt(r)} "
                      f"(your {r['target']['text']} limit ${r['target']['max_price']:,})")
    title = f"Your price: {len(rows)} listing{'s' if len(rows) > 1 else ''} at or under your limit"
    if push_enabled(cfg):
        send_push(cfg, title, "\n".join("• " + line(r) for r in rows[:6]),
                  url=rows[0]["url"] if len(rows) == 1 else email_cfg.get("dashboard_url"), tags=["dollar"])
    if email_cfg.get("from") and email_cfg.get("password"):
        items = "".join(
            f'<div style="border-top:1px solid #e2e8f0;padding:10px 0;"><a href="{escape(r["url"])}" '
            f'style="color:#1e3a5f;font-weight:700;text-decoration:none;">{escape(r["title"])}</a><br>'
            f'<span style="color:#166534;font-weight:700;">{escape(r["price"])}{escape(ship_txt(r))}</span>'
            f'<span style="color:#64748b;font-size:13px;"> · your limit for {escape(r["target"]["text"])}: '
            f'${r["target"]["max_price"]:,} · {escape((r.get("source_name") or "").split(" — ")[0])}</span></div>'
            for r in rows)
        html = (f'<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,sans-serif;">'
                f'<div style="max-width:640px;margin:0 auto;padding:20px;"><div style="background:#1e3a5f;color:#fff;'
                f'padding:16px 20px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">💲 {escape(title)}</div>'
                f'<div style="background:#fff;padding:16px 20px;border-radius:0 0 12px 12px;color:#1e293b;">'
                f'<p>These match a model you set a maximum price for, and they are at or under it.</p>{items}</div></div>'
                f'</body></html>')
        send_html_email(html, f"Gear Scout — {title}", email_cfg)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO target_alerted VALUES (?, ?, ?)", [(r["url"], r["price"], now) for r in rows])
        conn.commit()
    logger.info("Price-target alerts: %d", len(rows))
    return len(rows)
