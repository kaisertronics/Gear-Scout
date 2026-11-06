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
) -> tuple[list[ScrapeResult], list, dict]:
    """Scrapes every enabled source using cfg's standing keyword list,
    stores any new listings, and updates the same last_run.json snapshot a
    scheduled run would — so the Dashboard tab reflects it immediately —
    but never touches email."""
    from scrapers.learning import keywords_with_learned
    from scrapers.telex import _search_sites
    from scrapers.wanted import terms as wanted_terms
    started = datetime.now(timezone.utc).isoformat()
    keywords = keywords_with_learned(cfg)
    terms = wanted_terms(cfg)
    # The quick version (like the hourly refresh): Facebook Marketplace reads
    # each region's feed without the extra broad searches, and the slow
    # Facebook post search (which runs all day in the background) is left out
    # — a full scheduled-style pass takes 15–20 minutes.
    from scrapers.liquidation import with_defaults as _liq_defaults
    from scrapers.pawn_shops import with_defaults as _pawn_defaults
    sources = [{**s, "_light": True} for s in _pawn_defaults(_liq_defaults(cfg.get("sources", [])))
               if s.get("enabled", True) and s.get("type") != "facebook_posts"]
    from scrapers.fb_posts_background import facebook_paused
    if facebook_paused():  # Facebook asked the account to slow down
        sources = [s for s in sources if not str(s.get("type", "")).startswith("facebook")]
    total = len(sources) + len(terms)

    def feed_progress(done, _total, current):
        if on_progress:
            on_progress(done, total, current)
    results: list[ScrapeResult] = run_sources(sources, keywords, cfg, on_progress=feed_progress)

    all_listings = drop_excluded([l for r in results for l in r.listings], cfg, keywords)
    new_listings = filter_new(all_listings)

    # Then every Wanted (Telex List) term searched directly on Reverb, eBay,
    # Craigslist, Kijiji, ShopGoodwill, Long & McQuade and OfferUp — that's
    # where most of your ads come from. (Facebook: the all-day rotation.)
    for i, term in enumerate(terms):
        if on_progress:
            on_progress(len(sources) + i, total, f"Searching “{term}”")
        try:
            _search_sites(term, cfg, {"fast", "ebay"})
        except Exception:
            logger.exception("Scrape now: search failed for %r", term)

    try:
        from scrapers.enrich import build_price_index
        from scrapers.market import refresh_market_prices
        from scrapers.store import all_priced_rows
        # Only this scrape's new finds, briefly — the hourly refresh fills in
        # the rest, so "Scrape now" finishes right after scraping.
        refresh_market_prices([l.title for l in new_listings],
                              build_price_index(all_priced_rows()), max_lookups=15, pause=0.3, cfg=cfg)
    except Exception:
        logger.exception("Reverb market price refresh failed")

    write_run_status(
        run_number=0,
        run_time=datetime.now(timezone.utc),
        results=results,
        new_listings=new_listings,
        manual=True,
    )
    purge_old(days=90)  # same as scheduled runs (this used to drop everything over 30 days)

    if on_progress:
        on_progress(total, total, None)

    return results, new_listings, _wanted_new_since(cfg, started)


def _wanted_new_since(cfg: dict, since: str) -> dict:
    """How many listings found since `since` match your Wanted list (what
    shows up on the Feed), and how many were found in all."""
    from scrapers import wanted
    from scrapers.enrich import exclude_match, is_not_audio, title_says_sold
    from scrapers.store import _conn
    with _conn() as conn:
        rows = conn.execute("SELECT title, price, source_name FROM seen WHERE first_seen >= ? AND COALESCE(hidden, 0) = 0"
                            " AND COALESCE(duplicate, 0) = 0", (since,)).fetchall()
    ms, exclude = wanted.matchers(cfg), cfg.get("exclude_words") or []
    hits = sum(1 for t, p, src in rows if t and not exclude_match(t, exclude) and not is_not_audio(t)
               and not title_says_sold(t) and wanted.match(t, p, ms, src))
    return {"all": len(rows), "wanted": hits}


STATUS_PATH = "/data/manual_scrape_status.json"


def main() -> None:
    """Runs "Scrape now" as its own lower-priority process (the dashboard
    starts it), so a scrape never starves the web pages of CPU. Writes the
    progress file the pages poll."""
    import json
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    started = time.time()

    def write(status: dict) -> None:
        try:
            with open(STATUS_PATH, "w") as f:
                json.dump(status, f)
        except OSError:
            pass

    def on_progress(done, total, current_source):
        write({"state": "running", "started": started, "done": done, "total": total,
               "current_source": current_source})
    try:
        with open("/config/config.yaml") as f:
            cfg = yaml.safe_load(f) or {}
        results, _new, found = run_manual_scrape(cfg, on_progress=on_progress)
        open("/data/manual_scrape_finished", "a").close()
        import os
        os.utime("/data/manual_scrape_finished")  # pages rebuild once, now
        write({"state": "done", "new_count": found["all"], "wanted_new": found["wanted"],
               "sources_ok": sum(1 for r in results if r.success or r.blocked), "sources_total": len(results)})
    except Exception as e:
        logger.exception("Manual scrape failed")
        write({"state": "failed", "reason": str(e)})


if __name__ == "__main__":
    main()
