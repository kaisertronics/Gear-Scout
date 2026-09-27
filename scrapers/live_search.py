"""
On-demand live search — re-scrapes every enabled source right now, filtered
by a one-off search phrase instead of config.yaml's standing keyword list.
Unlike the dashboard's regular search box (which only searches listings
already collected by a past scheduled run), this hits the real sites live,
so it can surface something posted minutes ago that hasn't been through a
scheduled run yet.

Slow by nature (several sources use a full headless-browser fetch) — callers
should run this in a background thread and poll `on_progress` state rather
than block a request on it.
"""
import logging
import time
from typing import Callable, Optional

from scrapers.enrich import drop_excluded
from scrapers.base import ScrapeResult
from scrapers.dispatch import dispatch_scrape
from scrapers.store import mark_seen

logger = logging.getLogger(__name__)


def run_live_search(
    query: str,
    cfg: dict,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> list[ScrapeResult]:
    """Scrapes every enabled source live, filtering by `query` as if it were
    the only keyword. Matches are recorded via mark_seen(live_only=True) so
    they show up on the Search page (not the Dashboard, since a one-off
    phrase can match anything) and don't get re-notified by a future
    scheduled run's email — but ALL current matches are returned here, not
    just ones that are new."""
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]

    # A live, on-demand search should mean "search everything right now" —
    # if the user has removed or disabled one of the 3 Craigslist regions
    # (e.g. to lighten scheduled runs), add whichever ones are missing back
    # in just for this live search, so it always covers the whole country
    # regardless of that toggle. Nothing here is written back to config.yaml.
    from scrapers.html_scraper import default_craigslist_region_sources

    existing_region_names = {s.get("name") for s in sources if s.get("type") == "craigslist_region"}
    for region_source in default_craigslist_region_sources():
        if region_source["name"] not in existing_region_names:
            sources = sources + [region_source]

    keywords = [query]
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
            logger.exception("Live search: unhandled exception scraping %s", name)
            result = ScrapeResult(
                source_name=name,
                source_url=source.get("url", ""),
                success=False,
                error=str(e),
                duration_seconds=time.time() - start,
            )
        result.listings = drop_excluded(result.listings, cfg)
        results.append(result)
        for listing in result.listings:
            mark_seen(listing, live_only=True)

    if on_progress:
        on_progress(len(sources), len(sources), None)

    return results
