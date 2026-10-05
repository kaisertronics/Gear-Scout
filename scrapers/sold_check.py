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


def _ebay_gone(url: str) -> Optional[bool]:
    """eBay through its official API (eBay blocks reading its pages)."""
    import yaml
    from scrapers.ebay_api import SEARCH_URL, _check_response, _get_token, ebay_paused_until
    m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", url)
    if not m or ebay_paused_until():
        return None
    try:
        api = (yaml.safe_load(open("/config/config.yaml")) or {}).get("ebay_api") or {}
    except Exception:
        return None
    if not (api.get("client_id") and api.get("client_secret")):
        return None
    token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
    r = requests.get(SEARCH_URL.replace("item_summary/search", "item/get_item_by_legacy_id"),
                     params={"legacy_item_id": m.group(1)},
                     headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=20)
    _check_response(r)
    if r.status_code in (404, 410):
        return True
    if r.status_code != 200:
        return None
    item = r.json()
    end = item.get("itemEndDate")
    if end:
        try:
            if datetime.fromisoformat(end.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return True
        except ValueError:
            pass
    avail = [(a.get("estimatedAvailabilityStatus") or "") for a in (item.get("estimatedAvailabilities") or [])]
    return bool(avail) and all(a == "OUT_OF_STOCK" for a in avail)


def check_url(url: str) -> Optional[bool]:
    """True = sold/gone, False = still listed, None = couldn't tell."""
    if "ebay.com/itm" in url:
        return _ebay_gone(url)
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
    if "craigslist.org" in url:
        m = re.search(r'class="date timeago"[^>]*datetime="([^"]+)"', html) or re.search(r'datetime="(\d{4}-\d\d-\d\dT[^"]+)"', html)
        if m:
            try:
                from scrapers.store import set_posted_at
                set_posted_at(url, datetime.fromisoformat(re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", m.group(1))))
            except ValueError:
                pass
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


SHOWN_FILES = ("/data/shown_dashboard.txt", "/data/shown_steals.txt", "/data/shown_telex.txt",
               "/data/shown_telex_groups.txt")
SHOWN_RECHECK_HOURS = 4


def _shown_first(limit: int, include_fb: bool) -> list[str]:
    """Listings currently on your Dashboard / Telex (the dashboard notes
    them) that haven't been checked in the last few hours — checked first,
    so what you see doesn't linger after it sells."""
    shown = []
    for path in SHOWN_FILES:
        try:
            shown += [u for u in open(path).read().split("\n") if u.startswith("http")]
        except OSError:
            pass
    if not include_fb:
        shown = [u for u in shown if "facebook.com" not in u]
    else:
        shown = [u for u in shown if "facebook.com/marketplace/item" in u]
    if not shown:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=SHOWN_RECHECK_HOURS)).isoformat()
    due = []
    with _conn() as conn:
        _ensure(conn)
        for i in range(0, len(shown), 400):
            chunk = shown[i:i + 400]
            due += conn.execute(
                f"SELECT url, MAX(COALESCE(sold_checked_at, '')) FROM seen WHERE url IN ({','.join('?' * len(chunk))})"
                " AND sold = 0 AND url NOT LIKE '%shopgoodwill.com%'"
                " AND (sold_checked_at IS NULL OR sold_checked_at < ?) GROUP BY url", (*chunk, cutoff)).fetchall()
    # Never-checked first, then the longest since a check — so nothing far
    # down the page waits forever behind the top listings.
    order = {u: i for i, u in enumerate(shown)}
    return [u for u, _ in sorted(set(due), key=lambda r: (r[1], order.get(r[0], 0)))][:limit]


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


def fb_listed_at(text: str):
    """When a Facebook Marketplace ad was posted, from "Listed 3 weeks ago"
    on its page (approximate, which is fine for "how fresh is this")."""
    from datetime import timedelta
    m = re.search(r"Listed\s+(?:about\s+|over\s+)?(an?|\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", text, re.I)
    if not m:
        return None
    n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
    days = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2).lower()]
    return datetime.now(timezone.utc) - timedelta(days=n * days)


def check_fb(urls: list[str]):
    """Opens each Facebook listing in one paced browser and yields
    (url, gone) — gone is None when the page couldn't be read. Also notes
    "pending" sales."""
    if not urls:
        return
    try:
        from scrapers.facebook_scraper import SESSION_FILE, _browser, _new_context
        if not SESSION_FILE.exists():
            return
        with _browser() as browser:
            page = _new_context(browser).new_page()
            for url in urls:
                gone = None
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                    # Wait only until the item (its title) or a "gone" notice shows,
                    # instead of a fixed 2 seconds.
                    try:
                        page.wait_for_function(
                            "() => document.querySelector('h1') || /no longer available|isn't available|this listing/i"
                            ".test(document.body.innerText.slice(0, 3000))", timeout=3500, polling=200)
                    except Exception:
                        pass
                    page.wait_for_timeout(300)
                    text = page.inner_text("body")[:5000]
                    gone = bool(_GONE_TEXT.search(text) or re.search(r"^\s*Sold\s*$", text, re.M)
                                or "/marketplace/item/" not in page.url)
                    if not gone:
                        from scrapers.store import set_pending
                        set_pending(url, bool(re.search(r"(?mi)^\s*pending\s*$|sale pending", text)))
                        posted = fb_listed_at(text)
                        if posted:
                            from scrapers.store import set_posted_at
                            set_posted_at(url, posted)
                except Exception as e:
                    logger.debug("FB sold check failed for %s: %s", url, e)
                yield url, gone
                time.sleep(1.0)
    except Exception:
        logger.exception("Facebook sold check skipped")


def run_sold_check(max_http: int = 120, max_fb: int = 60) -> dict:
    """Website checks run 4 at a time while the Facebook checks (one
    browser, paced) run alongside them."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    start = time.time()
    checked = sold = 0
    lock = threading.Lock()
    shown = _shown_first(max_http, include_fb=False)
    http = list(dict.fromkeys(shown + [u for u, _ in _candidates(max_http * 2, include_fb=False)]))
    # eBay is checked through its API, which counts against the daily
    # allowance: at most 40 per hour, the ones you can see first.
    ebay = [u for u in http if "ebay.com/itm" in u][:40]
    http = [u for u in http if "ebay.com/itm" not in u][:max_http] + ebay

    def check_one(url):
        nonlocal checked, sold
        try:
            gone = check_url(url)
        except Exception as e:
            logger.debug("Sold check failed for %s: %s", url, e)
            gone = None
        _record(url, gone)
        with lock:
            checked += 1
            sold += bool(gone)

    def fb_checks():
        nonlocal checked, sold
        fb = list(dict.fromkeys(_shown_first(max_fb, include_fb=True)
                            + [u for u, _ in _candidates(max_fb * 4, include_fb=True)
                               if "facebook.com/marketplace/item" in u]))[:max_fb]
        for url, gone in check_fb(fb):
            _record(url, gone)
            checked += 1
            sold += bool(gone)

    fb_thread = threading.Thread(target=fb_checks, name="sold-check-fb", daemon=True)
    fb_thread.start()
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="sold-check") as pool:
        list(pool.map(check_one, http))
    fb_thread.join()
    logger.info("Sold check: %d listings checked, %d sold or removed, %.0fs", checked, sold, time.time() - start)
    return {"checked": checked, "sold": sold}
