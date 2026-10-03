"""
Auctions about to end with the current bid far under what the gear sells for.

- Every hour, eBay's pro-audio auctions ending soonest are fetched (2 API
  calls) and stored like other listings (description "Auction · current bid
  (N bids) · ends YYYY-MM-DD HH:MM", Pacific time — same as ShopGoodwill).
- During the day: a push + email about an hour before a good one ends.
- At 9:30 PM: one message listing tonight's overnight auctions (ending
  between 11 PM and 7 AM) — fewer people are awake to bid, so they often
  close cheap. Set your max bid before bed.

Bidding is always left to you.
"""
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
DAY_ALERT_PCT = 40      # current bid at least 40% under the comp
NIGHT_LIST_PCT = 30     # overnight list: at least 30% under
_ENDS = re.compile(r"ends (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")


def ebay_auction_listing(item: dict, name: str):
    """A Listing for an eBay auction-only item (None for Buy It Now items):
    the price is the current bid and the description says when it ends."""
    from scrapers.base import Listing, truncate
    options = item.get("buyingOptions") or []
    if "AUCTION" not in options or "FIXED_PRICE" in options:
        return None
    bid = item.get("currentBidPrice") or item.get("price") or {}
    try:
        value = float(bid.get("value"))
    except (TypeError, ValueError):
        return None
    ends = ""
    if item.get("itemEndDate"):
        try:
            ends = datetime.fromisoformat(item["itemEndDate"].replace("Z", "+00:00")) \
                .astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
    bids = item.get("bidCount") or 0
    item_id = item.get("legacyItemId") or re.sub(r"\W", "", item.get("itemId", ""))
    loc = item.get("itemLocation") or {}
    return Listing(
        source_name=name, title=truncate(item.get("title") or "", 120),
        url=item.get("itemWebUrl") or f"https://www.ebay.com/itm/{item_id}",
        price=f"${value:,.2f}",
        description=f"Auction · current bid ({bids} bid{'s' if bids != 1 else ''}) · ends {ends}"
                    + (f" · {item.get('condition')}" if item.get("condition") else ""),
        image_url=(item.get("image") or {}).get("imageUrl"), listing_id=item_id,
        location=", ".join(p for p in (loc.get("city"), loc.get("stateOrProvince")) if p) or None,
    )


def fetch_ebay_ending(cfg: dict) -> int:
    """Stores eBay pro-audio auctions ending soonest (keyword-matched).
    Bids change, so stored ones get their current bid updated."""
    import requests
    from scrapers.base import keyword_match
    from scrapers.ebay_api import (CONDITION_FILTER, PRO_AUDIO_CATEGORY, SEARCH_URL, _check_response,
                                   _get_token, ebay_paused_until)
    from scrapers.learning import keywords_with_learned
    from scrapers.store import _conn, mark_seen
    api = cfg.get("ebay_api") or {}
    if not (api.get("client_id") and api.get("client_secret")) or ebay_paused_until():
        return 0
    keywords = list(keywords_with_learned(cfg))
    token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
    stored = 0
    for offset in (0, 200):
        resp = requests.get(SEARCH_URL, params={
            "category_ids": PRO_AUDIO_CATEGORY, "sort": "endingSoonest", "limit": "200", "offset": str(offset),
            "filter": f"{CONDITION_FILTER},buyingOptions:{{AUCTION}},priceCurrency:USD"},
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=30)
        _check_response(resp)
        if resp.status_code != 200:
            logger.warning("eBay ending-soon auctions failed: HTTP %s", resp.status_code)
            break
        for item in resp.json().get("itemSummaries", []) or []:
            listing = ebay_auction_listing(item, "eBay — Auctions")
            if not listing or not keyword_match(listing.title, keywords):
                continue
            mark_seen(listing)
            with _conn() as conn:
                conn.execute("UPDATE seen SET price = ?, description = ? WHERE url = ?",
                             (listing.price, listing.description, listing.url))
                conn.commit()
            stored += 1
    return stored


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS auction_alerted (url TEXT, kind TEXT, at TEXT, PRIMARY KEY (url, kind))")


def _candidates(cfg: dict, start: datetime, end: datetime, min_pct: int) -> list[dict]:
    """Stored auctions (still open) ending between start and end (Pacific)
    with the current bid at least min_pct under a trusted comp."""
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, is_relevant,
                                 item_form, needs_repair)
    from scrapers.learning import keywords_with_learned
    terms = tuple(keywords_with_learned(cfg))
    exclude_words = cfg.get("exclude_words") or []
    from scrapers.market import load_market
    from scrapers.store import _conn
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE description LIKE 'Auction%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0")]
    market, index, similar = load_market(), price_index(), similar_index()
    out, seen = [], set()
    for r in rows:
        m = _ENDS.search(r.get("description") or "")
        if not m or r["url"] in seen:
            continue
        ends = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=PACIFIC)
        if not (start <= ends <= end):
            continue
        title = r.get("title") or ""
        if (exclude_match(title, exclude_words) or not is_relevant(title, r.get("price"), terms)
                or is_not_audio(title) or is_accessory_only(title) or is_partial(title) or item_form(title) == "pedal"
                or needs_repair(title, r.get("description"))):
            continue
        c = evaluate(title, r.get("price"), None, r["url"], market, index, similar)
        if not c or c["est"] or c["ref"] < 100 or c["pct"] < min_pct or c["pct"] > 95:
            continue
        seen.add(r["url"])
        r["comp"], r["ends"] = c, ends
        out.append(r)
    return sorted(out, key=lambda r: r["ends"])


