"""
HiBid (hibid.com) — thousands of US auction houses, estate and surplus sales —
via the public GraphQL endpoint its own lot search page uses. No account or
browser needed; US lots only.

A scheduled run searches a short set of broad audio terms, ending soonest
first (a few requests, results cached for a while), and filters the lots
against the full keyword list like every other source. A live search sends
the one term directly.

Items are auctions: the price is the current bid (the opening bid while there
are no bids), labelled in the description like ShopGoodwill and eBay auctions
("Auction · current bid (N bids) · ends YYYY-MM-DD HH:MM", Pacific time).

No logins and no bot-check workarounds: if the site ever answers with a
challenge, 403 or 429, the source steps aside for several hours.
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")

GRAPHQL_URL = "https://hibid.com/graphql"
SITE_URL = "https://hibid.com"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin": SITE_URL,
    "Referer": SITE_URL + "/",
    "site_subdomain": "hibid.com",
}
BROAD_TERMS = [
    "microphone", "preamp", "mixing console", "audio interface", "studio monitor",
    "compressor limiter", "reel to reel", "tube amplifier",
]
LIGHT_TERMS = 4           # hourly refreshes search only the first few
PAGE_SIZE = 100
CACHE_SECONDS = 15 * 60   # the same search is reused by refreshes, full runs and live searches
PAUSE_HOURS = 6           # how long to leave HiBid alone after a challenge / 403 / 429
MIN_GAP_SECONDS = 1.5     # between requests

_QUERY = (
    "query LotSearch($pageNumber: Int!, $pageLength: Int!, $searchText: String, $status: AuctionLotStatus, "
    "$sortOrder: EventItemSortOrder, $countryName: String) { lotSearch(input: {searchText: $searchText, "
    "status: $status, sortOrder: $sortOrder, countryName: $countryName, countAsView: false, "
    "filter: ALL, isArchive: false}, pageNumber: $pageNumber, pageLength: $pageLength, sortDirection: DESC) "
    "{ pagedResults { totalCount results { id lead description shippingOffered featuredPicture { thumbnailLocation } "
    "auction { id eventName eventCity eventState auctioneer { name country } } "
    "lotState { highBid minBid bidCount timeLeftSeconds isClosed buyNow } } } } }"
)

_US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_paused_until = 0.0
_last_request = 0.0


class HiBidBlocked(Exception):
    """HiBid answered with a challenge / 403 / 429 / login wall."""


def _search(term: str, page: int = 1) -> list[dict]:
    """One page of open US lots for a term, soonest ending first (cached)."""
    global _paused_until, _last_request
    key = (term.lower(), page)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        if time.time() < _paused_until:
            raise HiBidBlocked("paused after a bot check")
        wait = MIN_GAP_SECONDS - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        body = {"operationName": "LotSearch", "query": _QUERY, "variables": {
            "pageNumber": page, "pageLength": PAGE_SIZE, "searchText": term, "status": "OPEN",
            "sortOrder": "TIME_LEFT", "countryName": "United States"}}
        try:
            resp = requests.post(GRAPHQL_URL, json=body, headers=HEADERS, timeout=30)
        finally:
            _last_request = time.time()
        if resp.status_code in (401, 403, 429) or "captcha" in resp.text[:2000].lower() \
                or "incapsula" in resp.text[:2000].lower():
            _paused_until = time.time() + PAUSE_HOURS * 3600
            raise HiBidBlocked(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        lots = (((resp.json().get("data") or {}).get("lotSearch") or {}).get("pagedResults") or {}).get("results") or []
        _cache[key] = (time.time(), lots)
        return lots


def _is_us(lot: dict) -> bool:
    auction = lot.get("auction") or {}
    country = ((auction.get("auctioneer") or {}).get("country") or "").strip()
    if country:
        return country.lower().startswith("united states")
    return (auction.get("eventState") or "").upper() in _US_STATES


def _money(value) -> Optional[str]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return f"${v:,.2f}".replace(".00", "") if v > 0 else None


def lot_listing(lot: dict, name: str, now: Optional[datetime] = None) -> Optional[Listing]:
    """A Listing for one open US lot (None for closed / non-US / unpriced
    lots): the price is the current bid and the description says when it ends."""
    state = lot.get("lotState") or {}
    lead = re.sub(r"\s+", " ", lot.get("lead") or "").strip()
    seconds = state.get("timeLeftSeconds")
    if not lead or state.get("isClosed") or not isinstance(seconds, (int, float)) or seconds <= 0 or not _is_us(lot):
        return None
    bid = _money(state.get("highBid")) or _money(state.get("minBid"))
    if not bid:
        return None
    now = now or datetime.now(timezone.utc)
    ends = (now + timedelta(seconds=seconds)).astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M")
    bids = state.get("bidCount") or 0
    auction = lot.get("auction") or {}
    where = ", ".join(p for p in (auction.get("eventCity"), auction.get("eventState")) if p)
    desc = f"Auction · current bid ({bids} bid{'s' if bids != 1 else ''}) · ends {ends}"
    if _money(state.get("buyNow")):
        desc += f" · buy now {_money(state['buyNow'])}"
    desc += " · shipping offered" if lot.get("shippingOffered") else " · pickup"
    house = (auction.get("auctioneer") or {}).get("name")
    if house:
        desc += f" · {truncate(house, 40)}"
    # A short lead ("Studio microphone") often has the brand and model in the
    # description; the title is what the matching reads.
    title = lead
    detail = re.sub(r"<[^>]+>|&nbsp;", " ", lot.get("description") or "")
    detail = re.sub(r"\s+", " ", detail).strip()
    if len(lead) < 40 and detail:
        title = f"{lead} — {detail}"
    return Listing(
        source_name=name,
        title=truncate(title, 120),
        url=f"{SITE_URL}/lot/{lot['id']}/",
        price=bid,
        description=desc,
        image_url=(lot.get("featuredPicture") or {}).get("thumbnailLocation") or None,
        listing_id=str(lot["id"]),
        location=where or None,
        country="US",
    )


def scrape_hibid(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    live_term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    light = bool(source.get("_light"))
    terms = [live_term] if live_term else BROAD_TERMS[:LIGHT_TERMS] if light else BROAD_TERMS
    manual_url = f"{SITE_URL}/lots?q={requests.utils.quote(live_term or 'microphone')}"

    lots: dict[int, dict] = {}
    errors = []
    for term in terms:
        try:
            for lot in _search(term):
                lots.setdefault(lot["id"], lot)
        except HiBidBlocked as e:
            logger.warning("HiBid not searched (%s)", e)
            return ScrapeResult(
                source_name=name, source_url=manual_url, success=False, blocked=True,
                error="HiBid asked for a bot check, so it is being left alone for a few hours.",
                fix_hint="Nothing to do — it retries on its own. Use the manual link meanwhile.",
                duration_seconds=time.time() - start,
            )
        except Exception as e:
            logger.warning("HiBid search %r failed: %s", term, e)
            errors.append(f"{term}: {e}")
    if errors and not lots:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error=f"HiBid search failed ({errors[0]})",
            fix_hint="Usually temporary — it should work next run.",
            duration_seconds=time.time() - start,
        )

    listings = []
    for lot in lots.values():
        listing = lot_listing(lot, name)
        if listing and keyword_match(listing.title, keywords):
            listings.append(listing)

    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
