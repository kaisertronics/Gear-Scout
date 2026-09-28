"""
Runs a set of sources concurrently instead of one after another — shared by
scheduled runs, "Scrape now", live search and saved searches.

Facebook sources get their own small pool (3 at a time) where each worker
keeps one browser open for all the Facebook pages it handles: opening all
of them at once on the user's account would look far more bot-like, and a
fresh browser per page was most of the wait. Everything else runs in a
wider pool.
"""
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from scrapers.base import ScrapeResult
from scrapers.dispatch import dispatch_scrape

logger = logging.getLogger(__name__)

FB_TYPES = ("facebook", "facebook_marketplace_region", "facebook_posts")
FB_WORKERS = 3
# Docker Desktop commonly gets 4 CPUs; several of these also run a
# headless browser, so more workers mostly means more contention.
OTHER_WORKERS = 4


def _scrape_one(source: dict, keywords: list[str], cfg: dict) -> Optional[ScrapeResult]:
    start = time.time()
    try:
        return dispatch_scrape(source, keywords, cfg)
    except Exception as e:
        logger.exception("Unhandled exception scraping %s", source.get("name"))
        return ScrapeResult(
            source_name=source["name"],
            source_url=source.get("url", ""),
            success=False,
            error=str(e),
            fix_hint="Unhandled exception — check the Docker logs for a full traceback.",
            duration_seconds=time.time() - start,
        )


def run_sources(
    sources: list[dict],
    keywords: list[str],
    cfg: dict,
    on_progress: Optional[Callable[[int, int, Optional[str]], None]] = None,
    fb_workers: Optional[int] = None,
    other_workers: Optional[int] = None,
) -> list[ScrapeResult]:
    """Scrapes every source and returns results in the same order as
    `sources` (unrecognized source types are dropped, as before).
    on_progress(done, total, name_just_started) may be called from worker
    threads, but never concurrently."""
    perf = cfg.get("performance") or {}
    fb_workers = max(1, int(fb_workers or perf.get("facebook_workers") or FB_WORKERS))
    other_workers = max(1, int(other_workers or perf.get("other_workers") or OTHER_WORKERS))
    total = len(sources)
    results: list[Optional[ScrapeResult]] = [None] * total
    progress_lock = threading.Lock()
    done = [0]

    def report(started: Optional[str] = None, finished: bool = False):
        if not on_progress:
            return
        with progress_lock:
            if finished:
                done[0] += 1
            on_progress(done[0], total, started)

    def run(i: int):
        report(started=sources[i]["name"])
        results[i] = _scrape_one(sources[i], keywords, cfg)
        report(finished=True)

    fb_idx = [i for i, s in enumerate(sources) if s.get("type") in FB_TYPES]
    other_idx = [i for i, s in enumerate(sources) if s.get("type") not in FB_TYPES]

    fb_queue: "queue.Queue[int]" = queue.Queue()
    for i in fb_idx:
        fb_queue.put(i)

    def fb_worker():
        from scrapers.facebook_scraper import fb_browser_session
        try:
            with fb_browser_session():
                while True:
                    try:
                        i = fb_queue.get_nowait()
                    except queue.Empty:
                        return
                    run(i)
        except Exception:
            # Couldn't even start a browser — still produce a result for
            # every remaining Facebook source rather than silently skipping.
            logger.exception("Facebook worker failed to start")
            while True:
                try:
                    i = fb_queue.get_nowait()
                except queue.Empty:
                    return
                run(i)

    fb_threads = [
        threading.Thread(target=fb_worker, name=f"fb-worker-{n}", daemon=True)
        for n in range(min(fb_workers, len(fb_idx)))
    ]
    for t in fb_threads:
        t.start()
    with ThreadPoolExecutor(max_workers=other_workers, thread_name_prefix="scrape") as pool:
        list(pool.map(run, other_idx))
    for t in fb_threads:
        t.join()

    if on_progress:
        on_progress(total, total, None)
    return [r for r in results if r is not None]
