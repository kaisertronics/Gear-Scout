"""
HTML scrapers for Guitar Center Used, Sweetwater Used, and Craigslist.
Sites that block requests use Playwright for browser-like fetching.
"""
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from .base import Listing, ScrapeResult, clean_price, keyword_match, truncate

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # No "br": requests/urllib3 can't decode Brotli without the optional
    # `brotli` package installed, and would silently return garbage bytes for
    # any server that honors this and responds with Brotli encoding.
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _playwright_get(url: str, wait_ms: int = 3000) -> Optional[str]:
    """Fetch a page using Playwright (bypasses bot detection)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.warning("Playwright fetch failed for %s: %s", url, e)
        return None


def _get_html(url: str, use_playwright: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Return (html, error). Tries requests first, Playwright as fallback."""
    if use_playwright:
        html = _playwright_get(url)
        return html, None if html else "Playwright fetch failed"

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 403:
            # Retry with Playwright
            logger.info("Got 403, retrying with Playwright: %s", url)
            html = _playwright_get(url)
            if html:
                return html, None
            return None, "HTTP 403 — blocked even with browser emulation"
        resp.raise_for_status()
        return resp.text, None
    except requests.exceptions.HTTPError as e:
        return None, f"HTTP {e.response.status_code}: {e}"
    except requests.exceptions.Timeout:
        return None, "Request timed out"
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Guitar Center Used
# ---------------------------------------------------------------------------

