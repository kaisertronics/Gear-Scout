"""
Deal calibration report (Savings page card).

For each recent purchase on the Savings sheet, looks for a listing Gear Scout
had seen before the purchase date: same model words in the title, asking price
within ~15% of what was paid. Shows how many hours ahead Gear Scout first saw
it and the discount vs the going used value. Read-only, no network.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MIN_PURCHASES = 3
PRICE_TOL = 0.15
MAX_ROWS = 12
LOOKBACK_DAYS = 365
CACHE_TTL = 1800
_cache: dict = {"key": None, "at": 0.0, "out": None}
_lock = threading.Lock()  # one table read at a time; other callers wait and reuse it
_STOP = {"the", "and", "for", "with", "used", "new", "mic", "microphone", "pair", "x", "of"}
_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d")


def _when(s) -> datetime | None:
    s = str(s or "").strip()
    if not s:
        return None
    d = None
    for f in _FORMATS:
        try:
            d = datetime.strptime(s, f)
            break
        except ValueError:
            continue
    if d is None:
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _STOP and (len(w) > 1 or w.isdigit())}


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^0-9.]", "", str(v)))
    except (TypeError, ValueError):
        return None


def calibrate(purchases: list[dict], listings: list[dict]) -> dict | None:
    """Pure. purchases: item, paid, date, used (optional used value).
    listings: title, price, first_seen (ISO). Returns {"rows", "m", "n",
    "median_lead", "median_discount", "text"} or None when fewer than
    MIN_PURCHASES purchases have a usable date and price."""
    ls = []
    for l in listings or []:
        t, p = _when(l.get("first_seen")), _num(l.get("price"))
        w = _words(l.get("title"))
        if t and p and p > 0 and w:
            ls.append((t, p, w, l.get("title")))
    rows, m = [], 0
    for p in purchases or []:
        bought, paid = _when(p.get("date")), _num(p.get("paid"))
        iw = _words(p.get("item"))
        if not bought or not paid or paid <= 0 or not iw:
            continue
        m += 1
        best = None
        for t, price, w, title in ls:
            if t >= bought or abs(price - paid) > PRICE_TOL * paid or not iw <= w:
                continue
            if best is None or t < best[0]:
                best = (t, title)
        used = _num(p.get("used"))
        row = {"item": p.get("item"), "paid": paid, "lead_h": None, "discount_pct": None}
        if used and used > 0:
            row["discount_pct"] = round((1 - paid / used) * 100)
        if best:
            row["lead_h"] = round((bought - best[0]).total_seconds() / 3600)
        rows.append((bought, row))
    if m < MIN_PURCHASES:
        return None
    rows.sort(key=lambda r: r[0], reverse=True)
    out = [r for _, r in rows]
    leads = [r["lead_h"] for r in out if r["lead_h"] is not None]
    discs = [r["discount_pct"] for r in out if r["discount_pct"] is not None]
    ml = round(statistics.median(leads)) if leads else None
    md = round(statistics.median(discs)) if discs else None
    text = f"Gear Scout had seen {len(leads)} of {m} purchases first"
    if ml is not None:
        text += f", median lead {ml} h"
    if md is not None:
        text += f", median discount {md}%"
    return {"rows": out[:MAX_ROWS], "m": m, "n": len(leads), "median_lead": ml,
            "median_discount": md, "text": text}


def _listings() -> list[dict]:
    from scrapers.store import DB_PATH
    cut = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS * 2)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("SELECT title, price, first_seen FROM seen WHERE first_seen >= ?"
                            " AND price IS NOT NULL AND title IS NOT NULL", (cut,)).fetchall()
    finally:
        conn.close()
    return [{"title": t, "price": p, "first_seen": f} for t, p, f in rows]


def report(items: list[dict]) -> dict | None:
    """Cached 30 minutes. Never raises; None = render nothing."""
    try:
        key = tuple((i.get("item"), i.get("paid"), i.get("date")) for i in items or [])

        def fresh():
            return _cache["key"] == key and time.time() - _cache["at"] < CACHE_TTL
        if fresh():
            return _cache["out"]
        with _lock:
            if fresh():
                return _cache["out"]
            cut = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
            recent = [i for i in items or [] if (_when(i.get("date")) or cut) >= cut]
            out = calibrate(recent, _listings())
            _cache.update(key=key, at=time.time(), out=out)
            return out
    except Exception:
        logger.exception("deal calibration")
        return None
