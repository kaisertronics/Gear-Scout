"""
"Saved" page: what you paid for your gear vs. buying it new.

Reads a Google Sheet you keep (one row per item: the gear, what you paid,
and optionally its new price and the date). Any row without a new price
gets one from Reverb's "Brand New" listings (median), cached for a month.
"""
import csv
import io
import json
import logging
import re
import statistics
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

NEW_CACHE = Path("/data/new_prices.json")
NEW_MAX_AGE = 30 * 86400

# Column headers we recognise (lower-cased, punctuation removed).
_ITEM = ("item", "gear", "model", "name", "product", "equipment")
_PAID = ("paid", "pricepaid", "ipaid", "cost", "price", "boughtfor", "purchaseprice")
_NEW = ("new", "newprice", "msrp", "retail", "list", "listprice", "newcost", "streetprice")
_DATE = ("date", "bought", "purchased", "when", "datebought")
_WHERE = ("where", "source", "from", "seller", "site")


def csv_url(sheet_url: str) -> Optional[str]:
    """A Google Sheets link (any form) → its CSV export link. The sheet must be
    shared as "Anyone with the link can view" (or published to the web)."""
    url = (sheet_url or "").strip()
    if not url:
        return None
    if "output=csv" in url or "format=csv" in url:
        return url
    m = re.search(r"/spreadsheets/d/(?:e/)?([a-zA-Z0-9_-]+)", url)
    if not m:
        return None
    gid = re.search(r"[#&?]gid=(\d+)", url)
    if "/d/e/" in url:  # "Publish to the web" link
        return f"https://docs.google.com/spreadsheets/d/e/{m.group(1)}/pub?output=csv" + (f"&gid={gid.group(1)}" if gid else "")
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def _money(v) -> Optional[float]:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _col(headers: list[str], names) -> Optional[int]:
    norm = [re.sub(r"[^a-z]", "", h.lower()) for h in headers]
    for want in names:
        for i, h in enumerate(norm):
            if h == want:
                return i
    for want in names:
        for i, h in enumerate(norm):
            if h and want in h:
                return i
    return None


def read_sheet(sheet_url: str) -> tuple[list[dict], Optional[str]]:
    """([{item, paid, new, date, where}], error message or None). Works with
    no header row: column A = gear, B = price paid, C = new price (optional)."""
    url = csv_url(sheet_url)
    if not url:
        return [], "That doesn't look like a Google Sheets link."
    try:
        r = requests.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        return [], f"Couldn't reach Google Sheets ({e.__class__.__name__})."
    if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""):
        return [], "Google won't share that sheet yet: in Sheets choose Share → General access → Anyone with the link → Viewer."
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="replace"))))
    if not rows:
        return [], "The sheet is empty."
    head = rows[0]
    ci, cp = _col(head, _ITEM), _col(head, _PAID)
    if ci is None or cp is None:
        # No header row: column A = the gear, B = what you paid, C = new
        # price if you typed one (blank → looked up).
        ci, cp, cn, cd, cw = 0, 1, 2, None, None
        body = rows
    else:
        cn, cd, cw = _col(head, _NEW), _col(head, _DATE), _col(head, _WHERE)
        if cn == cp:
            cn = None
        body = rows[1:]
    out = []
    for row in body:
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        item, paid = get(ci), _money(get(cp))
        if not item or paid is None:
            continue
        out.append({"item": item, "paid": paid, "new": _money(get(cn)) if cn is not None else None,
                    "date": get(cd), "where": get(cw)})
    return out, None


def _load_cache() -> dict:
    try:
        return json.loads(NEW_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def new_price(item: str, refresh: bool = False) -> tuple[Optional[float], str]:
    """(today's new price, where it came from) for a model, from Reverb's
    Brand New listings. Cached a month; (None, reason) when nothing's new."""
    key = item.strip().lower()
    cache = _load_cache()
    hit = cache.get(key)
    if hit and not refresh and time.time() - hit.get("at", 0) < NEW_MAX_AGE:
        return hit.get("value"), hit.get("basis", "")
    value, basis = None, "no new listings found"
    try:
        from scrapers.market import _reverb_by_condition
        prices = (_reverb_by_condition(item).get("brand new") or [])
        if prices:
            value = round(statistics.median(prices), 2)
            basis = f"Reverb new, median of {len(prices)}"
    except Exception as e:
        logger.debug("New price lookup failed for %s: %s", item, e)
        return None, "lookup failed"
    cache[key] = {"value": value, "basis": basis, "at": time.time()}
    try:
        NEW_CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return value, basis


def build(sheet_url: str, refresh: bool = False) -> dict:
    """Everything the page and the slides show."""
    rows, error = read_sheet(sheet_url)
    items, running = [], 0.0
    for r in rows:
        new, basis = (r["new"], "from your sheet") if r["new"] else new_price(r["item"], refresh)
        saved = round(new - r["paid"], 2) if new else None
        if saved is not None:
            running += saved
        items.append({**r, "new": new, "basis": basis, "saved": saved,
                      "pct": round(100 * saved / new) if saved is not None and new else None,
                      "running": round(running, 2)})
    priced = [i for i in items if i["saved"] is not None]
    total_new = sum(i["new"] for i in priced)
    total_paid = sum(i["paid"] for i in priced)
    return {"items": items, "error": error, "count": len(priced), "missing": len(items) - len(priced),
            "total_saved": round(total_new - total_paid, 2), "total_new": round(total_new, 2),
            "total_paid": round(total_paid, 2),
            "pct": round(100 * (total_new - total_paid) / total_new) if total_new else 0,
            "best": max(priced, key=lambda i: i["saved"], default=None)}