def _not_yet(urls: list[str], kind: str) -> set[str]:
    from scrapers.store import _conn
    with _conn() as conn:
        _ensure(conn)
        done = {u for (u,) in conn.execute("SELECT url FROM auction_alerted WHERE kind = ?", (kind,))}
    return {u for u in urls if u not in done}


def _mark(urls, kind: str):
    from scrapers.store import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _ensure(conn)
        conn.executemany("INSERT OR IGNORE INTO auction_alerted VALUES (?, ?, ?)", [(u, kind, now) for u in urls])
        conn.commit()


def _line(r) -> str:
    c = r["comp"]
    return (f"{r['title'][:70]} — bid {r['price']} (comp ~${c['ref']:,.0f}, {c['pct']}% under), "
            f"ends {r['ends'].strftime('%-I:%M %p').lower()}")


def _send(cfg: dict, title: str, rows: list[dict], intro: str):
    from scrapers.emailer import send_html_email
    from scrapers.notify import push_enabled, send_push
    email_cfg = cfg.get("email") or {}
    dash = email_cfg.get("dashboard_url", "http://localhost:8420")
    if push_enabled(cfg):
        send_push(cfg, title, "\n".join("• " + _line(r) for r in rows[:8]), url=rows[0]["url"] if len(rows) == 1 else dash,
                  tags=["hammer"])
    if email_cfg.get("from") and email_cfg.get("password"):
        items = "".join(
            f'<div style="border-top:1px solid #e2e8f0;padding:10px 0;">'
            f'<a href="{escape(r["url"])}" style="color:#1e3a5f;font-weight:700;text-decoration:none;">{escape(r["title"])}</a><br>'
            f'<span style="color:#166534;font-weight:700;">bid {escape(r["price"])}</span>'
            f'<span style="color:#64748b;font-size:13px;"> · {escape(r["comp"]["label"])} ~${r["comp"]["ref"]:,.0f} · '
            f'{r["comp"]["pct"]}% under · ends {r["ends"].strftime("%a %-I:%M %p")} · '
            f'{escape((r.get("source_name") or "").split(" — ")[0])}</span></div>' for r in rows)
        html = (f'<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,sans-serif;">'
                f'<div style="max-width:640px;margin:0 auto;padding:20px;"><div style="background:#1e3a5f;color:#fff;'
                f'padding:16px 20px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">🔨 {escape(title)}</div>'
                f'<div style="background:#fff;padding:16px 20px;border-radius:0 0 12px 12px;color:#1e293b;">'
                f'<p>{escape(intro)}</p>{items}</div></div></body></html>')
        send_html_email(html, f"Gear Scout — {title}", email_cfg)


def check(cfg: dict) -> int:
    """Hourly: daytime heads-up for good auctions ending in the next ~1-2
    hours; at the 9 PM hour, tonight's overnight list."""
    now = datetime.now(PACIFIC)
    sent = 0
    if 7 <= now.hour < 23:
        rows = _candidates(cfg, now + timedelta(minutes=30), now + timedelta(minutes=120), DAY_ALERT_PCT)
        rows = [r for r in rows if r["url"] in _not_yet([r["url"] for r in rows], "soon")
                and not 23 <= r["ends"].hour and r["ends"].hour >= 7]
        if rows:
            _send(cfg, f"{len(rows)} auction{'s' if len(rows) != 1 else ''} ending soon, bid well under value", rows,
                  "Current bids at least 40% under what the gear sells for — set a max bid if you want one.")
            _mark([r["url"] for r in rows], "soon")
            sent += len(rows)
    if now.hour == 21:
        start = now.replace(hour=23, minute=0, second=0, microsecond=0)
        rows = _candidates(cfg, start, start + timedelta(hours=8), NIGHT_LIST_PCT)
        rows = [r for r in rows if r["url"] in _not_yet([r["url"] for r in rows], "night")]
        if rows:
            _send(cfg, f"Tonight's overnight auctions ({len(rows)})", rows,
                  "These end while most people are asleep (11 PM–7 AM), so they often close cheap. "
                  "The bids shown are as of this evening — set your max bid before bed.")
            _mark([r["url"] for r in rows], "night")
            sent += len(rows)
    return sent
