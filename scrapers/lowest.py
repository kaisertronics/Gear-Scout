"""
Lowest price tracker: for an item (e.g. "neumann u87ai"), find the 3
cheapest listings across every source right now, and alert — push + email —
when a new listing comes in under the current lowest price.

Reuses live search for the actual searching (same sources, same matching),
so a manual lookup and a tracked item behave identically. Only the listings
found in the most recent check count as "current", so a sold lowest drops
out and the next cheapest becomes the mark.
"""
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from scrapers.enrich import is_partial, needs_repair, parse_price
from scrapers.store import _conn

logger = logging.getLogger(__name__)

TOP_N = 10
MIN_PRICE = 5.0
# A price this far under the model's typical price is almost always a
# placeholder ("$1 — make an offer") rather than a real asking price.
MIN_FRACTION_OF_TYPICAL = 0.1
_WANTED = re.compile(r"^\W*(?:wanted|wtb|iso|in search of|looking for|buying)\b", re.I)
_RENTAL = re.compile(r"\b(?:rental|for rent|rent it|per day|/day|per week|lease)\b", re.I)
# Listings FOR the item rather than the item: "EA87 shock mount (suitable
# for U87)", "sleeve nut replacement for Neumann …".
_FOR_ITEM = re.compile(r"\b(?:suitable for|compatible with|replacement for|designed for|for use with|fits(?: the)?)\b", re.I)
_ACCESSORY = re.compile(
    r"\b(?:shock ?mounts?|suspension|sleeve|nut|clips?|cables?|flight case|case|windscreens?|"
    r"pop filters?|foam|stands?|adapters?|holders?|bags?|covers?|grilles?|spider|"
    r"power suppl(?:y|ies)|psu|manual|box only|decal|badge|knobs?)\b",
    re.I,
)
# Clones/replicas stay in the results (the user wants them) but are tagged,
# so a $400 "U87 replica" isn't mistaken for a Neumann.
_CLONE = re.compile(r"\b(?:replica|clone|copy|tribute|inspired|style|type|kit|diy|based on)\b", re.I)
REPAIR_FLOOR = 0.1      # needs-repair units can legitimately be very cheap
WORKING_FLOOR = 0.25    # below this share of typical it's an accessory/placeholder


def normalize(q: str) -> str:
    return " ".join((q or "").lower().split())


