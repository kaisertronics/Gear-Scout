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
import json
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
from scrapers.market import load_market, refresh_market_prices
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


LAST_SCHEDULED_PATH = Path("/data/last_scheduled_run.json")
LAST_DIGEST_PATH = Path("/data/last_digest.json")


def _last_digest_time() -> datetime:
    try:
        return datetime.fromisoformat(json.loads(LAST_DIGEST_PATH.read_text())["sent"])
    except Exception:
        return datetime.now(timezone.utc)


def _found_since_last_digest(exclude: set[str]) -> list:
    """Listings the hourly background refreshes found since the last digest
    email, so the scheduled email still reports them."""
    import sqlite3
    from scrapers.base import Listing
    from scrapers.store import _conn
    since = _last_digest_time().isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM seen WHERE first_seen > ? AND COALESCE(live_only, 0) = 0
               AND COALESCE(hidden, 0) = 0 AND COALESCE(duplicate, 0) = 0
               AND source_name NOT LIKE 'FB Posts%'
               ORDER BY first_seen""", (since,)).fetchall()
    out = []
    for r in rows:
        if r["global_id"] in exclude:
            continue
        posted = None
        try:
            posted = datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None
        except ValueError:
            pass
        out.append(Listing(
            source_name=r["source_name"], title=r["title"], url=r["url"], price=r["price"],
            description=r["description"], image_url=r["image_url"], posted_at=posted,
            listing_id=r["global_id"].split("::", 1)[-1], location=r["location"],
        ))
    return out


def _worth_only(listings: list) -> tuple[list, int]:
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.market import load_market
    market, index, similar = load_market(), price_index(), similar_index()
    keep, no_comp = [], 0
    for l in listings:
        if (l.description or "").startswith("Auction"):
            no_comp += 1
            continue
        c = evaluate(l.title, l.price, l.description, l.url, market, index, similar)
        if not c:
            no_comp += 1
        elif c["worth"]:
            l.comp = c
            keep.append(l)
    logger.info("Digest: %d of %d new listings are 10%%+ under their comp (%d without a comp)",
                len(keep), len(listings), no_comp)
    return keep, no_comp


def run_refresh_cycle(cfg: dict):
    """Hourly background refresh (no email): re-reads every source so the
    dashboard always has the latest. Lighter than a full run — Facebook
    Marketplace reads each region's feed without the extra broad searches,
    and the Facebook post search (slow, many searches) is left to the full
    scheduled runs. New finds go into the next digest email."""
    from scrapers.learning import keywords_with_learned, skip_on_light_runs
    keywords = keywords_with_learned(cfg)
    sources = [
        {**s, "_light": True} for s in cfg.get("sources", [])
        if s.get("enabled", True) and s.get("type") != "facebook_posts"
    ]
    sources, skipped = skip_on_light_runs(sources)
    if skipped:
        logger.info("Background refresh skipping (failing or never matching lately): %s", ", ".join(skipped))
    start = time.time()
    results = run_sources(sources, keywords, cfg)
    all_listings = drop_excluded([l for r in results for l in r.listings], cfg, keywords)
    new_listings = filter_new(all_listings)
    logger.info("Background refresh: %d new listings from %d sources in %.0fs",
                len(new_listings), len(results), time.time() - start)
    try:
        Path("/data/last_refresh.json").write_text(json.dumps({
            "finished": datetime.now(timezone.utc).isoformat(), "new_count": len(new_listings)}))
    except OSError:
        pass
    scraped_at = time.time()

    def sold_job():
        # Catch listings that sold, went pending or were taken down.
        from scrapers.sold_check import run_sold_check
        run_sold_check()

    def market_job():
        # New finds first, then this run's other listings, then anything from
        # the last few days (live searches, Telex matches) still without a value.
        from scrapers.store import _conn
        with _conn() as conn:
            recent = [t for (t,) in conn.execute(
                "SELECT title FROM seen WHERE hidden = 0 AND sold = 0 AND first_seen >= datetime('now', '-3 days')"
                " ORDER BY first_seen DESC LIMIT 1500")]
        try:
            nocomp = [t for t in Path("/data/nocomp_titles.txt").read_text().split("\n") if t.strip()]
        except OSError:
            nocomp = []
        refresh_market_prices(nocomp + [l.title for l in new_listings] + [l.title for l in all_listings] + recent,
                              build_price_index(all_priced_rows()), max_lookups=80, cfg=cfg)

    # Independent jobs (listing pages vs. Reverb/eBay lookups): side by side.
    timings = {}

    def timed(name, fn):
        t0 = time.time()
        try:
            fn()
        except Exception:
            logger.exception("%s failed", name)
        timings[name] = time.time() - t0

    def picture_job():
        # Save Facebook pictures while their links still work (they expire).
        from scrapers.image_cache import cache_images, cache_recent
        cache_images([l.image_url for l in all_listings if l.image_url])
        cache_recent(days=2)

    jobs = [threading.Thread(target=timed, args=(n, f), daemon=True)
            for n, f in (("sold check", sold_job), ("price lookups", market_job), ("pictures", picture_job))]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join()
    logger.info("Background refresh timing: scrape %.0fs, sold check %.0fs, price lookups %.0fs, total %.0fs",
                scraped_at - start, timings.get("sold check", 0), timings.get("price lookups", 0),
                time.time() - start)


def run_scrape_cycle():
    with _run_lock:
        _run_scrape_cycle()
        try:
            from scrapers.telex import check_telex_lowest
            check_telex_lowest(load_config())
        except Exception:
            logger.exception("Telex lowest-price check failed")
    # Recorded only once a run finishes, so a run cut short by a restart is
    # caught up on the next start (see _catch_up_missed_run).
    try:
        LAST_SCHEDULED_PATH.write_text(json.dumps({"finished": datetime.now(timezone.utc).isoformat()}))
    except Exception:
        logger.exception("Couldn't record the finished run")


def _catch_up_missed_run(trigger, window_hours: float = 3) -> bool:
    """True if the latest scheduled time (within `window_hours`) has no
    finished run after it — e.g. the container was rebuilt or restarted
    mid-run, or was down at that time."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    fire, latest = trigger.get_next_fire_time(None, now - timedelta(hours=window_hours)), None
    while fire and fire <= now:
        latest = fire
        fire = trigger.get_next_fire_time(fire, fire + timedelta(seconds=1))
    if not latest:
        return False
    try:
        finished = datetime.fromisoformat(json.loads(LAST_SCHEDULED_PATH.read_text())["finished"])
    except Exception:
        finished = None
    return finished is None or finished < latest


