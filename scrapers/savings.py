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


_QTY = re.compile(r"(?:\b[x×]\s*(\d{1,2})\b|\b(\d{1,2})\s*[x×]\b|\((\d{1,2})\)|\bpair\b)", re.I)


def split_qty(item: str) -> tuple[str, int]:
    """("CAPI VP28 x 8" → ("CAPI VP28", 8)); "pair" counts as 2."""
    m = _QTY.search(item or "")
    if not m:
        # "Hedd type 20's" = a pair of them.
        if re.search(r"\d['’]s\b", item or ""):
            return item.strip(), 2
        return item.strip(), 1
    n = next((int(g) for g in m.groups() if g), 2)
    name = (item[:m.start()] + " " + item[m.end():])
    return re.sub(r"\s+", " ", name).strip(" -,/"), max(n, 1)


def _query(name: str) -> str:
    """Shorthand → a search: "Hedd type 20's" → "hedd type 20",
    "la-2a audioscape" → "audioscape la-2a", "distressors" → "distressor"."""
    q = re.sub(r"(?<=\w)['’]s\b", "", name)
    q = re.sub(r"[^\w\s\-.]", " ", q)
    q = re.sub(r"\b([a-z]{5,}[^s\W])s\b", r"\1", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    try:
        from scrapers.enrich import _BRAND_RE
        m = _BRAND_RE.search(q.lower())
        if m and m.start() > 0 and not q[m.end():].strip():  # "la-2a audioscape" → brand first
            q = (m.group(0) + " " + q[:m.start()] + q[m.end():]).strip()
            q = re.sub(r"\s+", " ", q)
    except ImportError:
        pass
    return q


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", re.sub(r"[-.]", "", (text or "").lower())))


def telex_value(q: str) -> tuple[Optional[float], str]:
    """The used price Gear Scout already keeps for the Telex term that best
    names this item ("distressor", "vanguard v13"). One-word brand terms
    ("audioscape", "ssl") are too broad to price a specific piece."""
    try:
        from scrapers.store import _conn
        with _conn() as conn:
            rows = conn.execute("SELECT query, typical, samples FROM market_prices "
                                "WHERE model_key LIKE 'telex:%' AND typical IS NOT NULL").fetchall()
    except Exception:
        return None, ""
    from scrapers.enrich import canonical_brand
    have = _words(q)
    brand = canonical_brand(q)

    def has(w):  # "v13" in the term counts for "vanguard 13"
        return w in have or (re.fullmatch(r"[a-z]{1,2}\d+", w) and re.sub(r"^[a-z]+", "", w) in have)

    best = None
    for term, typical, n in rows:
        tw = _words(term)
        specific = len(tw) >= 2 or any(re.search(r"\d", w) for w in tw) or term.lower() in ("distressor",)
        if not (tw and specific and all(has(w) for w in tw)):
            continue
        if brand and canonical_brand(term) != brand:
            continue  # "LA-2A" prices a Teletronix, not an Audioscape
        if best is None or len(tw) > len(_words(best[0])):
            best = (term, typical, n)
    if not best:
        return None, ""
    return round(best[1], 2), f"used, Gear Scout Telex price for “{best[0]}” ({best[2]} listings)"


_PART_NO = re.compile(r"\b\d{5,}-\d{1,3}\b|\b(?:only|card|board|cabinet|faceplate|knob|manual|schematic)\b", re.I)
_seen_q: dict[str, tuple[float, tuple[list, list]]] = {}


def _reverb_new_used(query: str) -> tuple[list[float], list[float]]:
    """(brand-new prices, used prices) on Reverb for the whole piece:
    parts, cards, cabinets, add-ons, broken units and reposts left out."""
    hit = _seen_q.get(query)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    from scrapers.enrich import parse_price
    from scrapers.market import API_URL, HEADERS
    r = requests.get(API_URL, params={"query": query, "per_page": 50}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    new, used, seen = [], [], set()
    for it in r.json().get("listings", []) or []:
        title = it.get("title") or ""
        price = it.get("price") or {}
        cond = ((it.get("condition") or {}).get("display_name") or "").lower()
        value = parse_price(str(price.get("amount") or ""))
        if (price.get("currency") != "USD" or cond == "non functioning" or (title.lower(), value) in seen
                or not _whole_piece(title, query, value)):
            continue
        seen.add((title.lower(), value))
        (new if cond == "brand new" else used).append(value)
    _seen_q[query] = (time.time(), (new, used))
    return new, used


def _whole_piece(title: str, query: str, value) -> bool:
    from scrapers.board import _is_addon
    from scrapers.enrich import needs_repair
    from scrapers.market import _usable
    return (_usable(title, query, value) and not _is_addon(title) and not _PART_NO.search(title)
            and not needs_repair(title))


def scouted_used(query: str) -> list[float]:
    """Prices of listings Gear Scout itself has found for this piece (every
    source, live or sold): the used prices you see in the app."""
    from scrapers.enrich import parse_price
    from scrapers.market import _GENERIC
    from scrapers.store import _conn
    words = [w for w in re.findall(r"[\w-]+", query.lower()) if w not in _GENERIC]
    if not words:
        return []
    anchor = max(words, key=lambda w: (bool(re.search(r"\d", w)) and bool(re.search(r"[a-z]", w)), len(w)))
    with _conn() as conn:
        rows = conn.execute("SELECT title, price FROM seen WHERE lower(title) LIKE ? AND price IS NOT NULL "
                            "AND COALESCE(duplicate, 0) = 0 ORDER BY first_seen DESC LIMIT 400",
                            (f"%{anchor}%",)).fetchall()
    out, seen = [], set()
    for title, price in rows:
        value = parse_price(str(price))
        if (title.lower(), value) in seen or not _whole_piece(title, query, value):
            continue
        seen.add((title.lower(), value))
        out.append(value)
    return out


def new_price(item: str, refresh: bool = False) -> tuple[Optional[float], str]:
    """(what one unit costs today, where that came from): the Brand New
    price on Reverb when the model is still sold new, else its going used
    price (the Telex term's price, else listings Gear Scout has found, else
    Reverb used) — whichever is higher (a "new" price under the used one is
    a clone or accessory). Shorthand names are loosened step by step like
    Gear Scout's market lookups. Cached a month; (None, reason) if nothing."""
    q = _query(item)
    key = "v6:" + q.lower()
    cache = _load_cache()
    hit = cache.get(key)
    if hit and not refresh and time.time() - hit.get("at", 0) < NEW_MAX_AGE:
        return hit.get("value"), hit.get("basis", "")
    value, basis = None, "no listings found"
    tv, tbasis = telex_value(q)
    rough = None  # one listing, if that's all there is anywhere
    try:
        from scrapers.market import lookup_plan
        for i, attempt in enumerate(lookup_plan(q)):
            if i:
                time.sleep(0.4)
            new, used = _reverb_new_used(attempt)
            scouted = scouted_used(attempt)
            if rough is None and (scouted or used or new):
                rough = (scouted or used or new)[0]
            new_med = statistics.median(new) if len(new) >= 2 else None
            used_med, used_basis = None, ""
            if tv:
                used_med, used_basis = tv, tbasis
            elif len(scouted) >= 3 or (len(scouted) >= 2 and len(used) < 3):
                used_med, used_basis = statistics.median(scouted), f"used, median of {len(scouted)} listings Gear Scout found"
            elif len(used) >= 3:
                used_med, used_basis = statistics.median(used), f"used on Reverb, median of {len(used)}"
            if new_med and (not used_med or new_med >= used_med):
                value, basis = round(new_med, 2), f"new on Reverb, median of {len(new)}"
            elif used_med:
                value, basis = round(used_med, 2), used_basis
            if value:
                if attempt != q.lower() and not basis.startswith("used, Gear Scout Telex"):
                    basis += f" (“{attempt}”)"
                break
    except Exception as e:
        logger.debug("Price lookup failed for %s: %s", item, e)
        if not tv:
            return None, "lookup failed"
    if not value and tv:
        value, basis = tv, tbasis
    if not value and rough:
        value, basis = round(rough, 2), "rough: only 1 listing found"
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
        name, qty = split_qty(r["item"])
        if r["new"]:
            new, basis = r["new"], "from your sheet"
        else:
            unit, basis = new_price(name, refresh)
            new = round(unit * qty, 2) if unit else None
            if unit and qty > 1:
                basis = f"{qty} × ${unit:,.0f} · " + basis
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
