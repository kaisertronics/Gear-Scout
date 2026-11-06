"""One-tap instant offer (owner proposal bea4ad31). A deal push/email links to
the dashboard's /offer?url=<listing url> page, which shows the ask, the used
value and a suggested offer, and sends it through the existing Reverb / eBay
offer helpers (scrapers/reverb_offer.py, ebay_offer.py; they log to
offer_log) or, for any other site, gives the drafted message to copy.

Suggestion: ~15% under the ask, never under 70% of the ask, rounded to $5."""
import re
import sqlite3
from typing import Optional
from urllib.parse import quote

DISCOUNT = 0.85
FLOOR = 0.70
SHIP_NOTE = "shipped to Seattle"


def suggest(ask) -> Optional[int]:
    """~15% under the ask, rounded to $5, never under 70% of the ask or over it."""
    try:
        ask = float(ask)
    except (TypeError, ValueError):
        return None
    if ask <= 0:
        return None
    amt = round(ask * DISCOUNT / 5) * 5
    floor = -(-ask * FLOOR // 5) * 5  # 70% of ask, rounded up to $5
    amt = max(amt, floor)
    return int(min(amt, ask)) if ask >= 5 else int(round(ask))


def min_offer(ask) -> int:
    return int(-(-float(ask) * FLOOR // 1))


def channel(url, offers_ok=None, ebay_connected=False) -> str:
    """"reverb" / "ebay" when the listing takes a Best Offer we can send, else "copy"."""
    u = str(url or "")
    if re.search(r"reverb\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?item/\d+", u, re.I) and offers_ok != 0:
        return "reverb"
    if "ebay.com/itm" in u and ebay_connected and offers_ok:
        return "ebay"
    return "copy"


def message(title, amount) -> str:
    t = re.sub(r"\s+", " ", title or "it")[:70]
    return (f"Hi! Is the {t} still available? I can pay ${int(amount):,} {SHIP_NOTE} and "
            f"pay right away. Thanks!")


def link(cfg: dict, url) -> str:
    """The /offer page link for a push/email ("" when no dashboard_url is set)."""
    dash = ((cfg.get("email") or {}).get("dashboard_url") or "").rstrip("/")
    return f"{dash}/offer?url={quote(str(url), safe='')}" if dash and url else ""


def push_actions(cfg: dict, url, amount=None) -> Optional[list]:
    """ntfy action button that opens the offer page."""
    href = link(cfg, url)
    if not href:
        return None
    return [{"action": "view", "label": f"Offer ${amount:,}" if amount else "Make offer", "url": href}]


def suggested_for(row: dict) -> Optional[int]:
    """Quick amount for a push: the ask on a stored row (price + known shipping excluded; the ask is the ad price)."""
    from scrapers.enrich import parse_price
    return suggest(parse_price(row.get("price")))


def lookup(url) -> Optional[dict]:
    """The listing for /offer: its stored row plus the used value from the Mic Focus snapshot."""
    from scrapers.enrich import parse_price
    from scrapers.store import _conn
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM seen WHERE url = ? ORDER BY first_seen DESC LIMIT 1", (url,)).fetchone()
    if not r:
        return None
    r = dict(r)
    r["ask"] = parse_price(r.get("price"))
    r["used"] = None
    try:
        from scrapers import mic_focus
        for s in mic_focus.load().get("rows") or []:
            if s.get("url") == url:
                m = s.get("mic") or {}
                r["used"] = m.get("used")
                break
    except Exception:
        pass
    return r