def _ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lowest_state (
            query TEXT PRIMARY KEY,
            lowest REAL,
            top_json TEXT,
            checked_at TEXT,
            seeded INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lowest_seen (
            query TEXT NOT NULL,
            url TEXT NOT NULL,
            price REAL,
            PRIMARY KEY (query, url)
        )
    """)


def tracked_queries(cfg: dict) -> list[str]:
    out = []
    for t in cfg.get("price_trackers") or []:
        q = normalize(t.get("query", "") if isinstance(t, dict) else str(t))
        enabled = t.get("enabled", True) if isinstance(t, dict) else True
        if q and enabled and q not in out:
            out.append(q)
    return out


def _is_accessory(title: str) -> bool:
    m = _ACCESSORY.search(title)
    if not m:
        return False
    before = title[:m.start()].lower()
    # "U87Ai with shock mount and case" is the mic, extras included.
    return not re.search(r"\b(?:with|w/|incl|includes|including|comes with)\b|\+|&", before)


def item_typical(query: str, index, market, ebay_cfg: Optional[dict] = None) -> Optional[float]:
    """Typical price of exactly what was searched for — "u47 clone" must use
    clone prices, not a real U47's (which made every clone look like a
    too-cheap accessory). Reverb listings matching every word of the search
    come first; local history for the bare model only if the search is just
    that model."""
    from scrapers.enrich import model_key, model_query
    try:
        from scrapers.market import market_typical
        typical, _, _, _ = market_typical(query, ebay_cfg)
        if typical:
            return typical
    except Exception:
        pass
    key = model_key(query)
    only_model = normalize(model_query(query) or "") == normalize(query)
    if key and only_model:
        return (index or {}).get(key) or (market or {}).get(key)
    return None


def _candidates(listings, index, market, typical: Optional[float] = None,
                query: str = "") -> list[dict]:
    """Priced, currently-listed matches — no parts, accessories, rentals,
    'wanted' posts or placeholder prices, no hidden or duplicate rows — one
    per link."""
    from scrapers.enrich import basis_note, model_key, price_basis, value_key

    out, seen_urls = [], set()
    with _conn() as conn:
        for l in listings:
            if not l.url or l.url in seen_urls:
                continue
            title = l.title or ""
            value = parse_price(l.price)
            from scrapers.enrich import is_not_audio, item_form
            # Pedal/plugin versions only when the search asks for them.
            if item_form(title) in ("pedal", "plugin") and item_form(title) != item_form(query):
                continue
            if (not value or value < MIN_PRICE or _WANTED.match(title) or _RENTAL.search(title) or is_not_audio(title)
                    or _FOR_ITEM.search(title) or is_partial(title)):
                continue
            # An auction's current bid isn't what it will sell for.
            if (l.description or "").startswith("Auction"):
                continue
            repair = needs_repair(l.title, l.description)
            # "Pair of X — $900" competes as $450 per piece; "$450 each"
            # stays $450.
            basis, qty, unit = price_basis(title, l.description, value, typical)
            value = unit
            if typical:
                if value < typical * (REPAIR_FLOOR if repair else WORKING_FLOOR) or _is_accessory(title):
                    continue
            else:
                own = (index or {}).get(model_key(title)) or (market or {}).get(value_key(title))
                if (own and value < own * MIN_FRACTION_OF_TYPICAL) or _is_accessory(title):
                    continue
            row = conn.execute(
                "SELECT hidden, duplicate, sold, pending FROM seen WHERE url = ? ORDER BY sold DESC LIMIT 1", (l.url,)
            ).fetchone()
            if row and (row[0] or row[2] or row[3]) or getattr(l, "pending", False):
                continue
            seen_urls.add(l.url)
            out.append({
                "title": l.title, "price": l.price, "value": value, "url": l.url,
                "source_name": l.source_name, "image_url": l.image_url,
                "location": getattr(l, "location", None),
                "needs_repair": repair,
                "note": basis_note(basis, qty, unit),
                "is_clone": bool(_CLONE.search(title)) and not _CLONE.search(query),
                "global_id": l.global_id,
            })
    return sorted(out, key=lambda c: c["value"])


def check_lowest(query: str, cfg: dict, on_progress=None) -> dict:
    """Runs the search, updates the stored top 10 and returns
    {'top': [...], 'alert': candidate-or-None, 'previous_lowest': float|None}.
    An alert is only produced once the item has been checked before, and
    only for a listing this item hasn't already seen."""
    from scrapers.enrich import build_price_index
    from scrapers.live_search import run_live_search
    from scrapers.market import load_market
    from scrapers.store import all_priced_rows

    q = normalize(query)
    results = run_live_search(q, cfg, on_progress=on_progress)
    listings = [l for r in results for l in r.listings]
    # eBay by lowest price first (the regular eBay source reads the newest
    # listings, which can miss the cheapest). Needs the free eBay API keys.
    api_cfg = cfg.get("ebay_api") or {}
    if api_cfg.get("client_id") and api_cfg.get("client_secret"):
        try:
            from scrapers.ebay_api import ebay_lowest
            listings += ebay_lowest(q, api_cfg, name="eBay (lowest price)")
        except Exception as e:
            logger.warning("eBay lowest-price search failed for %r: %s", q, e)
    index, market = build_price_index(all_priced_rows()), load_market()
    typical = item_typical(q, index, market, api_cfg)
    cands = _candidates(listings, index, market, typical, q)
    top = cands[:TOP_N]

    alert = None
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _ensure_tables(conn)
        prev = conn.execute(
            "SELECT lowest, seeded FROM lowest_state WHERE query = ?", (q,)
        ).fetchone()
        previous_lowest = prev[0] if prev else None
        seeded = bool(prev and prev[1])
        if top and seeded and previous_lowest is not None and top[0]["value"] < previous_lowest:
            # New listing under the lowest, or an existing one whose price
            # dropped under it — but never the same listing at the same price twice.
            seen = conn.execute(
                "SELECT price FROM lowest_seen WHERE query = ? AND url = ?", (q, top[0]["url"])
            ).fetchone()
            if not seen or seen[0] is None or top[0]["value"] < seen[0]:
                alert = top[0]
        conn.executemany(
            "INSERT OR REPLACE INTO lowest_seen (query, url, price) VALUES (?, ?, ?)",
            [(q, c["url"], c["value"]) for c in cands],
        )
        if top:
            conn.execute(
                "INSERT OR REPLACE INTO lowest_state (query, lowest, top_json, checked_at, seeded)"
                " VALUES (?,?,?,?,1)",
                (q, top[0]["value"], json.dumps(top), now),
            )
        elif not prev:
            conn.execute(
                "INSERT INTO lowest_state (query, lowest, top_json, checked_at, seeded) VALUES (?,?,?,?,1)",
                (q, None, "[]", now),
            )
        # A check that found nothing (site timeouts, Facebook hiccup) keeps
        # the previous lowest and top 10 rather than wiping them — otherwise
        # the next real drop wouldn't have a lowest to be compared against.
        conn.commit()
    return {"top": top, "alert": alert, "previous_lowest": previous_lowest,
            "checked_at": now, "typical": typical}


