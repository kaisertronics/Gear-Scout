"""
Finds listings that have sold or been taken down, so the dashboard stops
showing them as available. Runs with every hourly refresh on a capped batch:
favorites first, then deals and the newest listings, skipping anything
checked in the last 12 hours.

How each site says "gone" (all checked live):
  Reverb           public API: state "sold" / "ended"
  Shopify stores   <product url>.js: "available": false (Alto Music, Rudy's…)
  Kijiji           "This ad is no longer available"
  Vintage King     product data: availability OutOfStock
  Forums           thread title marked SOLD (GroupDIY, The Gear Page…)
  Craigslist, OfferUp, Long & McQuade, everything else
                   page removed (404/410) or a "deleted / no longer available" notice
  Facebook         item page says sold / no longer available, or "Pending"
                   (Facebook hides pending items from searches and feeds, so
                   only the item page shows it; 60 per run, pending ones
                   re-checked every 3 hours)
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from scrapers.store import _conn, mark_sold

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
RECHECK_HOURS = 12
_GONE_TEXT = re.compile(
    r"this ad is no longer available|this posting has been deleted|this posting has expired|"
    r"this posting has been flagged for removal|listing (?:is no longer|isn'?t) available|"
    r"this listing (?:has ended|sold|is no longer available)|that listing sold|"
    r"this item is no longer available", re.I)
_SOLD_TITLE = re.compile(r"<title>[^<]*\b(?:sold|gone|pending)\b[^<]*</title>", re.I)


def _ensure(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(seen)")}
    if "sold_checked_at" not in cols:
        conn.execute("ALTER TABLE seen ADD COLUMN sold_checked_at TEXT")


def check_url(url: str) -> Optional[bool]:
    """True = sold/gone, False = still listed, None = couldn't tell."""
    if "reverb.com/item/" in url:
        m = re.search(r"/item/(\d+)", url)
        if not m:
            return None
        r = requests.get(f"https://api.reverb.com/api/listings/{m.group(1)}",
                         headers={"Accept-Version": "3.0", "Accept": "application/hal+json"}, timeout=20)
        if r.status_code == 404:
            return True
        slug = ((r.json().get("state") or {}).get("slug") or "").lower()
        return None if not slug else slug != "live"
    if "/products/" in url and "facebook.com" not in url:
        # Shopify product: its .js twin says whether any variant is for sale.
        r = requests.get(url.split("?")[0].rstrip("/") + ".js", headers=HEADERS, timeout=20)
        if r.status_code in (404, 410):
            return True
        if r.ok and r.headers.get("content-type", "").startswith(("application/json", "text/javascript")):
            try:
                return not r.json().get("available", True)
            except ValueError:
                pass
    r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    if r.status_code in (404, 410):
        return True
    if not r.ok:
        return None
    html = r.text
    if "kijiji.ca" in url:
        # Every Kijiji page carries "no longer available" text somewhere; the
        # ad's own status field is what counts.
        m = re.search(r'"(?:adState|status)"\s*:\s*"([A-Z_]+)"', html)
        return None if not m else m.group(1) != "ACTIVE"
    if "vintageking.com" in url:
        return bool(re.search(r'"availability"\s*:\s*"https?://schema\.org/(?:OutOfStock|SoldOut|Discontinued)"', html))
    if re.search(r"groupdiy\.com|thegearpage\.net|audiokarma\.org|gearspace\.com", url):
        return bool(_SOLD_TITLE.search(html[:5000]))
    if "long-mcquade.com/GearHunter/" in url:
        m = re.search(r"/GearHunter/(\d+)", url)
        return bool(m and m.group(1) not in r.url)  # redirected away from the item
    return bool(_GONE_TEXT.search(html))


def _candidates(limit: int, include_fb: bool) -> list[tuple[str, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    recheck = (datetime.now(timezone.utc) - timedelta(hours=RECHECK_HOURS)).isoformat()
    # Pending items change fast (sold, or back on the market): every 3 hours.
    pending_recheck = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    fb_clause = "" if include_fb else "AND url NOT LIKE '%facebook.com%'"
    with _conn() as conn:
        _ensure(conn)
        rows = conn.execute(
            f"""SELECT url, MAX(favorite) AS fav, MAX(first_seen) AS seen FROM seen
                WHERE url LIKE 'http%' AND sold = 0 AND hidden = 0 AND duplicate = 0
                AND (first_seen >= ? OR favorite = 1)
                AND (sold_checked_at IS NULL OR sold_checked_at < ?
                     OR (COALESCE(pending, 0) = 1 AND sold_checked_at < ?))
                AND url NOT LIKE '%shopgoodwill.com%' AND url NOT LIKE '%search/posts%'
                {fb_clause}
                GROUP BY url
                ORDER BY fav DESC, MAX(COALESCE(pending, 0)) DESC,
                         MAX(sold_checked_at) IS NULL DESC, MAX(sold_checked_at) ASC, seen DESC
                LIMIT ?""",
            (cutoff, recheck, pending_recheck, limit)).fetchall()
    return [(u, "fav" if f else "") for u, f, _ in rows]


def _record(url: str, gone: Optional[bool]) -> None:
    with _conn() as conn:
        _ensure(conn)
        conn.execute("UPDATE seen SET sold_checked_at = ? WHERE url = ?",
                     (datetime.now(timezone.utc).isoformat(), url))
        conn.commit()
    if gone:
        mark_sold(url)


def run_sold_check(max_http: int = 120, max_fb: int = 60) -> dict:
    start = time.time()
    checked = sold = 0
    http = [u for u, _ in _candidates(max_http * 2, include_fb=False)][:max_http]
    for url in http:
        try:
            gone = check_url(url)
        except Exception as e:
            logger.debug("Sold check failed for %s: %s", url, e)
            gone = None
        _record(url, gone)
        checked += 1
        sold += bool(gone)
        time.sleep(0.4)

    fb = [u for u, _ in _candidates(max_fb * 4, include_fb=True) if "facebook.com/marketplace/item" in u][:max_fb]
    if fb:
        try:
            from scrapers.facebook_scraper import SESSION_FILE, _browser, _new_context
            if SESSION_FILE.exists():
                with _browser() as browser:
                    page = _new_context(browser).new_page()
                    for url in fb:
                        gone = None
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=40000)
                            page.wait_for_timeout(2000)
                            text = page.inner_text("body")[:5000]
                            gone = bool(_GONE_TEXT.search(text) or re.search(r"^\s*Sold\s*$", text, re.M)
                                        or "/marketplace/item/" not in page.url)
                            if not gone:
                                from scrapers.store import set_pending
                                set_pending(url, bool(re.search(r"(?mi)^\s*pending\s*$|sale pending", text)))
                        except Exception as e:
                            logger.debug("FB sold check failed for %s: %s", url, e)
                        _record(url, gone)
                        checked += 1
                        sold += bool(gone)
                        time.sleep(1.5)
        except Exception:
            logger.exception("Facebook sold check skipped")
    logger.info("Sold check: %d listings checked, %d sold or removed, %.0fs", checked, sold, time.time() - start)
    return {"checked": checked, "sold": sold}
