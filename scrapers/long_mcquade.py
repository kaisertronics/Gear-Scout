"""
Long & McQuade used gear ("GearHunter"), Pro Audio department — plain HTML,
no browser needed. The page lists the newest used items first (highest item
ids); PerPage=64 is the largest page it serves, and further pages are loaded
by JavaScript, so a scheduled run reads the newest 64 and filters by the
keyword list. A live search uses GearHunter's own keyword box
(ProductSearchTxt).

Prices are Canadian dollars (shown "C$…"; converted for comparisons).
"""
import logging
import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

BASE = "https://www.long-mcquade.com"
LIST_URL = BASE + "/GearHunter/pro-audio/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "saskatchewan": "SK",
}


def _cad(text: Optional[str]) -> Optional[str]:
    m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return f"C${value:,.0f}" if value == int(value) else f"C${value:,.2f}"


def scrape_long_mcquade(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    start = time.time()
    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    params = {"PerPage": 64}
    if term:
        params["ProductSearchTxt"] = term
    manual_url = LIST_URL + (f"?ProductSearchTxt={requests.utils.quote(term)}" if term else "")

    try:
        resp = requests.get(LIST_URL, params=params, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        # The site declares UTF-8 but serves Windows-1252 bytes (e.g. "é").
        html = resp.content.decode("cp1252", errors="replace")
    except Exception as e:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error=f"Long & McQuade fetch failed: {e}",
            fix_hint="Usually temporary — it should work next run.",
            duration_seconds=time.time() - start,
        )

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("a.products-item-link[href*='/GearHunter/']")
    if not cards and "GearHunter" not in html:
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error="Couldn't find any GearHunter items on the page.",
            fix_hint="Long & McQuade may have changed their layout — check scrapers/long_mcquade.py.",
            duration_seconds=time.time() - start,
        )

    listings, seen = [], set()
    for a in cards:
        m = re.search(r"/GearHunter/(\d+)/", a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        title = (a.get("title") or "").strip()
        # "Sennheiser - MD 421-II" -> "Sennheiser MD 421-II"
        title = re.sub(r"\s+-\s+", " ", title, count=1)
        if not title or not keyword_match(title, keywords):
            continue
        sale = a.select_one(".text-red")
        text = a.get_text(" ", strip=True)
        price = _cad(sale.get_text() if sale else None) or _cad(re.search(r"Price:\s*\$[\d,.]+", text).group(0) if re.search(r"Price:\s*\$[\d,.]+", text) else None)
        regular = re.search(r"Regular Price:\s*(\$[\d,.]+)", text)
        # The store's town is the line right before its province (the card's
        # mobile title uses the same small-text class, so it can't be "the
        # first small line").
        loc_parts = [p.get_text(strip=True).rstrip(",") for p in a.select("p.fs-7")]
        location = None
        for i, part in enumerate(loc_parts):
            if part.lower() in _PROVINCES and i > 0 and loc_parts[i - 1]:
                location = f"{loc_parts[i - 1]}, {_PROVINCES[part.lower()]}"
                break
        img = a.select_one("img")
        desc = "Used"
        if regular:
            desc += f" · regular {_cad(regular.group(1))}"
        if location:
            desc += f" · at the {location} store"
        listings.append(Listing(
            source_name=name,
            title=truncate(title, 120),
            url=BASE + a["href"],
            price=price,
            description=desc,
            image_url=img.get("src") if img else None,
            listing_id=m.group(1),
            location=location,
        ))

    return ScrapeResult(
        source_name=name, source_url=manual_url, success=True,
        listings=listings, duration_seconds=time.time() - start,
    )
