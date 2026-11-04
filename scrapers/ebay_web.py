"""Backup for eBay when the Browse API allowance is spent or eBay is paused
(Nov 3: the allowance ran out mid-afternoon and the Hosa DTP-802 / STX-805
deals were missed). Reads eBay's plain web search page (Buy It Now, newest
first, US sellers) with ordinary polite requests:

- at most one request every 3 seconds, across all threads;
- the fast lane reads a couple of its terms (mics, compressors, preamps) each
  tick; the shopping-list / Telex terms and the starred clone makers get a few
  searches every 15 minutes, shopping list first;
- no CAPTCHA or challenge is ever solved: on a block or challenge page (or a
  403 / 429 / 503) eBay's web search is left alone for 30 minutes.

Listings go through the same pipeline as API results (the caller returns them
as a ScrapeResult)."""
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

import requests

from .base import ScrapeResult

logger = logging.getLogger(__name__)

SPACING_S = 3.0
BLOCK_PAUSE_MIN = 30
WINDOW_S = 15 * 60
CAPS = {"shop": 6, "maker": 2}  # web searches per 15 minutes besides the fast lane
FAST_PER_TICK = 2
FAST_TERMS = ["microphone", "tube microphone", "ribbon microphone", "condenser microphone",
              "compressor", "limiter", "preamp", "tube preamp"]
_BLOCK_FILE = "/data/ebay_web_blocked_until.txt"
_BLOCK_MARKERS = ("pardon our interruption", "captcha", "verify yourself", "security measure",
                  "access denied", "splashui/challenge", "checking your browser", "robot or human")

_lock = threading.Lock()
_last_request = 0.0
_recent: dict[str, deque] = {"shop": deque(), "maker": deque()}
_fast_i = 0


def search_url(term: str) -> str:
    return (f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(term.strip())}"
            "&LH_BIN=1&_sop=10&LH_PrefLoc=1&LH_ItemCondition=1500%7C2500%7C3000")


def blocked_until() -> Optional[datetime]:
    try:
        until = datetime.fromisoformat(open(_BLOCK_FILE).read().strip())
        return until if until > datetime.now(timezone.utc) else None
    except Exception:
        return None


def _note_block(why: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=BLOCK_PAUSE_MIN)
    try:
        with open(_BLOCK_FILE, "w") as f:
            f.write(until.isoformat())
    except OSError:
        pass
    logger.warning("eBay web search backup: %s — leaving eBay's website alone for %d minutes", why, BLOCK_PAUSE_MIN)


def _fetch(term: str) -> Optional[str]:
    """The results page HTML, or None (blocked / failed). One request, at least 3 s after the last."""
    global _last_request
    from .html_scraper import HEADERS
    with _lock:
        if blocked_until():
            return None
        wait = SPACING_S - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            resp = requests.get(search_url(term), headers=HEADERS, timeout=20)
        except Exception as e:
            logger.info("eBay web search backup failed for %r: %s", term, e)
            return None
    if resp.status_code in (403, 429, 503):
        _note_block(f"HTTP {resp.status_code}")
        return None
    if resp.status_code != 200:
        return None
    low = resp.text[:6000].lower()
    if any(m in low for m in _BLOCK_MARKERS) or "error page" in low:
        _note_block("challenge page")
        return None
    return resp.text


def parse(html: str, name: str, keywords: Optional[list]) -> list:
    from bs4 import BeautifulSoup
    from .html_scraper import ebay_cards_to_listings
    out = ebay_cards_to_listings(BeautifulSoup(html, "html.parser").select("li.s-card"), name, keywords)
    for l in out:
        l.url = l.url.split("?")[0]  # eBay adds per-search tracking to the link
    return out


def search(term: str, name: str, keywords: Optional[list]) -> Optional[list]:
    html = _fetch(term)
    return None if html is None else parse(html, name, keywords)


def _allowed(kind: str) -> bool:
    now = time.time()
    q = _recent[kind]
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    if len(q) >= CAPS[kind]:
        return False
    q.append(now)
    return True


def term_kind(term: str, job: str) -> Optional[str]:
    """"shop" for the owner's Telex / shopping-list terms, "maker" for the starred clone makers, else None."""
    if job == "priority_sweep":
        return "shop"
    try:
        from .clone_makers import sibling_terms
        if (term or "").strip().lower() in {t.lower() for t in sibling_terms()}:
            return "maker"
    except Exception:
        pass
    return None


def fallback(name: str, manual_url: str, *, newest: int = 0, term: str = "", job: str = "") -> Optional[ScrapeResult]:
    """A successful ScrapeResult from eBay's web search, or None when the backup
    doesn't apply (term not the owner's, over this window's limit, blocked, nothing readable)."""
    global _fast_i
    start = time.time()
    if blocked_until():
        return None
    if newest:  # fast lane: a couple of terms a tick, rotating
        found, got = {}, False
        for _ in range(FAST_PER_TICK):
            t = FAST_TERMS[_fast_i % len(FAST_TERMS)]
            _fast_i += 1
            ls = search(t, name, None)
            if ls is None:
                continue
            got = True
            for l in ls:
                found.setdefault(l.url, l)
        if not got:
            return None
        return ScrapeResult(source_name=name, source_url=manual_url, success=True,
                            listings=list(found.values()), duration_seconds=time.time() - start)
    kind = term_kind(term, job)
    if not term or not kind or not _allowed(kind):
        return None
    ls = search(term, name, [term])
    if ls is None:
        return None
    return ScrapeResult(source_name=name, source_url=manual_url, success=True, listings=ls,
                        duration_seconds=time.time() - start)