def scrape_guitar_center(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch page",
            fix_hint=(
                "Guitar Center sits behind Akamai Bot Manager, which blocks "
                "headless Playwright (confirmed via a reCAPTCHA challenge) — "
                "use the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    cards = (
        soup.select(".product-card") or
        soup.select("[data-test='product-card']") or
        soup.select(".plp-card") or
        soup.select("article.product") or
        soup.select("li.product-item") or
        soup.select("[class*='ProductCard']")
    )

    if not cards:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find product cards on Guitar Center page.",
            fix_hint=(
                "Guitar Center's Akamai protection redirects the scraper to "
                "the homepage instead of serving results (confirmed via a "
                "reCAPTCHA challenge) — that's why no cards are found. Use "
                "the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    for card in cards:
        title_el = (card.select_one("h2") or card.select_one("h3") or
                    card.select_one(".product-title") or
                    card.select_one("[data-test='product-name']"))
        price_el = (card.select_one(".price") or
                    card.select_one("[data-test='price']") or
                    card.select_one("[class*='price']"))
        link_el = card.select_one("a[href]")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.guitarcenter.com" + href

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Sweetwater Used
# ---------------------------------------------------------------------------

def scrape_sweetwater(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch page",
            fix_hint=(
                "Sweetwater sits behind Akamai Bot Manager, which blocks "
                "headless Playwright (confirmed via a reCAPTCHA challenge) — "
                "use the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    cards = (
        soup.select(".product-item") or
        soup.select(".used-gear-item") or
        soup.select("[class*='ProductCard']") or
        soup.select("li.grid-item") or
        soup.select(".product")
    )

    if not cards:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find product cards on Sweetwater page.",
            fix_hint=(
                "Sweetwater's Akamai protection redirects the scraper away "
                "from results (confirmed via a reCAPTCHA challenge) — that's "
                "why no cards are found. Use the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    for card in cards:
        title_el = (card.select_one("h2") or card.select_one("h3") or
                    card.select_one(".product-name") or
                    card.select_one("[class*='title']"))
        price_el = (card.select_one(".price") or
                    card.select_one("[class*='price']") or
                    card.select_one("[class*='Price']"))
        link_el = card.select_one("a[href]")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.sweetwater.com" + href

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Craigslist
# ---------------------------------------------------------------------------

def _cl_image_map(soup) -> dict:
    """Map listing title -> first image URL, from Craigslist's own embedded
    JSON-LD structured data. The static <li> markup scrape_craigslist() reads
    has no <img> tags at all, but the same page embeds a
    <script id="ld_searchpage_results"> block with each listing's images —
    matched here by title since that JSON-LD has no per-item URL to join on."""
    script = soup.find("script", id="ld_searchpage_results")
    if not script:
        return {}
    try:
        data = json.loads(script.get_text())
    except (ValueError, TypeError):
        return {}
    image_map = {}
    for entry in data.get("itemListElement", []):
        item = entry.get("item", {})
        title = item.get("name")
        images = item.get("image") or []
        if title and images:
            image_map[title] = images[0]
    return image_map


def _scrape_craigslist_listings(
    name: str, url: str, keywords: list[str], use_row_location: bool = False
) -> tuple[Optional[list[Listing]], Optional[str], Optional[str]]:
    """Fetch + parse one Craigslist search-results page.

    Returns (listings, None, None) on success, or (None, error, error_kind)
    on failure — error_kind is "fetch" (couldn't load the page at all) or
    "layout" (page loaded but the listing selectors didn't match anything).
    Shared by scrape_craigslist() (a single configured city) and
    scrape_craigslist_region() (a broad multi-state radius search), so
    both stay in sync when Craigslist changes their markup.

    use_row_location=True tags each Listing with ITS OWN posted city (parsed
    from that row's ".location" element, e.g. "Sherman Oaks") instead of the
    single `name` passed in — used for the radius-based regional search,
    where one request's results span many different cities. Left off (the
    default) for a single configured city, where `name` is already the
    right, stable label — changing it would mint new global_ids for
    listings already stored under the old name, making them look "new" again.
    """
    # Don't pre-filter server-side with a multi-keyword query — Craigslist
    # treats "query=a+b+c" as a phrase requiring ALL words, which almost never
    # matches. Fetch the recent listings and filter locally with keyword_match,
    # same as the other scrapers. This means a scheduled run (the full
    # keyword list) only ever sees whatever's on page 1 sorted by newest —
    # fine for "alert me to new stuff," but it can never surface something
    # that's been sitting on Craigslist for months, buried under thousands
    # of newer posts, no matter how well it matches.
    #
    # A single search term is different — that's exactly what a text search
    # box is for, and it's what the dashboard's one-off "live search" sends
    # (one query, not the whole keyword list). Confirmed live: Craigslist's
    # own search genuinely finds old listings this way (e.g. a "Behringer
    # x32 rack" posted days ago, well past page 1 of "newest"). So when
    # there's exactly one term, also send it to Craigslist's real search —
    # keyword_match below still applies afterward as a safety net.
    sep = "&" if "?" in url else "?"
    search_url = f"{url}{sep}sort=date"
    if len(keywords) == 1 and keywords[0].strip():
        # Craigslist only matches whole words, so "u87" misses "U87Ai" /
        # "C38B"-style model variants; a trailing * on each model-number word
        # makes it a prefix match (confirmed live: "u87*" returned every
        # "u87" result plus U87A ones).
        words = []
        for word in keywords[0].split():
            if re.search(r'[a-z]', word, re.I) and re.search(r'\d', word) and not word.endswith("*"):
                word += "*"
            words.append(word)
        search_url += f"&query={quote_plus(' '.join(words))}"

    html, error = _get_html(search_url)
    if not html:
        return None, error or "Fetch failed", "fetch"

    soup = BeautifulSoup(html, "html.parser")

    # Craigslist's subdomain search URLs now redirect to
    # www.craigslist.org/search/area/{city}?cat=... which server-renders a
    # static <li class="cl-static-search-result"> list (older selectors below
    # kept as fallback in case a given city/layout still serves them).
    results = (
        soup.select("li.cl-static-search-result") or
        soup.select("li.cl-search-result") or
        soup.select("li[data-pid]") or
        soup.select("li.result-row") or
        soup.select("[data-pid]") or
        soup.select("div.result-info")
    )

    if not results:
        # A real, valid search that just has zero matches (common with a
        # server-side query= search, unlike the browse-everything case)
        # renders this exact page instead of any listing cards — not a
        # layout problem, just nothing to show. Check for it before
        # assuming the selectors are stale.
        no_results_text = soup.get_text().lower()
        if "no results found" in no_results_text:
            return [], None, None

        # Try Playwright as fallback (CL sometimes needs JS)
        logger.info("Craigslist: no results with requests, trying Playwright")
        html = _playwright_get(search_url, wait_ms=4000)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            results = (
                soup.select("li.cl-static-search-result") or
                soup.select("li.cl-search-result") or
                soup.select("li.result-row") or
                soup.select("[data-pid]")
            )
            if not results and "no results found" in soup.get_text().lower():
                return [], None, None

    if not results:
        return None, "Could not find listings on Craigslist page.", "layout"

    image_map = _cl_image_map(soup)
    listings = []

    for row in results:
        # cl-static-search-result: <a href="..."><div class="title">..</div>
        #   <div class="details"><div class="price">$X</div>...</div></a>
        title_el = (row.select_one(".title") or
                    row.select_one(".titlestring") or
                    row.select_one("a.posting-title span") or
                    row.select_one(".result-title") or
                    row.select_one("a[data-id]") or
                    row.select_one("a.cl-app-anchor"))
        price_el = (row.select_one(".price") or
                    row.select_one(".priceinfo") or
                    row.select_one(".result-price") or
                    row.select_one("[class*='price']"))
        link_el = (row.select_one("a[href]") or
                   row.select_one("a.posting-title") or
                   row.select_one("a[href*='craigslist.org']"))
        img_el = row.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            # Try data attribute / title attribute on the <li> itself
            title = row.get("data-title", "") or row.get("title", "")
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else row.get("data-href", "")

        # New URLs: https://www.craigslist.org/view/d/{slug}/{id}
        cl_id_match = (re.search(r'/(\d{10})\.html', href) or
                       re.search(r'/view/d/[^/]+/([A-Za-z0-9]+)$', href))
        listing_id = cl_id_match.group(1) if cl_id_match else None
        if not listing_id:
            listing_id = row.get("data-pid", href[-20:] if href else None)

        row_source_name = name
        loc_el = row.select_one(".location") or row.select_one(".result-hood")
        loc = loc_el.get_text(strip=True).strip("()") if loc_el else ""
        if use_row_location and loc:
            row_source_name = f"Craigslist — {loc}"

        listings.append(Listing(
            source_name=row_source_name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=(img_el.get("src") if img_el else None) or image_map.get(title),
            listing_id=listing_id,
            location=loc or None,
        ))

    return listings, None, None


def scrape_craigslist(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    listings, error, error_kind = _scrape_craigslist_listings(name, url, keywords)

    if listings is None:
        if error_kind == "layout":
            return ScrapeResult(
                source_name=name, source_url=url, success=False,
                error=error,
                fix_hint=(
                    "Craigslist has updated their layout. "
                    "Open the search URL in a browser, inspect a listing row, "
                    "and update the selector in scrapers/html_scraper.py → scrape_craigslist(). "
                    "Also verify the city URL is correct — try replacing /search/msa with /search/sss "
                    "if musical instruments isn't returning results."
                ),
                duration_seconds=time.time() - start,
            )
        return _cl_error(name, url, error, start)

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# Three broad regions that together cover the continental US, using
# Craigslist's own "search distance" radius filter (postal + search_distance
# on any /search/area/{slug} URL — confirmed live: search_distance=1000
# genuinely returns results ~1000 miles out, e.g. a Seattle-area URL with a
# Miami postal code returns Cincinnati/Tampa/Nashville/etc). The area slug in
# the URL path turns out not to matter once postal overrides it — these
# reuse slugs already proven to work elsewhere in this app. Centers were
# picked so the three 1000-mile circles overlap and jointly cover the whole
# continental US. Each is configured in config.yaml as its own standalone
# `craigslist_region` source (see scrape_craigslist_region below) rather
# than one bundled multi-region source, so each shows up individually and
# can be toggled on its own.
_CRAIGSLIST_REGIONS = [
    ("US West", "seattle", "84101"),     # centered near Salt Lake City, UT
    ("US Central", "chicago", "67202"),  # centered near Wichita, KS
    ("US East", "newyork", "40202"),     # centered near Louisville, KY
]


def craigslist_region_url(area_slug: str, postal: str) -> str:
    return f"https://www.craigslist.org/search/area/{area_slug}?cat=msa&postal={postal}&search_distance=1000"


def default_craigslist_region_sources() -> list[dict]:
    """The 3 canonical region sources (see _CRAIGSLIST_REGIONS) as plain
    source dicts, matching exactly what config.yaml.example ships. Used by
    live_search.py to make sure a live search always covers all three
    regions even if the user has removed or disabled one for scheduled runs."""
    return [
        {
            "name": f"Craigslist — {region_name}",
            "url": craigslist_region_url(area_slug, postal),
            "type": "craigslist_region",
            "enabled": True,
        }
        for region_name, area_slug, postal in _CRAIGSLIST_REGIONS
    ]


def scrape_craigslist_region(source: dict, keywords: list[str]) -> ScrapeResult:
    """One broad ~1000-mile-radius Craigslist region search (US West, US
    Central, or US East — see _CRAIGSLIST_REGIONS). Each listing carries its
    own actual posted city as Listing.source_name (e.g. "Craigslist —
    Sherman Oaks"), parsed from that row's own location text, so it
    groups/searches/displays exactly like an individually-configured city
    everywhere else in the app, even though this is a single broad search
    rather than one request per city.
    """
    name = source["name"]
    url = source["url"]
    start = time.time()

    listings, error, error_kind = _scrape_craigslist_listings(
        name, url, keywords, use_row_location=True,
    )

    if listings is None:
        if error_kind == "layout":
            return ScrapeResult(
                source_name=name, source_url=url, success=False,
                error=error,
                fix_hint=(
                    "Craigslist has updated their layout. Open the search URL "
                    "in a browser, inspect a listing row, and update the "
                    "selector in scrapers/html_scraper.py → _scrape_craigslist_listings()."
                ),
                duration_seconds=time.time() - start,
            )
        return _cl_error(name, url, error, start)

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


def _cl_error(name, url, error, start) -> ScrapeResult:
    hint = (
        "Could not fetch Craigslist. Verify the city subdomain is correct "
        "(e.g. seattle.craigslist.org) and the path is /search/msa. "
        "If your IP is blocked, Craigslist may require a cooldown period."
    )
    return ScrapeResult(
        source_name=name, source_url=url, success=False,
        error=error or "Fetch failed", fix_hint=hint,
        duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Reverb (HTML scrape — they dropped RSS)
# ---------------------------------------------------------------------------

def scrape_reverb(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    # A single search term (typical of the dashboard's live search) can be
    # sent to Reverb's own real search via query= — confirmed live: this
    # surfaces actual matches (e.g. "72 Results for neve 1073"), not just
    # whatever's on page 1 of "recently listed". Left off for the full
    # keyword list (scheduled runs) for the same AND-phrase reason as
    # Craigslist/eBay.
    if len(keywords) == 1 and keywords[0].strip():
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}query={quote_plus(keywords[0].strip())}"

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch Reverb page",
            fix_hint="Reverb may be blocking. Try again next run.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    if soup.title and "just a moment" in soup.title.get_text().strip().lower():
        # Deliberately NOT marked blocked=True (unlike Gearspace/GC/
        # Sweetwater/eBay's confirmed-permanent challenges) — Reverb
        # clearly succeeds most runs (it's one of the more reliable
        # sources), this Cloudflare challenge just shows up intermittently.
        # Treating it as a normal failure keeps it visible in status/email
        # instead of being silently written off as permanently broken.
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Reverb served a Cloudflare bot-check page instead of results.",
            fix_hint=(
                "This is Cloudflare bot protection (confirmed by the page's "
                "own \"Are you human?\" challenge screen), not a stale "
                "selector — but it's intermittent for Reverb specifically "
                "(this run got challenged; most runs don't). No action "
                "needed, it typically works again next run."
            ),
            duration_seconds=time.time() - start,
        )

    # Reverb's old marketplace URL/selectors both went dead (?condition=used
    # &category=studio-recording 404s outright now). Current markup uses
    # .rc-listing-card cards; kept the old selectors as fallbacks in case
    # Reverb changes layout again rather than the URL.
    cards = (
        soup.select(".rc-listing-card") or
        soup.select("[data-listing-id]") or
        soup.select(".csp-tile") or
        soup.select(".listing-card") or
        soup.select("[class*='ListingCard']") or
        soup.select("article")
    )

    if not cards:
        # A real search with genuinely zero matches (common with a
        # server-side query= search) renders this exact message instead of
        # any cards — not a layout problem, just nothing to show.
        if "nothing here at the moment" in soup.get_text().lower():
            return ScrapeResult(
                source_name=name, source_url=url, success=True,
                listings=[], duration_seconds=time.time() - start,
            )
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find listing cards on Reverb page.",
            fix_hint=(
                "Reverb may have updated their layout. Open the URL in a browser, "
                "inspect a listing card, and update the selector in "
                "scrapers/html_scraper.py → scrape_reverb()."
            ),
            duration_seconds=time.time() - start,
        )

    # A live search only sees page 1 otherwise — confirmed live: "wa47"
    # (used + B-stock) had 58 matches on page 1 and 13 more on page 2.
    # Scheduled runs browse newest-first and only need page 1.
    is_live_search = len(keywords) == 1 and keywords[0].strip()
    if is_live_search:
        page_hrefs = lambda cs: {
            a["href"] for c in cs for a in c.select("a[href*='/item/']")[:1]
        }
        known = page_hrefs(cards)
        for page in range(2, 6):
            page_html, _ = _get_html(f"{url}&page={page}", use_playwright=True)
            if not page_html:
                break
            page_cards = BeautifulSoup(page_html, "html.parser").select(".rc-listing-card")
            new_hrefs = page_hrefs(page_cards) - known
            if not new_hrefs:
                break
            known |= new_hrefs
            cards = list(cards) + page_cards

    seen_listing_ids: set[str] = set()
    seen_hrefs: set[str] = set()
    for card in cards:
        title_el = (card.select_one(".rc-listing-card__title-element") or
                    card.select_one("h3") or card.select_one("h2") or
                    card.select_one("[class*='title']") or
                    card.select_one("[class*='Title']"))
        price_el = (card.select_one(".rc-price-block__price") or
                    card.select_one("[class*='price']") or
                    card.select_one("[class*='Price']"))
        link_el = card.select_one("a[href*='/item/']") or card.select_one("a[href]")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://reverb.com" + href

        listing_id = card.get("data-listing-id") or re.sub(r'[^a-z0-9]', '', title.lower())[:32]
        # The title-slug ID keeps existing stored listings from looking new,
        # but two different listings with the same title (common: several
        # sellers all listing "Neumann KM 184 Microphone") would collapse
        # into one. Only when a title repeats within this page, fall back to
        # the item's own ID from its URL for the repeats.
        if href and href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        if listing_id in seen_listing_ids:
            item_match = re.search(r'/item/(\d+)', href)
            if item_match:
                listing_id = f"{listing_id}-{item_match.group(1)}"
        seen_listing_ids.add(listing_id)

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=listing_id,
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# eBay (their RSS search feed was fully discontinued — HTTP 204 for every
# query, even generic ones like "iphone", confirmed from an authenticated
# browser session too, so it's not a bot-detection block. Their category
# browse pages (/b/... and ?_sacat=<old-id>) are also gone/renumbered, so
# this searches by keyword instead — same local-keyword-filter design as
# Craigslist et al.)
# ---------------------------------------------------------------------------

def scrape_ebay(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    # eBay is confirmed permanently blocked below regardless of URL (Akamai
    # blocks headless Playwright outright), so this doesn't change whether
    # automated scraping works — but it does make the "Open in browser"
    # manual link actually match a single live-search term, instead of just
    # the generic category page. Category-browse URLs (/b/...) silently
    # ignore a _nkw param tacked on (confirmed live) — eBay's real search
    # lives at /sch/i.html, with the same Pro Audio category id (180014)
    # passed as _sacat instead of being baked into the path.
    if len(keywords) == 1 and keywords[0].strip():
        url = (
            f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(keywords[0].strip())}"
            f"&_sacat=180014&LH_ItemCondition=1500%7C2500%7C3000&_sop=10"
        )

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch eBay page",
            fix_hint="eBay sits behind Akamai bot protection — use the manual link below instead.",
            blocked=True,
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    if soup.title and "Error Page" in soup.title.get_text():
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="eBay served a bot-detection error page instead of results.",
            fix_hint=(
                "This is Akamai bot protection (eBay's CDN), not a stale selector — "
                "it blocks headless Playwright specifically, confirmed reproducible. "
                "Use the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    cards = soup.select("li.s-card")
    if not cards:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find listing cards on eBay page.",
            fix_hint=(
                "eBay may have updated their layout. Open the URL in a browser, "
                "inspect a listing card, and update the selector in "
                "scrapers/html_scraper.py → scrape_ebay()."
            ),
            duration_seconds=time.time() - start,
        )

    for card in cards:
        title_el = card.select_one(".s-card__title")
        price_el = card.select_one(".s-card__price")
        link_el = card.select_one("a[href*='/itm/']")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        # eBay mixes in "Shop on eBay" ad placeholders (fake item 123456,
        # generic ebaystatic.com image) among the real results.
        title = re.sub(r'Opens in a new window or tab$', '', title).strip()
        if not title or title == "Shop on eBay" or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        item_id_match = re.search(r'/itm/(\d+)', href)
        listing_id = item_id_match.group(1) if item_id_match else re.sub(r'[^a-z0-9]', '', title.lower())[:32]

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=listing_id,
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Audiogon
# ---------------------------------------------------------------------------

def scrape_audiogon(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    # A single search term (typical of the dashboard's live search) can be
    # sent to Audiogon's own real search via q= — confirmed live: this
    # surfaces actual matches (title changes to "X for sale | Listings |
    # Audiogon"), not just whatever's on page 1 of the base listings page.
    # Left off for the full keyword list (scheduled runs) for the same
    # AND-phrase reason as Craigslist/Reverb/eBay.
    if len(keywords) == 1 and keywords[0].strip():
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}q={quote_plus(keywords[0].strip())}"

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch Audiogon page",
            fix_hint="Audiogon may be blocking scrapers. Try again next run.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    cards = soup.select(".listing-thumbnail")
    if not cards:
        # A real search with genuinely zero matches shows "0 Listings"
        # instead of any cards (confirmed live) — not a layout problem.
        if "0 listings" in soup.get_text().lower():
            return ScrapeResult(
                source_name=name, source_url=url, success=True,
                listings=[], duration_seconds=time.time() - start,
            )
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find listing cards on Audiogon page.",
            fix_hint=(
                "Audiogon may have updated their layout. Open the URL in a browser, "
                "inspect a listing card, and update the selector in "
                "scrapers/html_scraper.py → scrape_audiogon()."
            ),
            duration_seconds=time.time() - start,
        )

    for card in cards:
        title_el = card.select_one(".listing-thumbnail-title")
        price_el = card.select_one(".listing-thumbnail-price")
        link_el = card.select_one("a[href^='/listings/']")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.audiogon.com" + href

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=card.get("data-id") or re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Vintage King — Used Gear
# ---------------------------------------------------------------------------

def scrape_vintageking(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    html, error = _get_html(url)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch Vintage King page",
            fix_hint="Vintage King may be blocking scrapers. Try again next run.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    cards = soup.select(".product-item")
    if not cards:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find product cards on Vintage King page.",
            fix_hint=(
                "Vintage King may have updated their layout. Open the URL in a browser, "
                "inspect a product card, and update the selector in "
                "scrapers/html_scraper.py → scrape_vintageking()."
            ),
            duration_seconds=time.time() - start,
        )

    for card in cards:
        title_el = card.select_one(".product-item-link")
        price_el = card.select_one(".price")
        link_el = card.select_one("a.product-item-link") or card.select_one("a.product-item-photo")
        img_el = card.select_one("img")

        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""
        if href and not href.startswith("http"):
            href = "https://vintageking.com" + href

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_el.get_text(strip=True)) if price_el else None,
            image_url=img_el.get("src") if img_el else None,
            listing_id=re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# US Audio Mart (their RSS feed was discontinued — scrape the listing table)
# ---------------------------------------------------------------------------

def scrape_usaudiomart(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch US Audio Mart page",
            fix_hint="US Audio Mart may be blocking scrapers. Try again next run.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    rows = soup.select("tr.ad")
    if not rows:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find classified rows on US Audio Mart page.",
            fix_hint=(
                "US Audio Mart may have updated their layout. Open the URL in a browser, "
                "inspect a listing row, and update the selector in "
                "scrapers/html_scraper.py → scrape_usaudiomart()."
            ),
            duration_seconds=time.time() - start,
        )

    for row in rows:
        link_el = row.select_one("a[href*='/details/']")
        price_cell = row.select_one("td.rightCell")

        title = link_el.get_text(strip=True) if link_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = link_el["href"] if link_el else ""

        # The list page has no <img> at all (just a "has photo" camera icon),
        # so the actual image only exists on each listing's own detail page.
        # Only fetching it for rows that already passed the keyword filter
        # keeps this bounded to a handful of extra Playwright loads per run,
        # not one per row on the page.
        image_url = None
        if href:
            detail_html = _playwright_get(href, wait_ms=1500)
            if detail_html:
                detail_soup = BeautifulSoup(detail_html, "html.parser")
                og_image = detail_soup.select_one('meta[property="og:image"]')
                image_url = og_image.get("content") if og_image else None

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=clean_price(price_cell.get_text(strip=True)) if price_cell else None,
            image_url=image_url,
            listing_id=re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


# ---------------------------------------------------------------------------
# Generic forum scraper (Gearspace, DIYAudio, etc.)
# ---------------------------------------------------------------------------

def scrape_forum_html(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    html, error = _get_html(url, use_playwright=True)
    if not html:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=error or "Failed to fetch forum page",
            fix_hint=f"Could not load {url}. The site may be blocking scrapers.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    if soup.title and "just a moment" in soup.title.get_text().strip().lower():
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Site served a Cloudflare bot-check page instead of the forum.",
            fix_hint=(
                "This is Cloudflare bot protection, not a stale selector — "
                "confirmed by the page's own \"Performing security "
                "verification\" challenge screen. It blocks headless "
                "Playwright specifically. Use the manual link below instead."
            ),
            blocked=True,
            duration_seconds=time.time() - start,
        )

    # Standard forum thread selectors (XenForo 2.x, XenForo 1.x, vBulletin, phpBB)
    threads = (
        soup.select("div.structItem--thread") or   # XenForo 2.x (Gearspace, DIYAudio)
        soup.select("div.structItem") or           # XenForo 2.x generic
        soup.select("li.discussionListItem") or    # XenForo 1.x
        soup.select("tr.thread") or                # vBulletin
        soup.select("li[id^='thread']") or         # phpBB
        soup.select(".forumrow") or
        soup.select("div[class*='thread']") or
        soup.select("div[class*='Topic']")
    )

    for thread in threads:
        title_el = (
            # XenForo 2.x wraps a prefix/"labelLink" badge <a> before the real
            # title <a> inside .structItem-title — select_one() on the bare
            # "a" selector would grab that badge instead of the title.
            thread.select_one("div.structItem-title a:not(.labelLink)") or
            thread.select_one(".structItem-title a:not(.labelLink)") or
            thread.select_one("div.structItem-title a") or
            thread.select_one(".structItem-title a") or
            thread.select_one("a.structItem-title") or
            thread.select_one(".title a") or
            thread.select_one("a[data-preview-url]") or
            thread.select_one("h3 a") or
            thread.select_one("a.threadTitle")
        )
        title = title_el.get_text(strip=True) if title_el else ""
        if not title or not keyword_match(title, keywords):
            continue

        href = title_el.get("href", "") if title_el else ""
        if href and not href.startswith("http"):
            # Make absolute based on source URL domain
            from urllib.parse import urljoin
            href = urljoin(url, href)

        price = None
        price_match = re.search(r'\$[\d,]+', title)
        if price_match:
            price = price_match.group(0)

        # Thumbnail, when the forum/classifieds add-on renders one (e.g.
        # Gearspace's classifieds cards) — plain discussion threads usually
        # won't have one, which is fine, just leaves image_url unset.
        img_el = (
            thread.select_one(".casThumbnail-image") or
            thread.select_one(".structItem-iconContainer img") or
            thread.select_one("img[src]:not([src^='data:'])")
        )
        image_url = img_el.get("src") if img_el else None
        if image_url and not image_url.startswith("http"):
            from urllib.parse import urljoin
            image_url = urljoin(url, image_url)

        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=href,
            price=price,
            image_url=image_url,
            listing_id=re.sub(r'[^a-z0-9]', '', title.lower())[:32],
        ))

    if not threads:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Could not find thread listings on forum page.",
            fix_hint=(
                f"Forum layout not recognized for {url}. "
                "Inspect a thread row and add its CSS selector to "
                "scrape_forum_html() in scrapers/html_scraper.py."
            ),
            duration_seconds=time.time() - start,
        )

    return ScrapeResult(
        source_name=name, source_url=url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
