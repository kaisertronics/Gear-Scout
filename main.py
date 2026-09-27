#!/usr/bin/env python3
"""
Gear Scout — main entry point.

Commands:
  python main.py run        — run one scrape cycle now
  python main.py schedule   — start the scheduler (used by Docker)
  python main.py fb-login   — interactive Facebook login
  python main.py fb-search  — search Facebook groups by keyword
  python main.py status     — print DB stats and source list
"""
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from scrapers.enrich import drop_excluded
from scrapers.base import ScrapeResult
from scrapers.runner import run_sources
from scrapers.emailer import build_email_html, send_email
from scrapers.run_status import write_run_status
from scrapers.enrich import build_price_index
from scrapers.notify import push_enabled, send_push
from scrapers.store import (
    all_priced_rows,
    filter_new,
    mark_price_drops_notified,
    pending_favorite_price_drops,
    purge_old,
    stats as db_stats,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gear_scout")

CONFIG_PATH = Path("/config/config.yaml")

# Track how many times we've run this session (for email subject numbering)
_run_counter = 0


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def run_scrape_cycle():
    with _run_lock:
        _run_scrape_cycle()


def _run_scrape_cycle():
    global _run_counter
    _run_counter += 1
    run_number = _run_counter
    run_time = datetime.now(timezone.utc)

    cfg = load_config()
    keywords = cfg.get("keywords", [])
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    email_cfg = cfg["email"]

    logger.info("=" * 60)
    logger.info("Gear Scout run #%d — %s UTC", run_number, run_time.strftime("%Y-%m-%d %H:%M"))
    logger.info("%d sources active, %d keywords", len(sources), len(keywords))
    logger.info("=" * 60)

    scrape_start = time.time()
    results: list[ScrapeResult] = run_sources(sources, keywords, cfg)
    logger.info("Scraped %d sources in %.0fs", len(results), time.time() - scrape_start)

    for result in results:
        if result.success:
            logger.info(
                "  ✓ %s: %d listings found in %.1fs",
                result.source_name, len(result.listings), result.duration_seconds,
            )
        else:
            logger.warning(
                "  ✗ %s: FAILED in %.1fs — %s",
                result.source_name, result.duration_seconds, result.error,
            )
            if result.fix_hint:
                logger.warning("    FIX: %s", result.fix_hint)

    # Deduplicate — only keep listings we haven't seen before
    all_listings = drop_excluded([l for r in results for l in r.listings], cfg)
    new_listings = filter_new(all_listings)

    failed_sources = [r for r in results if not r.success]
    has_failures = bool(failed_sources)
    has_new = bool(new_listings)

    logger.info(
        "Results: %d new listings, %d/%d sources OK",
        len(new_listings), len(results) - len(failed_sources), len(results),
    )

    write_run_status(run_number, run_time, results, new_listings)

    # Favorites: re-check each one's own page for a price change, then gather
    # any drops not yet reported (from this check or from scrapes above).
    try:
        from scrapers.price_check import check_favorite_prices
        check_favorite_prices()
    except Exception:
        logger.exception("Favorite price check failed")
    price_drops, seen_urls = [], set()
    for d in pending_favorite_price_drops():
        if d["url"] not in seen_urls:
            seen_urls.add(d["url"])
            price_drops.append(d)
    if price_drops and push_enabled(cfg):
        for d in price_drops:
            send_push(cfg, "Price drop on a favorite",
                      f"{d['title'][:90]}\n{d.get('previous_price') or '?'} → {d['price']}",
                      url=d["url"], tags=["chart_with_downwards_trend"])

    # --- Email logic ---
    # Send email if:
    #   (a) there are new listings, OR
    #   (b) one or more sources failed (so you're always notified of problems), OR
    #   (c) a favorite dropped in price
    if has_new or has_failures or price_drops:
        html = build_email_html(
            new_listings=new_listings,
            results=results,
            run_time=run_time,
            run_number=run_number,
            max_listings_per_source=email_cfg.get("max_listings_per_source", 8),
            max_total_listings=email_cfg.get("max_total_listings", 40),
            dashboard_url=email_cfg.get("dashboard_url", "http://localhost:8420"),
            price_index=build_price_index(all_priced_rows()),
            price_drops=price_drops,
        )
        success = send_email(
            html=html,
            subject=email_cfg.get("subject", "Gear Scout"),
            cfg=email_cfg,
            new_count=len(new_listings),
            failed_count=len(failed_sources),
        )
        if success:
            logger.info("Email sent successfully.")
        else:
            logger.error("Email failed to send. Check SMTP credentials in config.yaml.")
    else:
        logger.info("No new listings and no failures — skipping email this run.")

    if price_drops:
        mark_price_drops_notified([d["global_id"] for d in pending_favorite_price_drops()])

    # Periodic DB cleanup
    purge_old(days=30)

    logger.info("Run #%d complete.\n", run_number)


# A full scrape and a saved-search pass each drive several headless browsers
# (and the Facebook account) — never let them overlap.
_run_lock = threading.Lock()


def run_watch_cycle():
    if not _run_lock.acquire(blocking=False):
        logger.info("Saved searches skipped this interval — a scrape is already running.")
        return
    try:
        from scrapers.watches import get_watches, notify_watch_hits, run_watches
        cfg = load_config()
        if not any(w["enabled"] for w in get_watches(cfg)):
            return
        notify_watch_hits(cfg, run_watches(cfg))
    except Exception:
        logger.exception("Saved-search cycle failed")
    finally:
        _run_lock.release()


def run_schedule():
    cfg = load_config()
    schedule_cfg = cfg.get("schedule", {})
    cron_expr = schedule_cfg.get("cron", "0 7 * * *")
    tz_name = schedule_cfg.get("timezone", "UTC")

    # Parse cron: "minute hour day month day_of_week"
    parts = cron_expr.split()
    if len(parts) != 5:
        logger.error("Invalid cron expression in config.yaml: '%s'", cron_expr)
        sys.exit(1)

    minute, hour, day, month, day_of_week = parts

    scheduler = BlockingScheduler(timezone=tz_name)
    scheduler.add_job(
        run_scrape_cycle,
        CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=tz_name,
        ),
        name="gear_scout",
        misfire_grace_time=300,
    )

    # Saved searches run on their own, more frequent timer so a hit can be
    # pushed to the phone within the hour instead of waiting for the digest.
    interval = int((cfg.get("notifications") or {}).get("watch_interval_minutes", 60) or 60)
    interval = max(15, interval)
    scheduler.add_job(
        run_watch_cycle,
        IntervalTrigger(minutes=interval, timezone=tz_name),
        name="gear_scout_watches",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    logger.info("Scheduler started. Cron: '%s' (%s); saved searches every %d min",
                cron_expr, tz_name, interval)

    # Off by default: every container restart (rebuild, reboot, crash
    # recovery) would otherwise scrape and email immediately, on top of the
    # normal cron times. Opt in with `schedule: run_on_startup: true`.
    if schedule_cfg.get("run_on_startup", False):
        logger.info("Running initial scrape now (run_on_startup)...")
        run_scrape_cycle()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


def cmd_fb_login():
    from scrapers.facebook_scraper import interactive_login
    cfg = load_config()
    fb_cfg = cfg.get("facebook", {})
    interactive_login(
        fb_email=fb_cfg.get("email", ""),
        fb_password=fb_cfg.get("password", ""),
    )


def cmd_fb_search():
    from scrapers.facebook_scraper import print_group_search_results, search_facebook_groups

    if len(sys.argv) < 3:
        print("\nUsage: docker compose run --rm scout fb-search \"search term\"\n")
        print("Examples:")
        print('  docker compose run --rm scout fb-search "vintage studio gear"')
        print('  docker compose run --rm scout fb-search "outboard gear for sale"')
        print('  docker compose run --rm scout fb-search "pro audio buy sell trade"\n')
        sys.exit(1)

    term = " ".join(sys.argv[2:])
    results = search_facebook_groups(term)
    print_group_search_results(results, config_path=str(CONFIG_PATH))


def cmd_status():
    cfg = load_config()
    sources = cfg.get("sources", [])
    enabled = [s for s in sources if s.get("enabled", True)]
    disabled = [s for s in sources if not s.get("enabled", True)]
    s = db_stats()

    print("\n" + "=" * 60)
    print("GEAR SCOUT STATUS")
    print("=" * 60)
    print(f"\nSources: {len(enabled)} enabled, {len(disabled)} disabled")
    for src in enabled:
        print(f"  ✓ [{src.get('type','?'):12}] {src['name']}")
    if disabled:
        print("\nDisabled:")
        for src in disabled:
            print(f"  ✗  [{src.get('type','?'):12}] {src['name']}")

    print(f"\nDatabase: {s['total_seen']} listings seen total, {s['seen_today']} seen today")
    print(f"DB path: /data/seen_listings.db")
    sched = cfg.get("schedule", {})
    print(f"\nSchedule: {sched.get('cron', '0 7 * * *')} ({sched.get('timezone', 'UTC')})")
    print("=" * 60 + "\n")


def cmd_run():
    run_scrape_cycle()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "run":
        cmd_run()
    elif cmd == "schedule":
        run_schedule()
    elif cmd == "fb-login":
        cmd_fb_login()
    elif cmd == "fb-search":
        cmd_fb_search()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: run | schedule | fb-login | fb-search | status")
        sys.exit(1)
