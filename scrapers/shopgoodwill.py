"""
ShopGoodwill (shopgoodwill.com) via the public JSON API its own site uses —
no account or browser needed.

Its categories are too mixed to browse (a "mixer" search is mostly kitchen
mixers), so a scheduled run searches a short set of broad audio terms,
newest listings first, and filters the results against the full keyword
list like every other source. A live search sends the one term directly.

Most items are auctions: the price is the current bid, not a sale price, so
those are labelled in the description (the dashboard keeps them out of
deal tags and lowest-price tracking).
"""
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

API_URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"
SITE_URL = "https://shopgoodwill.com"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": SITE_URL,
    "Referer": SITE_URL + "/",
}
BROAD_TERMS = [
    "microphone", "condenser mic", "ribbon mic", "preamp", "mixing console", "mixer audio",
    "audio interface", "studio monitor", "compressor limiter", "equalizer", "reel to reel",
    "tape recorder", "rack mount", "tube amplifier", "pro audio",
]


# ShopGoodwill sells everything; a "neumann" search also returns a vase by an
# artist named Neumann. These categories are never audio gear (checked
# against the categories real pro-audio results land in).
_NOT_AUDIO_CATEGORY = re.compile(
    r"decor|for the home|pottery|glass|seasonal|holiday|fishing|hunting|cycling|camera|camcorder|"
    r"projector|security|surveillance|networking|\btvs?\b|tea|coffee|kitchen|tableware|appliance|"
    r"tools|art supplies|beauty|jewel|clothing|apparel|hats|toys|books|collectible|sports|gaming|"
    r"peripherals|furniture|garden|pet|baby|craft|office|automotive|figurine|dolls|coins|stamps|religious",
    re.I,
)


# Categories where audio gear lands, browsed newest-first page by page
# (the site returns at most 40 per page). (category id, level)
CATEGORIES = [(13, 1), (431, 2)]   # Musical Instruments; Vintage Electronics
# Lots are listed in daily batches, so it reads a fixed number of pages:
# ~160 newest lots per category hourly, ~1,000 on full runs.
MAX_PAGES_FULL, MAX_PAGES_LIGHT = 25, 4


def _browse(cat: int, level: int, page: int) -> list[dict]:
    body = _body("", page)
    body.update({"selectedCategoryIds": str(cat), "categoryLevelNo": str(level), "categoryLevel": level,
                 "categoryId": cat, "catIds": str(cat)})
    resp = requests.post(API_URL, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return (resp.json().get("searchResults") or {}).get("items") or []


def _body(text: str, page: int = 1, page_size: int = 40) -> dict:
    return {
        "isSize": False, "isWeddingCatagory": "false", "isMultipleCategoryIds": False,
        "isFromHeaderMenuTab": False, "layout": "", "isFromHomePage": False,
        "searchText": text, "selectedGroup": "", "selectedCategoryIds": "", "selectedSellerIds": "",
        "lowPrice": "0", "highPrice": "999999", "searchBuyNowOnly": "", "searchPickupOnly": "false",
        "searchNoPickupOnly": "false", "searchOneCentShippingOnly": "false",
        "searchDescriptions": "false", "searchClosedAuctions": "false",
        "closedAuctionEndingDate": "1/1/2026", "closedAuctionDaysBack": "7",
        "searchCanadaShipping": "false", "searchInternationalShippingOnly": "false",
        # sortColumn 1 descending = most recently listed first (checked live).
        "sortColumn": "1", "page": str(page), "pageSize": str(page_size), "sortDescending": "true",
        "savedSearchId": 0, "useBuyerPrefs": "true", "searchUSOnlyShipping": "false",
        "categoryLevelNo": "1", "categoryLevel": 1, "categoryId": 0, "partNumber": "", "catIds": "",
    }


def _search(text: str, page_size: int = 40, page: int = 1) -> list[dict]:
    body = _body(text, page, page_size)
    resp = requests.post(API_URL, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return (resp.json().get("searchResults") or {}).get("items") or []


def _money(value) -> Optional[str]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return f"${v:,.2f}".replace(".00", "") if v > 0 else None


def scrape_shopgoodwill(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    live_term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    terms = [live_term] if live_term else BROAD_TERMS
    manual_url = f"{SITE_URL}/categories/listing?st={requests.utils.quote(live_term or 'pro audio')}"

    items: dict[int, dict] = {}
    errors = []
    pages = 3 if live_term else 2
    for term in terms:
        for page in range(1, pages + 1):
            try:
                batch = _search(term, page=page)
            except Exception as e:
                errors.append(f"{term}: {e}")
                break
            for it in batch:
                items.setdefault(it.get("itemId"), it)
            if len(batch) < 40:
                break
            time.sleep(0.4)
    if not live_term:
        # Walk the audio categories newest-first until reaching lots listed
        # before the lookback window (everything newer than that is covered).
        light = bool(source.get("_light"))
        for cat, level in CATEGORIES:
            for page in range(1, (MAX_PAGES_LIGHT if light else MAX_PAGES_FULL) + 1):
                try:
                    batch = _browse(cat, level, page)
                except Exception as e:
                    errors.append(f"category {cat}: {e}")
                    break
                for it in batch:
                    items.setdefault(it.get("itemId"), it)
                if len(batch) < 39:
                    break
                time.sleep(0.4)
    if errors and not items:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error=f"ShopGoodwill search failed ({errors[0]})",
            fix_hint="Usually temporary — it should work next run.",
            duration_seconds=time.time() - start,
        )

    listings = []
    for item_id, it in items.items():
        title = (it.get("title") or "").strip()
        if not title or _NOT_AUDIO_CATEGORY.search(it.get("categoryName") or ""):
            continue
        if not keyword_match(title, keywords):
            continue
        buy_now = _money(it.get("buyNowPrice"))
        bid = _money(it.get("currentPrice"))
        bids = it.get("numBids") or 0
        ends = (it.get("endTime") or "")[:16].replace("T", " ")
        if buy_now:
            price, desc = buy_now, f"Buy Now · auction ends {ends}"
        else:
            price, desc = bid, f"Auction · current bid ({bids} bid{'s' if bids != 1 else ''}) · ends {ends}"
        ship = _money(it.get("shippingPrice"))
        if ship:
            desc += f" · shipping {ship}"
        posted_at = None
        if it.get("startTime"):
            try:
                posted_at = datetime.fromisoformat(it["startTime"])
            except ValueError:
                pass
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=f"{SITE_URL}/item/{item_id}",
            price=price,
            description=desc,
            image_url=it.get("imageURL"),
            posted_at=posted_at,
            listing_id=str(item_id),
        ))

    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
