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


_NOT_PLURAL = {"series", "dynamics", "electronics", "acoustics", "labs", "systems", "products", "works",
               "instruments", "sounds", "tubes", "records", "studios", "designs", "engineers", "devices",
               "bros", "amps"}


def split_qty(item: str) -> tuple[str, int]:
    """("CAPI VP28 x 8" → ("CAPI VP28", 8)); "pair" counts as 2."""
    m = _QTY.search(item or "")
    if not m:
        # "Hedd type 20's", "mixcubes", "NS10s", "mics" = a pair of them.
        if re.search(r"\d['’]s\b|\b[a-z]+\d+s\b", item or "", re.I) or any(
                (re.fullmatch(r"[a-z]{3,}[^su'’]s", w) or w == "mics") and w not in _NOT_PLURAL
                for w in re.findall(r"[a-z'’]+", (item or "").lower())):
            return item.strip(), 2
        return item.strip(), 1
    n = next((int(g) for g in m.groups() if g), 2)
    name = (item[:m.start()] + " " + item[m.end():])
    return re.sub(r"\s+", " ", name).strip(" -,/"), max(n, 1)


def _query(name: str) -> str:
    """Shorthand → a search: "Hedd type 20's" → "hedd type 20",
    "la-2a audioscape" → "audioscape la-2a", "distressors" → "distressor"."""
    q = re.sub(r"(?<=\w)['’]s\b", "", name)
    q = re.sub(r"(?i)\b([a-z]+\d+)s\b", r"\1", q)  # "NS10s" → "NS10"
    q = re.sub(r"[^\w\s\-.]", " ", q)
    q = re.sub(r"\b([a-z]{5,}[^s\W])s\b", r"\1", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip()
    q = re.sub(r"(?i)\b(avalon)\s+(?:vt-?)?737(?:sp)?\b", r"\1 vt-737sp", q)
    q = re.sub(r"(?i)\bcapi\s*vm-?28\b", "capi vp28", q)  # CAPI's preamp is the VP28
    # "beyer 818 mics" → "beyerdynamic m818" (Beyer's mics are the M series).
    q = re.sub(r"(?i)\bbeyer(?:dynamic)?\b", "beyerdynamic", q)
    if re.search(r"(?i)\bbeyerdynamic\b", q) and re.search(r"(?i)\bmics?\b", name):
        q = re.sub(r"(?i)\b(?:m\s?)?(\d{2,3})\b", r"m\1", q)
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


def telex_value(q: str) -> tuple[Optional[float], str, Optional[str]]:
    """The used price Gear Scout already keeps for the Telex term that best
    names this item ("distressor", "vanguard v13"). One-word brand terms
    ("audioscape", "ssl") are too broad to price a specific piece."""
    try:
        from scrapers.store import _conn
        with _conn() as conn:
            rows = conn.execute("SELECT query, typical, samples FROM market_prices "
                                "WHERE model_key LIKE 'telex:%' AND typical IS NOT NULL").fetchall()
    except Exception:
        return None, "", None
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
        return None, "", None
    return round(best[1], 2), f"your Telex price for “{best[0]}” ({best[2]} listings)", best[0]


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
        (new if cond == "brand new" else used).append(_unit(title, value))
    _seen_q[query] = (time.time(), (new, used))
    return new, used


# Words that pick a version of the same product: "MixCubes active" must not
# be priced off passive MixCubes (the price search drops "active" as filler).
_VERSIONS = ((r"\bactive\b|\bpowered\b", r"\bpassive\b"),)


def _whole_piece(title: str, query: str, value) -> bool:
    from scrapers.board import _is_addon
    from scrapers.enrich import needs_repair
    from scrapers.market import _usable
    for a, b in _VERSIONS:
        for want, avoid in ((a, b), (b, a)):
            if re.search(want, query, re.I) and (re.search(avoid, title, re.I) or not re.search(want, title, re.I)):
                return False
    # A pair (monitors, mics) is the same gear twice, not a lot: judge it
    # as one, and _unit prices it per piece.
    single = re.sub(r"\(?\b(?:matched |stereo )?pair\b\)?|\(set\)", " ", title, flags=re.I)
    return (_usable(single, query, value) and not _is_addon(single) and not _PART_NO.search(title)
            and not needs_repair(title))


def _unit(title: str, value: float) -> float:
    """One unit's price: a pair listed at $400 is $200 each."""
    from scrapers.enrich import price_basis
    title = re.sub(r"\(set\)", "(pair)", title, flags=re.I)
    return price_basis(title, None, value)[2]


_SAYS_NEW = re.compile(r"\bbrand[- ]new\b|\bnew in box\b|\bnib\b|\bbnib\b|\bsealed\b|\bunopened\b|\bnever opened\b", re.I)


def scouted_used(query: str) -> list[float]:
    """Used prices of listings Gear Scout itself has found for this piece
    (every source, live or sold): the used prices you see in the app."""
    return _scouted(query)[0]


def scouted_new(query: str) -> list[float]:
    """Listings Gear Scout found that say they're brand new / sealed (gear
    sold new only by its maker shows up this way)."""
    return _scouted(query)[1]


_scouted_cache: dict[str, tuple[list, list]] = {}


def _scouted(query: str) -> tuple[list[float], list[float]]:
    from scrapers.enrich import parse_price
    from scrapers.market import _GENERIC
    from scrapers.store import _conn
    words = [w for w in re.findall(r"[\w-]+", query.lower()) if w not in _GENERIC]
    if not words:
        return [], []
    if query in _scouted_cache:
        return _scouted_cache[query]
    anchor = max(words, key=lambda w: (bool(re.search(r"\d", w)) and bool(re.search(r"[a-z]", w)), len(w)))
    with _conn() as conn:
        rows = conn.execute("SELECT title, price FROM seen WHERE lower(title) LIKE ? AND price IS NOT NULL "
                            "AND COALESCE(duplicate, 0) = 0 ORDER BY first_seen DESC LIMIT 400",
                            (f"%{anchor}%",)).fetchall()
    used, new, seen = [], [], set()
    for title, price in rows:
        value = parse_price(str(price))
        if (title.lower(), value) in seen or not _whole_piece(title, query, value):
            continue
        seen.add((title.lower(), value))
        (new if _SAYS_NEW.search(title) else used).append(_unit(title, value))
    if len(_scouted_cache) > 500:
        _scouted_cache.clear()
    _scouted_cache[query] = (used, new)
    return used, new


def _ebay_new(query: str) -> list[float]:
    """Buy-it-now prices of eBay listings in "New" condition (whole piece)."""
    import yaml
    from scrapers.ebay_api import SEARCH_URL, _check_response, _get_token, ebay_paused_until
    try:
        api = (yaml.safe_load(open("/config/config.yaml")) or {}).get("ebay_api") or {}
    except OSError:
        return []
    if ebay_paused_until() or not (api.get("client_id") and api.get("client_secret")):
        return []
    token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
    from scrapers.ebay_api import ebay_get
    r = ebay_get(SEARCH_URL, job="lookups", params={"q": query, "limit": 50,
                                                    "filter": "conditionIds:{1000},buyingOptions:{FIXED_PRICE},priceCurrency:USD"},
                 headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=20)
    _check_response(r)
    r.raise_for_status()
    out, seen = [], set()
    for it in r.json().get("itemSummaries", []) or []:
        title = it.get("title") or ""
        try:
            value = float((it.get("price") or {}).get("value"))
        except (TypeError, ValueError):
            continue
        if (title.lower(), value) not in seen and _whole_piece(title, query, value):
            seen.add((title.lower(), value))
            out.append(_unit(title, value))
    return out


def _used_pick(tv, tbasis, scouted, used):
    """(going used price, basis, listings) from the best source available."""
    if tv:
        return tv, tbasis, None
    if len(scouted) >= 3 or (len(scouted) >= 2 and len(used) < 3):
        return statistics.median(scouted), f"median of {len(scouted)} listings Gear Scout found", len(scouted)
    if len(used) >= 3:
        return statistics.median(used), f"Reverb used, median of {len(used)}", len(used)
    return None


def prices(item: str, refresh: bool = False) -> dict:
    """Today's prices for one unit of a piece of gear:
      new  — Brand New on Reverb + New-condition on eBay (median),
      used — your Telex term's price, else listings Gear Scout has found,
             else Reverb used; a single listing counts as a rough price.
    New listings far under (or over) the going price are accessories,
    parts, clones or bundles and are dropped one by one. Shorthand names are
    loosened step by step like Gear Scout's market lookups. Cached a month.
    {new, new_basis, used, used_basis, rough}"""
    q = _query(item)
    key = "v12:" + q.lower()
    cache = _load_cache()
    hit = cache.get(key)
    if hit and not refresh and time.time() - hit.get("at", 0) < NEW_MAX_AGE:
        return hit["p"]
    tv, tbasis, term = telex_value(q)
    if term and len(term) >= len(q) - 2:
        q = term.lower()  # "vanguard 13" searches as your term "vanguard v13"
    out = {"new": None, "new_basis": "no new price found", "used": None,
           "used_basis": "no used listings found", "rough": False}
    first_used = None
    try:
        from scrapers.market import lookup_plan
        for i, attempt in enumerate(lookup_plan(q)):
            if i:
                time.sleep(0.4)
            new_r, used = _reverb_new_used(attempt)
            try:
                new_e = _ebay_new(attempt)
            except Exception as e:
                logger.debug("eBay new lookup failed for %s: %s", attempt, e)
                new_e = []
            scouted, new_s = _scouted(attempt)
            if out["used"] is None:
                pick = _used_pick(tv, tbasis, scouted, used)
                if pick:
                    out["used"], out["used_basis"] = round(pick[0], 2), pick[1]
                elif first_used is None and (scouted or used):
                    first_used = (scouted or used)[0]
            if out["new"] is None and (new_r or new_e or new_s):
                # Yardstick for junk: the going used price, else Reverb's
                # new listings (dealers), else everything new.
                ref = out["used"] or statistics.median(new_r or (new_r + new_e + new_s))
                ok = lambda vs, lo=0.5: [v for v in vs if lo * ref <= v <= 4 * ref]
                # A private "brand new" ad under the used price is a bargain,
                # not what new costs: only count those at or over used.
                keep_r, keep_e = ok(new_r), ok(new_e)
                keep_s = ok(new_s, 1.0 if out["used"] else 0.5)
                if len(keep_r) + len(keep_e) + len(keep_s) >= 2 or keep_r or keep_s:
                    where = " + ".join(n for n, v in (("Reverb", keep_r), ("eBay", keep_e),
                                                      ("Gear Scout listings", keep_s)) if v)
                    keep_r = keep_r + keep_s
                    n = len(keep_r) + len(keep_e)
                    out["new"] = round(statistics.median(keep_r + keep_e), 2)
                    out["new_basis"] = f"{where} new, median of {n}" + (" (rough)" if n == 1 else "")
                    if attempt != q.lower():
                        out["new_basis"] += f" (“{attempt}”)"
            if out["new"] is not None and out["used"] is not None:
                break
    except Exception as e:
        logger.debug("Price lookup failed for %s: %s", item, e)
        if out["used"] is None and tv:
            out["used"], out["used_basis"] = tv, tbasis
        if out["new"] is None and out["used"] is None:
            return {**out, "new_basis": "lookup failed", "used_basis": "lookup failed"}
    if out["used"] is None and first_used:
        out["used"], out["used_basis"], out["rough"] = round(first_used, 2), "rough: only 1 used listing found", True
    cache[key] = {"p": out, "at": time.time()}
    try:
        NEW_CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return out


def build(sheet_url: str, refresh: bool = False) -> dict:
    """Everything the page and the slides show. Per item: new price (or the
    used price when no new price can be found — that's what "saved" uses), the going
    used price, saved vs new, saved vs the used market, and overpaid."""
    rows, error = read_sheet(sheet_url)
    items, running = [], 0.0
    for r in rows:
        # "4 x CAPI VP28 / 4 x Link SSL 4K" is two things: price each part.
        parts = [x for x in re.split(r"\s+(?:/|\+)\s+", r["item"]) if x.strip()]
        priced = []
        for part in parts:
            name, qty = split_qty(part)
            p = prices(name, refresh)
            each = f"{qty} × " if qty > 1 else ""
            priced.append({
                "qty": qty,
                "new": round(p["new"] * qty, 2) if p["new"] else None,
                "used": round(p["used"] * qty, 2) if p["used"] else None,
                "new_basis": (f"{each}${p['new']:,.0f} · " if p["new"] and qty > 1 else "") + p["new_basis"],
                "used_basis": (f"{each}${p['used']:,.0f} · " if p["used"] and qty > 1 else "") + p["used_basis"],
                "rough": p["rough"]})
        label = lambda k: " + ".join(x[k] for x in priced) if len(priced) > 1 else priced[0][k]
        qty = sum(x["qty"] for x in priced)
        new_v = round(sum(x["new"] for x in priced), 2) if all(x["new"] for x in priced) else None
        used_v = round(sum(x["used"] for x in priced), 2) if all(x["used"] for x in priced) else None
        new_basis, used_basis = label("new_basis"), label("used_basis")
        p = {"rough": any(x["rough"] for x in priced)}
        if r["new"]:
            new_v, new_basis = r["new"], "from your sheet"
        # "Saved" is against new; gear with no new price compares to used
        # (part by part when a row is several things).
        if new_v:
            compare, against = new_v, "new"
        elif all(x["new"] or x["used"] for x in priced) and any(x["new"] for x in priced):
            compare, against = round(sum(x["new"] or x["used"] for x in priced), 2), "mixed"
        else:
            compare, against = used_v, "used"
        saved = round(compare - r["paid"], 2) if compare else None
        if saved is not None:
            running += saved
        vs_market = round(used_v - r["paid"], 2) if used_v else None
        items.append({**r, "qty": qty, "new": compare, "against": against, "new_only": new_v,
                      "basis": new_basis if against == "new" else used_basis + " (no new price found)" if against == "used"
                      else "new where still made, used market average where not",
                      "new_basis": new_basis, "used": used_v, "used_basis": used_basis, "rough": p["rough"],
                      "saved": saved, "pct": round(100 * saved / compare) if saved is not None and compare else None,
                      "vs_market": vs_market,
                      "vs_market_pct": round(100 * vs_market / used_v) if vs_market is not None and used_v else None,
                      "overpaid": vs_market is not None and vs_market < -0.05 * used_v,
                      "running": round(running, 2)})
    priced = [i for i in items if i["saved"] is not None]
    marketed = [i for i in items if i["vs_market"] is not None]
    total_new = sum(i["new"] for i in priced)
    total_paid = sum(i["paid"] for i in priced)
    market_value = sum(i["used"] for i in marketed)
    market_paid = sum(i["paid"] for i in marketed)
    over = sorted([i for i in items if i["overpaid"]], key=lambda i: i["vs_market"])
    return {"items": items, "error": error, "count": len(priced), "missing": len(items) - len(priced),
            "total_saved": round(total_new - total_paid, 2), "total_new": round(total_new, 2),
            "total_paid": round(total_paid, 2),
            "pct": round(100 * (total_new - total_paid) / total_new) if total_new else 0,
            "best": max(priced, key=lambda i: i["saved"], default=None),
            # vs the used market
            "market_count": len(marketed), "market_value": round(market_value, 2),
            "market_paid": round(market_paid, 2), "vs_market": round(market_value - market_paid, 2),
            "vs_market_pct": round(100 * (market_value - market_paid) / market_value) if market_value else 0,
            "overpaid": over, "overpaid_total": round(-sum(i["vs_market"] for i in over), 2),
            "best_buys": sorted([i for i in marketed if i["vs_market"] > 0], key=lambda i: -i["vs_market_pct"])[:5],
            "not_new": [i for i in priced if i["against"] != "new"],
            "rough": [i for i in items if i["rough"] or "(rough)" in (i.get("new_basis") or "")],
            "unpriced": [i for i in items if i["saved"] is None],
            "total_paid_all": round(sum(i["paid"] for i in items), 2)}
