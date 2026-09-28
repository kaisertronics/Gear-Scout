"""
Kijiji (kijiji.ca), Canada — Pro Audio & Recording category (id 615).

Kijiji server-renders its results as JSON inside the page (__NEXT_DATA__ →
Apollo state "StandardListing" entries: title, url, price in cents,
activation date, images, and the ad's location with coordinates), so no
browser is needed. The category page is newest-first; a scheduled run reads
its first pages and filters by the keyword list, a live search uses
Kijiji's own keyword search within the category.

Prices are Canadian dollars — shown as "C$…"; enrich.parse_price converts
them to approximate USD for deal / lowest-price comparisons.
"""
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-CA,en;q=0.9",
}
CATEGORY_URL = "https://www.kijiji.ca/b-pro-audio-recording/canada/{page}c615l0"
SEARCH_URL = "https://www.kijiji.ca/b-pro-audio-recording/canada/{slug}/{page}k0c615l0"
_WANTED = re.compile(r"^\W*(?:wanted|wtb|iso|in search of|looking for|buying)\b", re.I)


def _listings_on(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.S)
    if not m:
        raise ValueError("Kijiji page had no listing data (layout changed?)")
    state = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    return [v for v in state.values() if isinstance(v, dict) and v.get("__typename") == "StandardListing"]


def _price(p: Optional[dict]) -> Optional[str]:
    if not p:
        return None
    if (p.get("type") or "").upper() == "FREE":
        return "Free"
    amount = p.get("amount")
    if not amount:
        return None
    value = amount / 100
    return f"C${value:,.0f}" if value == int(value) else f"C${value:,.2f}"


def _location(loc: Optional[dict]) -> Optional[str]:
    """'Abbotsford, BC @49.06523,-122.35242' — the coordinates let distance
    work for Canadian towns (the town lookup only covers the US); the
    dashboard hides the '@…' part."""
    if not loc:
        return None
    name = loc.get("name") or ""
    prov = re.search(r",\s*([A-Z]{2})\b", loc.get("address") or "")
    text = f"{name}, {prov.group(1)}" if prov and name else (name or None)
    coords = loc.get("coordinates") or {}
    if text and coords.get("latitude") and coords.get("longitude"):
        text += f" @{coords['latitude']:.5f},{coords['longitude']:.5f}"
    return text


def scrape_kijiji(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    if term:
        slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
        urls = [SEARCH_URL.format(slug=slug, page=""), SEARCH_URL.format(slug=slug, page="page-2/")]
        manual_url = urls[0]
    else:
        urls = [CATEGORY_URL.format(page=""), CATEGORY_URL.format(page="page-2/")]
        manual_url = urls[0]

    raw: dict[str, dict] = {}
    try:
        for i, url in enumerate(urls):
            page = _listings_on(url)
            for l in page:
                raw.setdefault(l.get("id"), l)
            if len(page) < 20:  # last page
                break
            time.sleep(0.8)
    except Exception as e:
        if not raw:
            return ScrapeResult(
                source_name=name, source_url=manual_url, success=False,
                error=f"Kijiji fetch failed: {e}",
                fix_hint="Usually temporary — it should work next run.",
                duration_seconds=time.time() - start,
            )

    listings = []
    for lid, l in raw.items():
        title = (l.get("title") or "").strip()
        if not title or _WANTED.match(title) or not keyword_match(title, keywords):
            continue
        posted_at = None
        try:
            posted_at = datetime.fromisoformat((l.get("activationDate") or "").replace("Z", "+00:00"))
        except ValueError:
            pass
        images = l.get("imageUrls") or []
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=l.get("url") or f"https://www.kijiji.ca/v-view-details.html?adId={lid}",
            price=_price(l.get("price")),
            description=truncate(l.get("description") or "", 250),
            image_url=images[0] if images else None,
            posted_at=posted_at,
            listing_id=str(lid),
            location=_location(l.get("location")),
        ))

    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