def _run_scrape_cycle():
    global _run_counter
    _run_counter += 1
    run_number = _run_counter
    run_time = datetime.now(timezone.utc)

    cfg = load_config()
    from scrapers.learning import keywords_with_learned, update_learned_terms
    try:
        update_learned_terms(cfg)
    except Exception:
        logger.exception("Couldn't update learned search terms")
    keywords = keywords_with_learned(cfg)
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    from scrapers.fb_posts_background import settings as fb_bg_settings
    if fb_bg_settings(cfg)["enabled"]:
        # Facebook post search runs all day in the background instead, with
        # its own afternoon roundup email.
        sources = [s for s in sources if s.get("type") != "facebook_posts"]
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
    all_listings = drop_excluded([l for r in results for l in r.listings], cfg, keywords)
    new_listings = filter_new(all_listings)
    try:
        from scrapers.image_cache import cache_images
        cache_images([l.image_url for l in all_listings if l.image_url])
    except Exception:
        logger.exception("Saving pictures failed")
    # Plus what the hourly background refreshes found since the last email.
    new_listings += _found_since_last_digest({l.global_id for l in new_listings})
    # Only what's worth your time: 10%+ under its comp. Auctions (a current
    # bid isn't a price) and listings with no comp yet aren't emailed; the
    # email says how many there were.
    # Give brand-new finds a comp first (quick lookups), then filter.
    try:
        refresh_market_prices([l.title for l in new_listings], build_price_index(all_priced_rows()),
                              max_lookups=60, pause=0.2, cfg=cfg)
    except Exception:
        logger.exception("Market price lookup for new listings failed")
    new_listings, no_comp_count = _worth_only(new_listings)

    # Sites known to block automated access (shown as "check manually") and
    # eBay pausing for its daily limit are expected, not errors.
    failed_sources = [r for r in results if not r.success and not getattr(r, "blocked", False)]
    has_failures = bool(failed_sources)
    has_new = bool(new_listings)

    logger.info(
        "Results: %d new listings, %d/%d sources OK",
        len(new_listings), len(results) - len(failed_sources), len(results),
    )

    write_run_status(run_number, run_time, results, new_listings)

    # Typical prices for models seen too rarely locally: look them up on
    # Reverb (cached, capped per run) so this run's listings and email get one.
    local_index = build_price_index(all_priced_rows())

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
            price_index=local_index,
            market_index=load_market(),
            price_drops=price_drops,
            no_comp_count=no_comp_count,
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
            LAST_DIGEST_PATH.write_text(json.dumps({"sent": datetime.now(timezone.utc).isoformat()}))
        else:
            logger.error("Email failed to send. Check SMTP credentials in config.yaml.")
    else:
        logger.info("No new listings and no failures — skipping email this run.")

    if price_drops:
        mark_price_drops_notified([d["global_id"] for d in pending_favorite_price_drops()])

    try:
        refresh_market_prices([l.title for l in all_listings], local_index, max_lookups=120, cfg=cfg)
    except Exception:
        logger.exception("Market price refresh failed")

    # Periodic DB cleanup
    # Kept for 90 days (the hourly checks already take sold listings out of
    # view) so slow-moving gear stays on the Telex List and in search.
    purge_old(days=90)

    logger.info("Run #%d complete.\n", run_number)


