"""
eBay results via eBay's official Browse API instead of scraping the site
(which Akamai blocks outright for headless browsers).

Needs a free eBay developer "Production" keyset (App ID + Cert ID), stored
in config.yaml under `ebay_api:`. Without one, dispatch falls back to the
old scraper, which reports eBay as "manual check only".

Docs: https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search
"""
import base64
import logging
import re
import time
from datetime import datetime
from typing import Optional

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
PRO_AUDIO_CATEGORY = "180014"
# 1500 Open box (eBay's B-stock equivalent), 2500 Seller refurbished,
# 3000 Used — same set as the manual-check eBay link.
CONDITION_FILTER = "conditionIds:{1500|2500|3000}"

_token_cache: dict[str, tuple[str, float]] = {}


# eBay allows a fixed number of searches per day (about 5,000). When it says
# "too many requests", every eBay call pauses until the daily reset (midnight
# Pacific) instead of failing over and over. Shared by the scraper and the
# dashboard through the data folder.
_PAUSE_FILE = "/data/ebay_paused_until.txt"


def ebay_paused_until() -> Optional[datetime]:
    from datetime import timezone
    try:
        until = datetime.fromisoformat(open(_PAUSE_FILE).read().strip())
        return until if until > datetime.now(timezone.utc) else None
    except Exception:
        return None


def _note_rate_limit() -> None:
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    pacific = datetime.now(ZoneInfo("America/Los_Angeles"))
    reset = (pacific + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    try:
        with open(_PAUSE_FILE, "w") as f:
            f.write(reset.astimezone(timezone.utc).isoformat())
    except OSError:
        pass
    logger.warning("eBay daily request limit reached — pausing eBay until %s", reset.strftime("%b %d %I:%M %p PT"))


def _check_response(resp) -> None:
    if resp.status_code == 429:
        _note_rate_limit()
        raise RuntimeError("eBay's daily request limit is used up — eBay resumes after midnight (Pacific).")


def _get_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise PermissionError(
            f"eBay rejected the API keys (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    token = data["access_token"]
    _token_cache[client_id] = (token, time.time() + int(data.get("expires_in", 7200)))
    return token


def _format_price(price: Optional[dict]) -> Optional[str]:
    if not price or "value" not in price:
        return None
    try:
        amount = float(price["value"])
    except (TypeError, ValueError):
        return None
    symbol = "$" if price.get("currency", "USD") == "USD" else f"{price.get('currency')} "
    return f"{symbol}{amount:,.0f}" if amount == int(amount) else f"{symbol}{amount:,.2f}"


def scrape_ebay_api(source: dict, keywords: list[str], api_cfg: dict) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    # A single term (live search) goes to eBay's own search; the full keyword
    # list (scheduled runs) fetches the newest listings in the category and
    # filters locally — one API call per run instead of one per keyword.
    is_live_search = len(keywords) == 1 and keywords[0].strip()
    params = {
        "category_ids": PRO_AUDIO_CATEGORY,
        "filter": CONDITION_FILTER,
        "sort": "newlyListed",
        "limit": "200",
    }
    if is_live_search:
        params["q"] = keywords[0].strip()
    manual_url = (
        "https://www.ebay.com/sch/i.html?_sacat=180014"
        "&LH_ItemCondition=1500%7C2500%7C3000&_sop=10"
        + (f"&_nkw={requests.utils.quote(keywords[0].strip())}" if is_live_search else "")
    )

    paused = ebay_paused_until()
    if paused:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False, blocked=True,
            error="eBay's daily request limit is used up for today.",
            fix_hint="Resumes on its own after midnight (Pacific). Nothing to fix.",
            duration_seconds=time.time() - start,
        )
    try:
        token = _get_token(api_cfg["client_id"].strip(), api_cfg["client_secret"].strip())
        resp = requests.get(
            SEARCH_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            timeout=30,
        )
        _check_response(resp)
        if resp.status_code != 200:
            raise RuntimeError(f"eBay search failed (HTTP {resp.status_code}): {resp.text[:200]}")
        items = resp.json().get("itemSummaries", []) or []
    except PermissionError as e:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False, error=str(e),
            fix_hint="Check the eBay App ID and Cert ID in Settings → eBay (use the Production keyset, not Sandbox).",
            duration_seconds=time.time() - start,
        )
    except Exception as e:
        logger.warning("eBay API error for %s: %s", name, e)
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False, error=str(e),
            fix_hint="Usually temporary — it should work again next run.",
            duration_seconds=time.time() - start,
        )

    listings = []
    for item in items:
        title = item.get("title") or ""
        if not title or not keyword_match(title, keywords):
            continue
        item_id = item.get("legacyItemId") or re.sub(r"\W", "", item.get("itemId", ""))
        posted_at = None
        if item.get("itemCreationDate"):
            try:
                posted_at = datetime.fromisoformat(item["itemCreationDate"].replace("Z", "+00:00"))
            except ValueError:
                pass
        # Auctions: the price is the current bid and the end time matters.
        from scrapers.auctions import ebay_auction_listing
        auction = ebay_auction_listing(item, name)
        if auction:
            auction.posted_at = posted_at
            listings.append(auction)
            continue
        condition = item.get("condition")
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=item.get("itemWebUrl") or f"https://www.ebay.com/itm/{item_id}",
            price=_format_price(item.get("price")),
            description=condition,
            image_url=(item.get("image") or {}).get("imageUrl"),
            posted_at=posted_at,
            listing_id=item_id,
        ))

    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )


def ebay_manual_lowest_url(query: str) -> str:
    """eBay website search for `query`: Buy It Now, used / open box /
    refurbished, sorted by price + shipping, lowest first."""
    return ("https://www.ebay.com/sch/i.html?_sacat=0&LH_BIN=1"
            "&LH_ItemCondition=1500%7C2500%7C3000&_sop=15"
            f"&_nkw={requests.utils.quote(query.strip())}")


def ebay_lowest(query: str, api_cfg: dict, name: str = "eBay") -> list[Listing]:
    """Cheapest Buy-It-Now listings for `query` on eBay (any category — a
    price-sorted search needs the whole site, not just the newest items).
    Auctions are left out: a current bid isn't what it will sell for."""
    if ebay_paused_until():
        return []
    token = _get_token(api_cfg["client_id"].strip(), api_cfg["client_secret"].strip())
    resp = requests.get(
        SEARCH_URL,
        params={"q": query.strip(), "sort": "price", "limit": "200",
                "filter": f"{CONDITION_FILTER},buyingOptions:{{FIXED_PRICE}},priceCurrency:USD"},
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        timeout=30,
    )
    _check_response(resp)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("itemSummaries", []) or []:
        title = item.get("title") or ""
        if not title or not keyword_match(title, [query]):
            continue
        item_id = item.get("legacyItemId") or re.sub(r"\W", "", item.get("itemId", ""))
        loc = item.get("itemLocation") or {}
        location = ", ".join(p for p in (loc.get("city"), loc.get("stateOrProvince")) if p) or None
        out.append(Listing(
            source_name=name, title=truncate(title, 120),
            url=item.get("itemWebUrl") or f"https://www.ebay.com/itm/{item_id}",
            price=_format_price(item.get("price")), description=item.get("condition"),
            image_url=(item.get("image") or {}).get("imageUrl"), listing_id=item_id,
            location=location,
        ))
    return out
