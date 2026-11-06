"""Quiet-auction finder: mic auctions ending within a few hours that have
known zero bids and a current price 40%+ under used value. They often close
cheap. Read-only: uses the stored eBay mic auctions ("Auction · current bid
(N bids) · ends ..." in the description) and scrapers.market_value. Reverb
auctions aren't stored with bid counts, so only eBay shows up here."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

HOURS = 6
MIN_UNDER_PCT = 40
LIMIT = 8


def bid_count(desc: Optional[str]) -> Optional[int]:
    """Bids from the stored description, None when unknown."""
    m = re.search(r"\((\d+) bids?\)", desc or "")
    return int(m.group(1)) if m else None


def pick(rows: list[dict], used_of, now: Optional[datetime] = None) -> list[dict]:
    """Pure selection. rows need title, price, description (+ optional
    shipping); used_of(title) gives the used value. Soonest ending first."""
    from scrapers.enrich import parse_price
    from scrapers.mic_focus import _auction_end
    now = now or datetime.now(timezone.utc)
    out, seen = [], set()
    for r in rows:
        desc = r.get("description") or ""
        end = _auction_end(desc)
        if not end or not (now < end <= now + timedelta(hours=HOURS)) or bid_count(desc) != 0:
            continue
        if r.get("url") in seen:
            continue
        bid = parse_price(r.get("price")) or 0
        if bid <= 0:
            continue
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        total = bid + (ship or 0)
        used = used_of(r.get("title") or "")
        if not used or total > used * (1 - MIN_UNDER_PCT / 100):
            continue
        seen.add(r.get("url"))
        r = dict(r)
        r["quiet"] = {"ends": end.isoformat(), "used": round(used), "total": round(total, 2),
                      "pct": round((1 - total / used) * 100)}
        out.append(r)
    out.sort(key=lambda r: r["quiet"]["ends"])
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list[dict]:
    """Up to 8 quiet mic auctions, soonest ending first."""
    from scrapers import mic_focus
    from scrapers.enrich import is_partial, needs_repair
    from scrapers.market_value import value
    from scrapers.store import _conn
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE description LIKE 'Auction%' AND url LIKE '%ebay.com%' AND hidden = 0"
            " AND duplicate = 0 AND COALESCE(sold, 0) = 0 AND (country IS NULL OR country = 'US')")]
    rows = [r for r in rows if mic_focus.classify(r.get("title") or "", targets)
            and not is_partial(r["title"]) and not needs_repair(r["title"], r.get("description"))]
    return pick(rows, lambda t: value(t)[0], now)