def get_state(query: str) -> Optional[dict]:
    with _conn() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT lowest, top_json, checked_at FROM lowest_state WHERE query = ?", (normalize(query),)
        ).fetchone()
    if not row:
        return None
    return {"lowest": row[0], "top": json.loads(row[1] or "[]"), "checked_at": row[2]}


def forget(query: str):
    with _conn() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM lowest_state WHERE query = ?", (normalize(query),))
        conn.execute("DELETE FROM lowest_seen WHERE query = ?", (normalize(query),))
        conn.commit()


def notify_new_lowest(cfg: dict, query: str, alert: dict, previous_lowest: Optional[float]):
    from scrapers.notify import push_enabled, send_push

    was = f"${previous_lowest:,.0f}" if previous_lowest is not None else "?"
    repair = (" (clone)" if alert.get("is_clone") else "") + (" (needs repair)" if alert["needs_repair"] else "")
    if push_enabled(cfg):
        send_push(
            cfg, f"New lowest price: {query}",
            f"{alert['title'][:90]}{repair}\n{alert['price']} (was lowest {was}) · {alert['source_name']}",
            url=alert["url"], tags=["moneybag"],
        )
    email_cfg = cfg.get("email") or {}
    if email_cfg.get("from") and email_cfg.get("password"):
        from scrapers.emailer import send_html_email
        dashboard_url = email_cfg.get("dashboard_url", "http://localhost:8420").rstrip("/")
        img = (f'<img src="{alert["image_url"]}" width="96" height="96" '
               f'style="width:96px;height:96px;object-fit:cover;border-radius:8px;">') if alert.get("image_url") else ""
        html = f"""<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
          <div style="max-width:620px;margin:0 auto;padding:20px;">
            <div style="background:#1e3a5f;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">💰 New lowest price — {query}</div>
            <div style="background:#fff;padding:18px 22px;border-radius:0 0 12px 12px;display:flex;gap:14px;">
              {img}
              <div><a href="{alert['url']}" style="color:#1e3a5f;font-weight:700;font-size:16px;text-decoration:none;">{alert['title']}</a>
                <div style="margin-top:6px;"><span style="color:#166534;font-weight:700;font-size:18px;">{alert['price']}</span>
                <span style="color:#94a3b8;">&nbsp;previous lowest {was}</span></div>
                <div style="color:#64748b;font-size:13px;margin-top:4px;">{alert['source_name']}{' · ' + alert['location'] if alert.get('location') else ''}{repair}</div>
                <p style="margin-top:12px;font-size:13px;"><a href="{dashboard_url}/lowest?q={quote_plus(query)}">See the 10 lowest for “{query}” →</a></p>
              </div>
            </div>
          </div></body></html>"""
        send_html_email(html, f"Gear Scout — new lowest price for “{query}”: {alert['price']}", email_cfg)


def run_trackers(cfg: dict) -> int:
    """Checks every tracked item; returns how many alerts went out."""
    alerts = 0
    for q in tracked_queries(cfg):
        try:
            res = check_lowest(q, cfg)
        except Exception:
            logger.exception("Lowest-price check failed for %r", q)
            continue
        if res["alert"]:
            notify_new_lowest(cfg, q, res["alert"], res["previous_lowest"])
            alerts += 1
        logger.info("Lowest price %r: %s", q,
                    res["top"][0]["price"] if res["top"] else "no priced listings")
    return alerts
