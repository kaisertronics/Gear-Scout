"""Pre-sold price drops (owner proposal 4a767354). The price-cut sniper reacts to ONE
seller dropping an ad. This watches a whole model: when the asking prices of its
newest ads (last RECENT_DAYS days) run well under the earlier ones, from several
different sellers, a buying window is opening: the market is softening before any
single ad shows a cut. Home lists the cheapest live ad of each such model.

  window  — pure: one model's (date, price, seller) rows -> the numbers, or None
  pick    — pure: rows -> {model key: window}
  find    — the Home list (ONE windowed read; the dashboard caches it for 30 minutes
            and pre-warms it, so a page load never runs the scan)
Read-only."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

RECENT_DAYS = 14
BASE_DAYS = 75
MIN_RECENT = 5          # asks in the recent window
MIN_SELLERS = 3         # different sellers among them
MIN_BASE = 8            # asks in the earlier window
MIN_DROP_PCT = 10
MAX_DROP_PCT = 35       # further than this is a different product mix, not a softening market
MIN_BASE_PRICE = 150
MIN_BELOW = 3           # recent ads individually under the earlier median (not one outlier pulling the middle)
LIMIT = 6


def window(items: list, now: datetime) -> Optional[dict]:
    """items = [(when, price, seller_key)] for ONE model. None unless prices fell broadly."""
    recent_cut = now - timedelta(days=RECENT_DAYS)
    recent = [(p, s) for w, p, s in items if w >= recent_cut]
    base = [p for w, p, _s in items if w < recent_cut]
    if len(recent) < MIN_RECENT or len(base) < MIN_BASE or len({s for _p, s in recent}) < MIN_SELLERS:
        return None
    b, r = median(base), median(p for p, _s in recent)
    if b < MIN_BASE_PRICE:
        return None
    drop = round((1 - r / b) * 100)
    if not (MIN_DROP_PCT <= drop <= MAX_DROP_PCT) or sum(1 for p, _s in recent if p < b) < MIN_BELOW:
        return None
    return {"base": round(b), "now": round(r), "drop": drop, "n_recent": len(recent), "n_base": len(base),
            "sellers": len({s for _p, s in recent})}


def label(key: str) -> str:
    brand, _, model = key.partition(":")
    return f"{brand.title()} {model.upper()}" if model else key.upper()


def pick(rows: list, now: datetime, key_of, ok=lambda r: True) -> list:
    """Pure. rows = priced ads (title, price, url, first_seen, seller, source_name, sold ...).
    Returns the cheapest live ad per falling model, biggest drop first."""
    from scrapers.enrich import parse_price
    from scrapers.finder_rows import total_cost
    from scrapers.instant_offer import suggest
    per: dict = {}
    seen = set()
    live_cut = (now - timedelta(days=RECENT_DAYS)).isoformat()
    for r in rows:
        if "C$" in (r.get("price") or "") or (r.get("description") or "").startswith("Auction") or not ok(r):
            continue
        p = parse_price(r.get("price"))
        if not p or p < MIN_BASE_PRICE / 2:
            continue
        ad = (re.sub(r"\s+", " ", (r.get("title") or "").lower()), round(p))
        if ad in seen:
            continue
        seen.add(ad)
        k = key_of(r.get("title") or "")
        try:
            when = datetime.fromisoformat((r.get("first_seen") or "").replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if k:
            seller = (r.get("source_name") or "") + "|" + (r.get("seller") or r.get("url") or "")
            per.setdefault(k, []).append((when, p, seller, r))
    out = []
    for k, items in per.items():
        w = window([(a, b, c) for a, b, c, _r in items], now)
        if not w:
            continue
        live = [(total_cost(r) or p, r) for a, p, _s, r in items
                if (r.get("first_seen") or "") >= live_cut and not r.get("sold") and not r.get("pending")]
        if not live:
            continue
        cost, best = min(live, key=lambda x: x[0])
        ask = suggest(cost) or cost
        out.append(dict(best, window=dict(w, key=k, label=label(k)), finder_note=(
            f"Price window: {label(k)} asks fell {w['drop']}% (median ${w['now']:,} over the last {RECENT_DAYS} days vs "
            f"${w['base']:,} before; {w['sellers']} sellers, {w['n_recent']} ads). This is the cheapest live one."),
            finder_msg=(f"Hi! Is the {re.sub(chr(32) + '+', ' ', best.get('title') or 'it')[:70]} still available? I can pay "
                        f"${int(ask):,} shipped to Seattle and pay right away. Thanks!")))
    out.sort(key=lambda r: -r["window"]["drop"])
    return out[:LIMIT]


def find(cfg: dict, now: Optional[datetime] = None) -> list:
    from scrapers import finder_rows as FR, market_value as mv
    from scrapers.enrich import exclude_match, is_accessory_only, is_not_audio, is_partial, needs_repair, title_says_sold
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=BASE_DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT title, price, url, first_seen, seller, source_name, sold, pending, description, shipping, image_url, location, country,"
            " global_id, posted_at, offers_ok FROM seen WHERE first_seen >= ? AND hidden = 0 AND duplicate = 0"
            " AND price IS NOT NULL AND price != '' AND price NOT LIKE 'C%' AND (country IS NULL OR country IN ('US', 'CA'))", (since,))]
    ex = cfg.get("exclude_words") or []

    def ok(r):
        t = r.get("title") or ""
        return bool(FR.BUYLIST.search(t)) and not (mv.skip_listing(t) or is_partial(t) or is_accessory_only(t) or is_not_audio(t)
                                                   or title_says_sold(t) or exclude_match(t, ex)
                                                   or needs_repair(t, r.get("description")))
    return pick(rows, now, mv._key, ok)
