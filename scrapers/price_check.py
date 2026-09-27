"""
Re-checks the current price of every favorited listing by visiting its own
page, so a price drop is caught even if no scrape happens to come across the
listing again. Supports Facebook Marketplace, Craigslist and Reverb (the
sites favorites come from); other favorites are skipped.
"""
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# First "$1,234" / "Free" text node after the item's <h1> title — the item's
# own price, before any "Today's picks" / related-listing prices further down
# (confirmed live on Facebook Marketplace item pages).
_FB_PRICE_JS = r"""() => {
  const h = document.querySelector('h1');
  if (!h) return null;
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let seen = false, n;
  while ((n = w.nextNode())) {
    if (!seen) { if (h.contains(n)) seen = true; continue; }
    const m = n.textContent.trim().match(/^(\$[\d,]+(?:\.\d{2})?|Free)$/);
    if (m && !h.contains(n)) return m[1];
  }
  return null;
}"""


# Pages that no longer show the item itself. Reverb in particular keeps the
# URL alive after a sale and fills it with *similar* listings (confirmed live:
# "That listing sold. Check out the similar listings below." followed by
# other items' prices) — reading a price there would be wrong.
_GONE_MARKERS = (
    "that listing sold", "this listing has ended", "this posting has been deleted",
    "this posting has expired", "this posting has been flagged for removal",
    "listing isn't available", "listing is no longer available",
)


def _is_gone(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _GONE_MARKERS)


def _craigslist_price(url: str) -> Optional[str]:
    from scrapers.html_scraper import _get_html
    html, _ = _get_html(url)
    soup = BeautifulSoup(html or "", "html.parser")
    if html and _is_gone(soup.get_text(" ")):
        return "GONE"
    el = soup.select_one("span.price")
    return el.get_text(strip=True) if el else None


def _reverb_price(url: str) -> Optional[str]:
    from scrapers.html_scraper import _get_html
    html, _ = _get_html(url, use_playwright=True)
    soup = BeautifulSoup(html or "", "html.parser")
    if html and _is_gone(soup.get_text(" ")):
        return "GONE"
    # The first itemprop=price meta is the item's own; later ones belong to
    # recommended listings on the same page.
    meta = soup.select_one('meta[itemprop="price"]')
    if not meta or not meta.get("content"):
        return None
    try:
        return f"${float(meta['content']):,.0f}"
    except ValueError:
        return None


def check_favorite_prices() -> int:
    """Visits each favorite and records any price change. Returns how many
    dropped in price (those are then reported by the next digest/push)."""
    from scrapers.store import favorite_listings, update_price

    favorites = favorite_listings(limit=500)
    seen_urls = set()
    fb, other = [], []
    for fav in favorites:
        url = fav.get("url") or ""
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        (fb if "facebook.com/marketplace/item" in url else other).append(url)

    current: dict[str, Optional[str]] = {}
    for url in other:
        try:
            if "craigslist.org" in url:
                current[url] = _craigslist_price(url)
            elif "reverb.com/item" in url:
                current[url] = _reverb_price(url)
        except Exception as e:
            logger.warning("Price check failed for %s: %s", url, e)

    if fb:
        try:
            from playwright.sync_api import sync_playwright
            from scrapers.facebook_scraper import SESSION_FILE, _get_browser, _new_context
            if SESSION_FILE.exists():
                with sync_playwright() as pw:
                    browser = _get_browser(pw, headless=True)
                    page = _new_context(browser).new_page()
                    for url in fb:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(2500)
                            if _is_gone(page.inner_text("body")[:4000]):
                                current[url] = "GONE"
                            else:
                                current[url] = page.evaluate(_FB_PRICE_JS)
                        except Exception as e:
                            logger.warning("Price check failed for %s: %s", url, e)
                    browser.close()
        except Exception as e:
            logger.warning("Facebook favorite price check skipped: %s", e)

    from scrapers.store import mark_sold

    drops = 0
    for url, price in current.items():
        if price == "GONE":
            mark_sold(url)
            continue
        if price and re.search(r"\d", price):
            if update_price(url, price) == "drop":
                drops += 1
    logger.info("Favorite price check: %d checked, %d dropped", len(current), drops)
    return drops
