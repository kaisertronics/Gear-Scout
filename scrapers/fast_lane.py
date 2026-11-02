"""Fast lane for fresh mics (owner proposal, Nov 1): data shows 20% of the
mics Gear Scout catches early sell within 6 hours, but the 15-minute sweep
takes ~4 minutes a round. Every 2 minutes this reads only the newest used
microphone listings (Reverb: 1 request; eBay Buy It Now: 1 request), stores
the new ones like the sweep does, and pushes a mic deal at once under the
existing Mic Focus rules (nothing here changes a threshold). A push is
recorded in mic_alerted, so the sweep's alert doesn't send it again."""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REVERB_NEWEST = 50
EBAY_NEWEST = 100


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


def _fetch_ebay(cfg: dict) -> list:
    api_cfg = cfg.get("ebay_api") or {}
    if not (api_cfg.get("client_id") and api_cfg.get("client_secret")):
        return []
    from scrapers.ebay_api import scrape_ebay_api
    res = scrape_ebay_api({"name": "eBay — Pro Audio"}, [], api_cfg, newest=EBAY_NEWEST)
    return res.listings if res.success else []


def tick(cfg: dict) -> dict:
    """One pass. Returns {"seen", "new", "pushed", "requests", "seconds"}."""
    from scrapers import mic_focus
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
    out["seen"] = len(listings)
    # Only Mic Focus mics are stored (eBay's newest Pro Audio also holds monitors, cables...).
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"]]
    listings = [l for l in listings if mic_focus.classify(l.title, targets)]
    fresh = [l for l in drop_excluded(listings, cfg) if mark_seen(l) == "new"]
    out["new"] = len(fresh)
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
                ok = send_push(cfg, f"🎤 {m['name']}: {head}{chk}", f"{r['title'][:100]}\n{mic_focus._line(r)}",
                               url=r["url"], tags=["microphone"], image=r.get("image_url"))
            if ok is not False:
                sent.append(r)
        if sent:
            now = datetime.now(timezone.utc).isoformat()
            with _conn() as conn:
                mic_focus._ensure(conn)
                conn.executemany("INSERT OR IGNORE INTO mic_alerted VALUES (?, ?, ?)",
                                 [(r["url"], str(r.get("price")), now) for r in sent])
                conn.commit()
        out["pushed"] = len(sent)
    out["seconds"] = round(time.time() - t0, 1)
    if out["new"]:
        logger.info("Fast lane: %d new, %d pushed (%.0fs)", out["new"], out["pushed"], out["seconds"])
    return out
