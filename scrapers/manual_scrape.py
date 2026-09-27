"""
The dashboard's "Scrape now" button — runs one full scrape cycle across
every enabled source using config.yaml's own keyword list (same as a
scheduled run), storing any new listings — but skips sending the email
digest entirely. Written to run in a background thread (same pattern as
live_search.py) with progress polling, since a full cycle across every
source takes a couple of minutes.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from scrapers.enrich import drop_excluded
from scrapers.base import ScrapeResult
from scrapers.runner import run_sources
from scrapers.run_status import write_run_status
from scrapers.store import filter_new, purge_old

logger = logging.getLogger(__name__)


def run_manual_scrape(
    cfg: dict,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> tuple[list[ScrapeResult], list]:
    """Scrapes every enabled source using cfg's standing keyword list,
    stores any new listings, and updates the same last_run.json snapshot a
    scheduled run would — so the Dashboard tab reflects it immediately —
    but never touches email."""
    keywords = cfg.get("keywords", [])
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    results: list[ScrapeResult] = run_sources(sources, keywords, cfg, on_progress=on_progress)

    all_listings = drop_excluded([l for r in results for l in r.listings], cfg)
    new_listings = filter_new(all_listings)

    try:
        from scrapers.enrich import build_price_index
        from scrapers.market import refresh_market_prices
        from scrapers.store import all_priced_rows
        refresh_market_prices([l.title for l in all_listings],
                              build_price_index(all_priced_rows()), max_lookups=40)
    except Exception:
        logger.exception("Reverb market price refresh failed")

    write_run_status(
        run_number=0,
        run_time=datetime.now(timezone.utc),
        results=results,
        new_listings=new_listings,
        manual=True,
    )
    purge_old(days=30)

    if on_progress:
        on_progress(len(sources), len(sources), None)

    return results, new_listings