# A full scrape and a saved-search pass each drive several headless browsers
# (and the Facebook account) — never let them overlap.
_run_lock = threading.Lock()


def _dashboard_job_running() -> bool:
    """True while you're running Scrape now, a live search, a Lowest Price
    check or an FB post search from the dashboard — background jobs step
    aside so your search gets the machine (and the Facebook account)."""
    for name in ("manual_scrape_status.json", "live_search_status.json",
                 "lowest_status.json", "fbposts_status.json", "telex_status.json"):
        p = Path("/data") / name
        try:
            if time.time() - p.stat().st_mtime < 900 and json.loads(p.read_text()).get("state") == "running":
                return True
        except Exception:
            continue
    return False


def run_watch_cycle():
    if _dashboard_job_running():
        logger.info("Hourly refresh skipped — a search from the dashboard is running.")
        return
    if not _run_lock.acquire(blocking=False):
        logger.info("Saved searches skipped this interval — a scrape is already running.")
        return
    try:
        from scrapers.lowest import run_trackers, tracked_queries
        from scrapers.watches import get_watches, notify_watch_hits, run_watches
        cfg = load_config()
        if (cfg.get("schedule") or {}).get("background_refresh", True):
            try:
                run_refresh_cycle(cfg)
            except Exception:
                logger.exception("Background refresh failed")
        # Actively search the next few Telex List terms on search-based sites,
        # then alert on any listing below the lowest price for its term.
        try:
            from scrapers.telex import check_telex_lowest, run_sweep
            run_sweep(cfg)
            check_telex_lowest(cfg)
        except Exception:
            logger.exception("Telex sweep / lowest-price check failed")
        try:
            from scrapers import board
            board.build(cfg)
        except Exception:
            logger.exception("Price Board build failed")
        # Auctions: eBay's ending-soonest pro-audio auctions, then daytime
        # "ending soon" alerts and the 9 PM overnight-auctions list.
        try:
            from scrapers import auctions
            auctions.fetch_ebay_ending(cfg)
            auctions.check(cfg)
        except Exception:
            logger.exception("Auction check failed")
        # Price cuts that turn an ad into a deal (in budget): push + email.
        try:
            from scrapers import price_drops
            price_drops.alert(cfg)
        except Exception:
            logger.exception("Price-drop check failed")
        # New steals (60%+ under used prices): push + email right away.
        try:
            from scrapers.steals import alert_new
            alert_new(cfg)
        except Exception:
            logger.exception("Steal alert check failed")
        # Alerts you set up by asking the AI assistant.
        try:
            from scrapers.assistant import check_alerts
            check_alerts(cfg)
        except Exception:
            logger.exception("AI alert check failed")
        if any(w["enabled"] for w in get_watches(cfg)):
            notify_watch_hits(cfg, run_watches(cfg))
        # Lowest-price trackers share the same timer (and lock).
        if tracked_queries(cfg):
            run_trackers(cfg)
    except Exception:
        logger.exception("Saved-search cycle failed")
    finally:
        _run_lock.release()


