"""Fast lane for fresh mics (owner proposal, Nov 1): data shows 20% of the
mics Gear Scout catches early sell within 6 hours, but the 15-minute sweep
takes ~4 minutes a round. Every 2 minutes this reads only the newest used
microphone listings (Reverb: 1 request; eBay Buy It Now: 1 request), stores
the new ones like the sweep does, and pushes a mic deal at once under the
existing Mic Focus rules (nothing here changes a threshold). A push is
recorded in mic_alerted, so the sweep's alert doesn't send it again.

Outboard (owner, Nov 2): two more Reverb requests read the newest used
compressors and preamps for the owner's targets (Distressor, LA-2A, 1176,
Fairchild, API / CAPI). A Distressor goes through the Mic Focus rules above
(its own caps); the rest are pushed only when they match the Wanted (Telex)
list, with the same "New: ..." push the sweep sends (recorded in wanted_fast,
so it doesn't send it again). No new price rules."""
import logging
import re
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REVERB_NEWEST = 50
EBAY_NEWEST = 100
GEAR_CATEGORIES = ("compressors-and-limiters", "preamps")  # Reverb pro-audio category slugs
GEAR_TARGETS = re.compile(r"\bdistressor\b|\bel8\b|\bla-?2a\b|\b1176|\bfairchild\b|\bcapi\b|\bapi\b", re.I)


def _fetch_reverb(cfg: dict) -> list:
    from scrapers.reverb_api import API_URL, HEADERS, _to_listing, reverb_get
    resp = reverb_get(API_URL, params={"product_type": "pro-audio", "category": "microphones", "condition": "used",
                                       "sort": "published_at|desc", "per_page": REVERB_NEWEST},
                      headers=HEADERS, timeout=15)
    resp.raise_for_status()
    from scrapers.reverb_api import country_of
    from scrapers.store import is_overseas
    return [_to_listing(i, "Reverb — All Used") for i in resp.json().get("listings") or []
            if (i.get("state") or {}).get("slug", "live") == "live" and i.get("title") and not is_overseas(country_of(i))]


def _fetch_reverb_gear(cfg: dict) -> tuple:
    """(listings, requests): the newest used compressors and preamps on Reverb
    (1 request each) that name one of the owner's targets."""
    from scrapers.reverb_api import API_URL, HEADERS, _to_listing, country_of, reverb_get
    from scrapers.store import is_overseas
    out, urls, n = [], set(), 0
    for category in GEAR_CATEGORIES:
        try:
            n += 1
            resp = reverb_get(API_URL, params={"product_type": "pro-audio", "category": category, "condition": "used",
                                               "sort": "published_at|desc", "per_page": REVERB_NEWEST},
                              headers=HEADERS, timeout=15)
            resp.raise_for_status()
            for i in resp.json().get("listings") or []:
                l = _to_listing(i, "Reverb — All Used")
                if ((i.get("state") or {}).get("slug", "live") == "live" and i.get("title") and not is_overseas(country_of(i))
                        and GEAR_TARGETS.search(l.title) and l.url not in urls):
                    urls.add(l.url)
                    out.append(l)
        except Exception as e:
            logger.warning("Fast lane: reverb %s failed: %s", category, e)
    return out, n


def _push_wanted(cfg: dict, fresh: list) -> list:
    """Pushes new outboard listings that match the Wanted list, like the
    sweep's "New: ..." push. More than a few at once are left to the sweep's
    summary push. Returns the rows pushed."""
    import sqlite3
    from scrapers import wanted
    from scrapers.notify import push_enabled
    from scrapers.store import _conn
    if not fresh or not push_enabled(cfg):
        return []
    urls = [l.url for l in fresh]
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM seen WHERE url IN ({','.join('?' * len(urls))}) AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND (country IS NULL OR country IN ('US', 'CA'))",
            urls)]
    hits = wanted.hits_in(cfg, rows)
    if not hits or len(hits) > wanted.ALERT_MAX_SINGLE:
        return []
    sent = [r for r, found in hits if wanted.push_hit(cfg, r, found) is not False]
    if sent:
        now = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            wanted._ensure_fast(conn)
            conn.executemany("INSERT OR IGNORE INTO wanted_fast VALUES (?, ?)", [(r["url"], now) for r in sent])
            conn.commit()
    return sent


