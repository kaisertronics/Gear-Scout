"""
Municibid (municibid.com) — US government surplus auctions (school districts,
towns, universities, police). Audio gear is rare there but sells for very little.

The browse page `/browse?search=<term>` is server-rendered, so plain HTTP is
enough: each result is an `<article class="listing-row">` card (title, town,
agency, bid count, current bid) and the page's embedded Next.js data carries
the exact end time (`endsAtUtc`). No account or browser needed.

A scheduled run searches a short set of audio terms (a handful of requests,
cached for a while) and filters the results against the full keyword list like
every other source. A live search sends the one term directly.

Items are auctions: the price is the current bid, labelled in the description
like HiBid / ShopGoodwill ("Auction · current bid (N bids) · ends ...", Pacific
time). US only (all Municibid agencies are US).

No logins and no bot-check workarounds: on a challenge, 403 or 429 the source
steps aside for several hours.
"""
import html as htmllib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")

SITE_URL = "https://municibid.com"
SEARCH_URL = SITE_URL + "/browse"
HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
BROAD_TERMS = ["microphone", "preamp", "compressor", "studio", "audio", "mixer"]
LIGHT_TERMS = 3           # hourly refreshes search only the first few
CACHE_SECONDS = 30 * 60
PAUSE_HOURS = 6
MIN_GAP_SECONDS = 2.0

_lock = threading.Lock()
_cache: dict[str, tuple[float, str]] = {}
_paused_until = 0.0
_last_request = 0.0

_CARD = re.compile(r'<article class="listing-row">(.*?)</article>', re.S)
_TITLE = re.compile(r'listing-row__title"><a href="(/listing/(\d+)/[^"]*)">(.*?)</a>', re.S)
_WHERE = re.compile(r'listing-row__where"><span>(.*?)</span>', re.S)
_AGENCY = re.compile(r'listing-row__agency"[^>]*>(.*?)</a>', re.S)
_BIDS = re.compile(r'Bid\(s\):</strong>\s*(?:<!-- -->)?\s*(\d+)')
_PRICE = re.compile(r'(?:Current|Starting|Opening)\s+Bid:</strong>\s*(?:<!-- -->)?\s*\$([\d,]+(?:\.\d+)?)')
_IMG = re.compile(r'<img src="([^"]+)"')
_ENDS = re.compile(r'\\"id\\":(\d+),\\"title\\".{0,600}?\\"endsAtUtc\\":\\"([0-9T:\-]+)\\"', re.S)


class MunicibidBlocked(Exception):
    """Municibid answered with a challenge / 403 / 429."""


def _fetch(term: str) -> str:
    """The first browse page of open auctions for a term (cached)."""
    global _paused_until, _last_request
    key = term.lower()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        if time.time() < _paused_until:
            raise MunicibidBlocked("paused after a bot check")
        wait = MIN_GAP_SECONDS - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(SEARCH_URL, params={"search": term}, headers=HEADERS, timeout=30)
        finally:
            _last_request = time.time()
        head = resp.text[:2000].lower()
        if resp.status_code in (401, 403, 429) or "captcha" in head or "incapsula" in head \
                or "just a moment" in head:
            _paused_until = time.time() + PAUSE_HOURS * 3600
            raise MunicibidBlocked(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        _cache[key] = (time.time(), resp.text)
        return resp.text


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_listings(page: str, name: str) -> list[Listing]:
    """Listings for every auction card on a browse page."""
    ends = {m.group(1): m.group(2) for m in _ENDS.finditer(page)}
    out = []
    for card in _CARD.findall(page):
        t = _TITLE.search(card)
        if not t:
            continue
        path, lot_id, title = t.group(1), t.group(2), _clean(t.group(3))
        p = _PRICE.search(card)
        if not title or not p:
            continue
        price = "$" + p.group(1).replace(".00", "")
        b = _BIDS.search(card)
        bids = int(b.group(1)) if b else 0
        desc = f"Auction · current bid ({bids} bid{'s' if bids != 1 else ''})"
        iso = ends.get(lot_id)
        if iso:
            try:
                when = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
                if when <= datetime.now(timezone.utc):
                    continue
                desc += " · ends " + when.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        desc += " · pickup"
        a = _AGENCY.search(card)
        if a and _clean(a.group(1)):
            desc += f" · {truncate(_clean(a.group(1)), 40)}"
        w = _WHERE.search(card)
        img = _IMG.search(card)
        out.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=SITE_URL + path,
            price=price,
            description=desc,
            image_url=img.group(1) if img else None,
            listing_id=lot_id,
            location=_clean(w.group(1)) if w else None,
            country="US",
        ))
    return out


def scrape_municibid(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    live_term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    light = bool(source.get("_light"))
    terms = [live_term] if live_term else BROAD_TERMS[:LIGHT_TERMS] if light else BROAD_TERMS
    manual_url = f"{SEARCH_URL}?search={requests.utils.quote(live_term or 'microphone')}"

    found: dict[str, Listing] = {}
    errors = []
    for term in terms:
        try:
            for listing in parse_listings(_fetch(term), name):
                found.setdefault(listing.listing_id, listing)
        except MunicibidBlocked as e:
            logger.warning("Municibid not searched (%s)", e)
            return ScrapeResult(
                source_name=name, source_url=manual_url, success=False, blocked=True,
                error="Municibid asked for a bot check, so it is being left alone for a few hours.",
                fix_hint="Nothing to do — it retries on its own. Use the manual link meanwhile.",
                duration_seconds=time.time() - start,
            )
        except Exception as e:
            logger.warning("Municibid search %r failed: %s", term, e)
            errors.append(f"{term}: {e}")
    if errors and not found:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error=f"Municibid search failed ({errors[0]})",
            fix_hint="Usually temporary — it should work next run.",
            duration_seconds=time.time() - start,
        )

    listings = [l for l in found.values() if keyword_match(l.title, keywords)]
    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
