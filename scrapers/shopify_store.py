"""
Any Shopify store collection (e.g. Alto Music's "Pre-Owned / Refurbished &
More"), read through Shopify's public collection JSON
(<collection url>/products.json) — no browser, no account.

Collections aren't sorted newest-first in that JSON, so every page is read
(250 products per request, capped) and filtered by the keyword list; a live
search filters the same data by its one term. Sold-out products are skipped.
"""
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
MAX_PAGES = 12


def _condition(product: dict) -> str:
    tags = " ".join(product.get("tags") or []).lower()
    title = (product.get("title") or "").lower()
    if "b-stock" in title or "b stock" in title or "b-stock" in tags:
        return "B-Stock"
    if "open box" in title or "open-box" in title or "open box" in tags:
        return "Open box"
    if "refurb" in title:
        return "Refurbished"
    if "demo" in title:
        return "Demo"
    return "Used / refurbished"


def scrape_shopify_collection(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    url = source["url"].split("?")[0].rstrip("/")
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    products = []
    try:
        for page in range(1, MAX_PAGES + 1):
            resp = requests.get(f"{url}/products.json", params={"limit": 250, "page": page},
                                headers=HEADERS, timeout=40)
            resp.raise_for_status()
            batch = resp.json().get("products") or []
            products.extend(batch)
            if len(batch) < 250:
                break
            time.sleep(0.5)
    except Exception as e:
        if not products:
            return ScrapeResult(
                source_name=name, source_url=url, success=False,
                error=f"Couldn't read the store's product list: {e}",
                fix_hint="Usually temporary. If it keeps failing, check the collection link on the Sources page.",
                duration_seconds=time.time() - start,
            )

    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    listings = []
    for p in products:
        title = (p.get("title") or "").strip()
        variants = p.get("variants") or []
        available = [v for v in variants if v.get("available")]
        if not title or not available or not keyword_match(title, keywords):
            continue
        price = min(float(v.get("price") or 0) for v in available)
        posted_at = None
        try:
            posted_at = datetime.fromisoformat((p.get("published_at") or p.get("created_at") or ""))
        except ValueError:
            pass
        images = p.get("images") or []
        body = re.sub(r"<[^>]+>", " ", p.get("body_html") or "")
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=f"{base}/products/{p.get('handle')}",
            price=f"${price:,.2f}".replace(".00", "") if price else None,
            description=truncate(f"{_condition(p)} · {body}", 250),
            image_url=images[0].get("src") if images else None,
            posted_at=posted_at,
            listing_id=str(p.get("id")),
        ))

    return ScrapeResult(
        source_name=name,
        source_url=f"{url}?q={requests.utils.quote(term)}" if term else url,
        success=True, listings=listings, duration_seconds=time.time() - start,
    )