def run_fb_posts_batch():
    # Leaves the machine to a scrape that's already running (CPU, memory
    # and the Facebook account) — the next batch is 15 minutes away.
    if _run_lock.locked() or _dashboard_job_running():
        return
    try:
        from scrapers.fb_posts_background import run_batch
        run_batch(load_config())
    except Exception:
        logger.exception("Background Facebook post search failed")


def run_ai_briefing():
    try:
        from scrapers.ai_reports import daily_briefing
        daily_briefing(load_config())
    except Exception:
        logger.exception("AI daily briefing failed")


def run_ai_weekly():
    try:
        from scrapers.ai_reports import weekly_notes
        weekly_notes(load_config())
    except Exception:
        logger.exception("AI weekly notes failed")


def run_fb_posts_roundup():
    """Checked every hour on the hour; sends at the configured hour."""
    try:
        from scrapers.fb_posts_background import _local_now, send_roundup, settings
        cfg = load_config()
        s = settings(cfg)
        if not s["enabled"] or _local_now(cfg).hour != s["roundup_hour"]:
            return
        send_roundup(cfg)
    except Exception:
        logger.exception("Facebook posts roundup failed")


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
    cron_trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=tz_name,
    )
    scheduler.add_job(
        run_scrape_cycle,
        cron_trigger,
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

    # Both jobs always run and re-read their settings each time, so changes
    # made on the FB Posts page apply without restarting anything.
    from scrapers import fb_posts_background as fbbg
    scheduler.add_job(run_fb_posts_batch, IntervalTrigger(minutes=fbbg.BATCH_EVERY_MINUTES, timezone=tz_name),
                      name="gear_scout_fb_posts", max_instances=1, coalesce=True, misfire_grace_time=300)
    scheduler.add_job(run_fb_posts_roundup, CronTrigger(minute=0, timezone=tz_name),
                      name="gear_scout_fb_roundup", misfire_grace_time=1800)
    # AI emails (only when a Gemini key is set): daily briefing and weekly notes.
    scheduler.add_job(run_ai_briefing, CronTrigger(hour=8, minute=0, timezone=tz_name),
                      name="gear_scout_ai_briefing", misfire_grace_time=3600)
    scheduler.add_job(run_ai_weekly, CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=tz_name),
                      name="gear_scout_ai_weekly", misfire_grace_time=3600)

    logger.info("Scheduler started. Cron: '%s' (%s); background refresh + saved searches every %d min",
                cron_expr, tz_name, interval)

    # Off by default: every container restart (rebuild, reboot, crash
    # recovery) would otherwise scrape and email immediately, on top of the
    # normal cron times. Opt in with `schedule: run_on_startup: true`.
    if schedule_cfg.get("run_on_startup", False):
        logger.info("Running initial scrape now (run_on_startup)...")
        run_scrape_cycle()
    elif _catch_up_missed_run(cron_trigger):
        # A scheduled run in the last 3 hours never finished (restart,
        # rebuild, computer asleep) — run it now instead of skipping it.
        from datetime import timedelta
        logger.info("A scheduled run was missed — running it in 1 minute.")
        scheduler.add_job(run_scrape_cycle, "date", name="gear_scout_catch_up",
                          run_date=datetime.now(timezone.utc) + timedelta(minutes=1))

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
