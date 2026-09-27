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
from scrapers.dispatch import dispatch_scrape
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
    results: list[ScrapeResult] = []

    for i, source in enumerate(sources):
        name = source["name"]
        if on_progress:
            on_progress(i, len(sources), name)
        start = time.time()
        try:
            result = dispatch_scrape(source, keywords, cfg)
            if result is None:
                continue
        except Exception as e:
            logger.exception("Manual scrape: unhandled exception scraping %s", name)
            result = ScrapeResult(
                source_name=name,
                source_url=source.get("url", ""),
                success=False,
                error=str(e),
                fix_hint="Unhandled exception — check the Docker logs for a full traceback.",
                duration_seconds=time.time() - start,
            )
        results.append(result)

    all_listings = drop_excluded([l for r in results for l in r.listings], cfg)
    new_listings = filter_new(all_listings)

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
