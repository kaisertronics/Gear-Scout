"""
OfferUp (offerup.com) — local listings within 50 miles (the most OfferUp's
website allows; it places you by your internet connection, e.g. the Seattle
area). OfferUp server-renders its search results as JSON in the page
(__NEXT_DATA__), so no browser or account is needed.

Scheduled runs search a set of broad words newest-first and keep what
matches the keyword list; a live search sends its one term to OfferUp.
"""
import json
import logging
import re
import time
from urllib.parse import quote_plus

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

SEARCH_URL = "https://offerup.com/search?q={q}&SORT={sort}&DISTANCE=50"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_BROAD_TERMS = [
    "microphone", "mic preamp", "audio compressor", "compressor limiter", "audio mixer",
    "mixing console", "audio interface", "studio monitors",
    "tube mic", "neumann", "shure", "akg", "sennheiser", "recording studio", "rack gear",
    "reel to reel", "equalizer", "api 500", "neve", "tascam", "pro audio",
]


def _search(q: str, sort: str) -> list[dict]:
    resp = requests.get(SEARCH_URL.format(q=quote_plus(q), sort=sort), headers=HEADERS, timeout=25)
    resp.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.S)
    if not m:
        raise ValueError("OfferUp page had no listing data (layout changed or blocked)")
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("__typename") == "ModularFeedListing" and node.get("listingId"):
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(json.loads(m.group(1)))
    return out


def scrape_offerup(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    searches = [(term, "-posted")] if term else [
        (t, "-posted") for t in (source.get("broad_terms") or DEFAULT_BROAD_TERMS)]
    manual_url = SEARCH_URL.format(q=quote_plus(term or "microphone"), sort="-posted")

    raw: dict[str, dict] = {}
    errors = []
    for i, (q, sort) in enumerate(searches):
        if i:
            time.sleep(0.8)
        try:
            for item in _search(q, sort):
                raw.setdefault(item["listingId"], item)
        except Exception as e:
            errors.append(str(e))
    if not raw and errors:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error=f"OfferUp fetch failed: {errors[0]}",
            fix_hint="Usually temporary — it should work next run.",
            duration_seconds=time.time() - start,
        )

    listings = []
    for lid, item in raw.items():
        title = (item.get("title") or "").strip()
        if not title or not keyword_match(title, keywords):
            continue
        price = item.get("price")
        try:
            value = float(price) if price not in (None, "") else None
        except ValueError:
            value = None
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=f"https://offerup.com/item/detail/{lid}",
            price=(f"${value:,.0f}" if value == int(value) else f"${value:,.2f}") if value else None,
            description=item.get("conditionText") or "",
            image_url=(item.get("image") or {}).get("url"),
            listing_id=lid,
            location=item.get("locationName"),
        ))
    return ScrapeResult(source_name=name, source_url=manual_url, success=True,
                        listings=listings, duration_seconds=time.time() - start)
