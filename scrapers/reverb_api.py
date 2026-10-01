"""
Reverb through its public listings API (the same one used for market
values) instead of loading reverb.com in a browser — about 1–3 seconds
instead of ~25, and no "page didn't finish loading" failures.

Scheduled runs: the newest used pro-audio listings, page by page, until
they're older than LOOKBACK_HOURS (so each hourly run sees everything new
since the last one). Live search: Reverb's own search for the term.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

API_URL = "https://api.reverb.com/api/listings/all"
HEADERS = {
    "Accept-Version": "3.0",
    "Accept": "application/hal+json",
    "User-Agent": "GearScout/1.0 (self-hosted gear alerts)",
}
LOOKBACK_HOURS = 3
MAX_PAGES = 10


def _to_listing(item: dict, name: str) -> Listing:
    price = item.get("price") or {}
    photos = item.get("photos") or []
    image = None
    if photos:
        links = photos[0].get("_links") or {}
        image = (links.get("large_crop") or links.get("small_crop") or links.get("full") or {}).get("href")
    posted = None
    try:
        posted = datetime.fromisoformat(item.get("published_at") or "")
    except ValueError:
        pass
    condition = (item.get("condition") or {}).get("display_name") or ""
    amount = price.get("display") or None
    if amount and price.get("currency") not in (None, "USD"):
        amount = f"{price.get('currency')} {price.get('amount')}"
    return Listing(
        source_name=name,
        title=truncate(item.get("title") or "", 120),
        url=((item.get("_links") or {}).get("web") or {}).get("href") or f"https://reverb.com/item/{item.get('id')}",
        price=amount,
        description=truncate(f"{condition} · {item.get('description') or ''}".strip(" ·"), 250),
        image_url=image,
        posted_at=posted,
        listing_id=str(item.get("id")),
    )


def scrape_reverb_api(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    if term:
        params = {"query": term, "condition": "used", "per_page": 50}
        manual_url = f"https://reverb.com/marketplace?query={quote_plus(term)}&condition=used"
        pages = 3
    else:
        params = {"product_type": "pro-audio", "condition": "used", "sort": "published_at|desc", "per_page": 50}
        manual_url = source.get("url") or "https://reverb.com/marketplace?product_type=pro-audio&condition=used"
        pages = MAX_PAGES
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    items: list[dict] = []
    try:
        for page in range(1, pages + 1):
            resp = requests.get(API_URL, params={**params, "page": page}, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            batch = resp.json().get("listings") or []
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
            if not term:
                try:
                    oldest = datetime.fromisoformat(batch[-1].get("published_at") or "")
                    if oldest < cutoff:
                        break
                except ValueError:
                    break
    except Exception as e:
        if not items:
            return ScrapeResult(source_name=name, source_url=manual_url, success=False,
                                error=f"Reverb API failed: {e}",
                                fix_hint="Usually temporary — it should work next run.",
                                duration_seconds=time.time() - start)

    # Listings stored by the old browser-based reader used a different ID;
    # reuse it for items already known, so they aren't counted as new again.
    known: dict[str, str] = {}
    try:
        import re
        from scrapers.store import _conn
        with _conn() as conn:
            for gid, url in conn.execute(
                    "SELECT global_id, url FROM seen WHERE source_name = ? AND url LIKE '%reverb.com/item/%'", (name,)):
                m = re.search(r"/item/(\d+)", url or "")
                if m:
                    known[m.group(1)] = gid.split("::", 1)[-1]
    except Exception:
        logger.exception("Couldn't read known Reverb listings")

    listings, seen = [], set()
    for item in items:
        lid = item.get("id")
        if lid in seen or (item.get("state") or {}).get("slug", "live") != "live":
            continue
        seen.add(lid)
        title = item.get("title") or ""
        if title and keyword_match(title, keywords):
            listing = _to_listing(item, name)
            if listing.listing_id in known:
                listing.listing_id = known[listing.listing_id]
            listings.append(listing)
    return ScrapeResult(source_name=name, source_url=manual_url, success=True,
                        listings=listings, duration_seconds=time.time() - start)
