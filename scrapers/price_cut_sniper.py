"""
Price-cut sniper (owner, Nov 3): many of the best deals are older ads whose
seller just cut the price. Every hour: a listing whose price was cut in the
last day that NOW is a Mic Focus deal (the same rules the new-listing mic
push uses, mic_focus.find — nothing here changes a threshold) but was not at
its previous price, or that dropped 15%+ and now qualifies → one push, with
the usual offer button. Each ad + new price is pushed once (mic_alerted, the
same record the mic pushes use), at most 3 per run.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
MAX_PER_RUN = 3
BIG_DROP_PCT = 15
WINDOW_HOURS = 24


def candidates(cfg: dict, hours: int = WINDOW_HOURS) -> list[dict]:
    """Recently cut listings that now qualify as a deal (and didn't before, or
    fell 15%+), not yet pushed at this price. Rows carry r['mic'] and r['was']."""
    from scrapers import deal_checks as D
    from scrapers import mic_focus
    from scrapers.enrich import parse_price
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE price_dropped_at >= ? AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND previous_price IS NOT NULL"
            " AND price IS NOT NULL AND url LIKE 'http%' AND (country IS NULL OR country = 'US')", (since,))]
        mic_focus._ensure(conn)
        done = {(u, p) for u, p in conn.execute("SELECT url, price FROM mic_alerted")}
    rows = [r for r in rows if (r["url"], str(r.get("price"))) not in done]
    rows = mic_focus.still_showing(rows)
    if not rows:
        return []
    old_rows = [dict(r, price=r["previous_price"]) for r in rows]
    now_hits = {r["url"]: r for r in mic_focus.find(cfg, rows) if r["mic"]["hit"]}
    was_hits = {r["url"] for r in mic_focus.find(cfg, old_rows) if r["mic"]["hit"]}
    out = []
    for url, r in now_hits.items():
        new, old = parse_price(r.get("price")), parse_price(r.get("previous_price"))
        if not new or not old or new >= old:
            continue
        cut = round((1 - new / old) * 100)
        if url in was_hits and cut < BIG_DROP_PCT:
            continue  # was already a deal and the cut is small
        if D.scam_flags(r, r["mic"].get("used"), r["mic"].get("delivered") or 0):
            continue
        r["cut_pct"] = cut
        out.append(r)
    return sorted(out, key=lambda r: r["price_dropped_at"], reverse=True)


def alert(cfg: dict, dry_run: bool = False) -> int:
    """Pushes up to MAX_PER_RUN cuts. dry_run: sends and records nothing and
    returns the number that would be pushed."""
    from scrapers import instant_offer, mic_focus
    from scrapers.notify import push_enabled, send_push
    from scrapers.store import _conn
    todo = candidates(cfg)[:MAX_PER_RUN]
    if not todo:
        return 0
    if dry_run:
        for r in todo:
            logger.info("Price-cut sniper (dry run): %s %s -> %s", r["title"][:60], r["previous_price"], r["price"])
        return len(todo)
    sent = []
    for r in todo:
        m = r["mic"]
        head = (f"pair ${m['total']:,.0f}" if (m.get("qty") or 1) > 1 else f"${m['delivered']:,.0f}") + \
            (f" ({m['pct']}% off {m['ref_kind']})" if m["pct"] and m["pct"] > 0 else "")
        chk = " (double-check)" if m["check"] else ""
        ok = True
        if push_enabled(cfg):
            ok = send_push(cfg, f"📉 Price cut: {m['name']} {head}{chk}",
                           f"{r['title'][:100]}\nwas {r['previous_price']}, now {r['price']} (-{r['cut_pct']}%)\n"
                           f"{mic_focus._line(r)}" + mic_focus.offers_text(r),
                           url=r["url"], tags=["chart_with_downwards_trend"], image=r.get("image_url"),
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
    logger.info("Price-cut sniper: %d candidates, %d pushed", len(todo), len(sent))
    return len(sent)