def _fetch_ebay(cfg: dict) -> list:
    api_cfg = cfg.get("ebay_api") or {}
    if not (api_cfg.get("client_id") and api_cfg.get("client_secret")):
        return []
    from scrapers.ebay_api import _pt_now, scrape_ebay_api
    # 1am-6am Pacific: every third tick only (nothing much is listed; saves ~100 of the day's eBay calls)
    if 1 <= _pt_now().hour < 6 and int(time.time() // 120) % 3:
        return []
    res = scrape_ebay_api({"name": "eBay — Pro Audio"}, [], api_cfg, newest=EBAY_NEWEST)
    return res.listings if res.success else []


def tick(cfg: dict) -> dict:
    """One pass. Returns {"seen", "new", "pushed", "requests", "seconds"}."""
    from scrapers import instant_offer, mic_focus
    from scrapers import deal_checks as D
    from scrapers.enrich import drop_excluded
    from scrapers.notify import push_enabled, send_push
    from scrapers.store import _conn, mark_seen
    t0 = time.time()
    out = {"seen": 0, "new": 0, "pushed": 0, "requests": 0, "seconds": 0.0}
    listings = []
    for fetch in (_fetch_reverb, _fetch_ebay):
        try:
            out["requests"] += 1
            listings += fetch(cfg)
        except Exception as e:
            logger.warning("Fast lane: %s failed: %s", fetch.__name__.strip("_"), e)
    gear = []
    try:
        gear, n = _fetch_reverb_gear(cfg)
        out["requests"] += n
    except Exception as e:
        logger.warning("Fast lane: reverb gear failed: %s", e)
    out["seen"] = len(listings) + len(gear)
    # Only Mic Focus mics are stored (eBay's newest Pro Audio also holds monitors, cables...).
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"]]
    focus = lambda l: mic_focus.classify(l.title, targets)
    listings = [l for l in listings + gear if focus(l)]  # a Distressor counts as one
    fresh = [l for l in drop_excluded(listings, cfg) if mark_seen(l) == "new"]
    out["new"] = len(fresh)
    # The other outboard targets: stored, and pushed when they're on the Wanted list
    # (with Mic Focus on, mic alerts only, like the sweep).
    fresh_gear = [l for l in drop_excluded([l for l in gear if not focus(l)], cfg) if mark_seen(l) == "new"]
    out["new"] += len(fresh_gear)
    if fresh_gear and not mic_focus.enabled(cfg):
        out["pushed"] += len(_push_wanted(cfg, fresh_gear))
    if fresh:
        urls = [l.url for l in fresh]
        with _conn() as conn:
            import sqlite3
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM seen WHERE url IN ({','.join('?' * len(urls))}) AND hidden = 0 AND duplicate = 0"
                " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
                " AND (country IS NULL OR country = 'US')", urls)]
        hits = [r for r in mic_focus.find(cfg, rows) if r["mic"]["hit"]]
        hits = [r for r in hits if not D.scam_flags(r, r["mic"].get("used"), r["mic"].get("delivered") or 0)]
        with _conn() as conn:
            mic_focus._ensure(conn)
            done = {(u, p) for u, p in conn.execute("SELECT url, price FROM mic_alerted")}
        hits = [r for r in hits if (r["url"], str(r.get("price"))) not in done]
        sent = []
        for r in hits:
            m = r["mic"]
            head = (f"pair ${m['total']:,.0f}" if (m.get("qty") or 1) > 1 else f"${m['delivered']:,.0f}") + \
                (f" ({m['pct']}% off {m['ref_kind']})" if m["pct"] and m["pct"] > 0 else "")
            chk = " (double-check)" if m["check"] else ""
            ok = True
            if push_enabled(cfg):
                ok = send_push(cfg, f"🎤 {m['name']}: {head}{chk}",
                               f"{r['title'][:100]}\n{mic_focus._line(r)}" + mic_focus.offers_text(r),
                               url=r["url"], tags=["microphone"], image=r.get("image_url"),
                               actions=instant_offer.push_actions(cfg, r["url"], instant_offer.suggested_for(r)))
            if ok is not False:
                sent.append(r)
        if sent:
            now = datetime.now(timezone.utc).isoformat()
            with _conn() as conn:
                mic_focus._ensure(conn)
                conn.executemany("INSERT OR IGNORE INTO mic_alerted VALUES (?, ?, ?)",
                                 [(r["url"], str(r.get("price")), now) for r in sent])
                conn.commit()
        out["pushed"] += len(sent)
    out["seconds"] = round(time.time() - t0, 1)
    if out["new"]:
        logger.info("Fast lane: %d new, %d pushed (%.0fs)", out["new"], out["pushed"], out["seconds"])
    return out
