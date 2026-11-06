"""
Market analytics for each Telex term, from every listing Gear Scout has
seen (for sale, sold or gone):

  - price trend: median asking price of ads posted in the last 2 weeks vs
    the 4 weeks before, plus weekly medians for a small chart
  - how fast it sells: median days an ad stays up before it sells, and the
    share that sell within a week
  - supply: how many are for sale now, the cheapest, new ads per week
  - haggle room: how often sellers cut their price, by how much, and how
    long unsold ads have been sitting
  - a one-line tip from all of that

Prices are per piece (a pair at $400 counts $200 each). Auctions, broken
units, parts and lots are left out. Rebuilt in the background about once an
hour (matching ~50k titles takes a few seconds) and saved to
/data/analytics.json so the front page never waits on it.
"""
import json
import logging
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT = Path("/data/analytics.json")
MAX_AGE = 3600          # rebuild after an hour
RECENT_DAYS = 14        # "now" window for the price trend
EARLIER_DAYS = 42       # compared against days 14–42 ago
TREND_MIN = 5           # listings needed in each window to call a trend
TREND_MAX = 75          # a bigger "move" is a mix of different gear, not a trend
TREND_PCT = 8           # a move smaller than this is "steady"
LIVE_DAYS = 3           # seen this recently and not sold = for sale now


def _dt(s: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(s or "")
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _median(vs):
    return round(statistics.median(vs), 2) if vs else None


def build(cfg: dict) -> dict:
    from scrapers import wanted
    from scrapers.wanted import is_cable_term
    from scrapers.enrich import is_lot, needs_repair, parse_price, price_basis
    from scrapers.store import _conn
    started = time.time()
    now = datetime.now(timezone.utc)
    ms = wanted.matchers(cfg)
    specific = {m[0]: m[3] for m in ms}
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT title, price, source_name, description, first_seen, posted_at, last_seen, sold, sold_at,
                      previous_price, price_dropped_at, pending
               FROM seen WHERE COALESCE(duplicate, 0) = 0 AND COALESCE(hidden, 0) = 0
               AND (country IS NULL OR country IN ('US', 'CA'))""").fetchall()
    per: dict[str, list[dict]] = {t: [] for t in wanted.terms(cfg)}
    for r in rows:
        title = r["title"] or ""
        if (r["description"] or "").startswith("Auction") or needs_repair(title) or is_lot(title):
            continue
        terms = wanted.match(title, r["price"], ms, r["source_name"])
        if not terms:
            continue
        value = parse_price(r["price"])
        if not value or value < 5:
            continue
        unit = price_basis(title, None, value)[2]
        posted = _dt(r["posted_at"])
        start = posted or _dt(r["first_seen"])
        if not start:
            continue
        prev = parse_price(r["previous_price"]) if r["previous_price"] else None
        # How long it was up before selling: only when the seller's posting
        # date is known (otherwise Gear Scout only knows when it first saw
        # the ad). It's marked sold within about an hour of disappearing.
        gone = _dt(r["sold_at"]) or _dt(r["last_seen"])
        rec = {"unit": unit, "start": start, "last": _dt(r["last_seen"]) or start,
               "up_days": max(0.0, (gone - posted).total_seconds() / 86400) if r["sold"] and posted and gone else None,
               "sold": bool(r["sold"]), "cut": round(100 * (prev - value) / prev) if prev and prev > value else 0,
               "title": title}
        for t in terms:
            if t in per:
                per[t].append(rec)

    out = []
    for term, recs in per.items():
        if not recs:
            out.append({"term": term, "count": 0})
            continue
        # Drop wild outliers (a $5 adapter under a $2,000 term).
        mid = statistics.median(r["unit"] for r in recs)
        recs = [r for r in recs if mid / 5 <= r["unit"] <= mid * 5]
        age = lambda r: (now - r["start"]).days
        recent = [r["unit"] for r in recs if age(r) < RECENT_DAYS]
        earlier = [r["unit"] for r in recs if RECENT_DAYS <= age(r) < EARLIER_DAYS]
        m_recent, m_earlier = _median(recent), _median(earlier)
        trend = None
        mixed = is_cable_term(term) or not specific.get(term, True)
        if len(recent) >= TREND_MIN and len(earlier) >= TREND_MIN and m_earlier:
            trend = round(100 * (m_recent - m_earlier) / m_earlier)
            if abs(trend) > TREND_MAX:
                mixed = True
        weekly = []
        for w in range(5, -1, -1):  # oldest → newest week
            vs = [r["unit"] for r in recs if 7 * w <= age(r) < 7 * (w + 1)]
            weekly.append(_median(vs) if len(vs) >= 2 else None)
        sold = [r for r in recs if r["sold"]]
        days_to_sell = [r["up_days"] for r in sold if r["up_days"] is not None]
        live = [r for r in recs if not r["sold"] and (now - r["last"]).days < LIVE_DAYS]
        live_age = [age(r) for r in live]
        cuts = [r["cut"] for r in recs if r["cut"] > 0]
        new_week = round(sum(1 for r in recs if age(r) < 28) / 4, 1)
        d = {
            "term": term, "count": len(recs), "specific": specific.get(term, True), "mixed": mixed,
            "live": len(live), "cheapest": min((r["unit"] for r in live), default=None),
            "median_now": m_recent or _median([r["unit"] for r in live]), "median_before": m_earlier,
            "n_recent": len(recent), "n_earlier": len(earlier), "trend": trend, "weekly": weekly,
            "sold": len(sold), "n_timed": len(days_to_sell),
            "days_to_sell": round(statistics.median(days_to_sell), 1) if len(days_to_sell) >= 3 else None,
            "sold_week_pct": round(100 * sum(1 for x in days_to_sell if x <= 7) / len(days_to_sell)) if len(days_to_sell) >= 3 else None,
            "new_per_week": new_week,
            "cut_pct": round(100 * len(cuts) / len(recs)) if len(recs) >= 5 else None,
            "avg_cut": round(statistics.mean(cuts)) if cuts else None,
            "sitting_days": round(statistics.median(live_age)) if live_age else None,
        }
        d["tip"] = _tip(d)
        out.append(d)

    priced = [d for d in out if d.get("count")]
    trending = [d for d in priced if d.get("trend") is not None and not d.get("mixed")]
    fast = [d for d in priced if d.get("days_to_sell") is not None]
    haggle = [d for d in priced if d.get("cut_pct") is not None and d.get("live")]
    def once(ds):
        """Terms that match the very same ads (three Golden Age R1 terms)
        show once on a leaderboard."""
        seen, out_ = set(), []
        for d in ds:
            sig = (d.get("count"), d.get("median_now"), d.get("live"), d.get("sold"))
            if sig not in seen:
                seen.add(sig)
                out_.append(d)
        return out_

    data = {
        "built": now.isoformat(), "seconds": round(time.time() - started, 1),
        "listings": sum(d.get("count", 0) for d in out), "since": min((r["first_seen"] for r in rows), default=None),
        "terms": sorted(out, key=lambda d: (-(d.get("live") or 0), d["term"].lower())),
        "falling": once(sorted((d for d in trending if d["trend"] <= -TREND_PCT), key=lambda d: d["trend"]))[:6],
        "rising": once(sorted((d for d in trending if d["trend"] >= TREND_PCT), key=lambda d: -d["trend"]))[:6],
        "fastest": once(sorted(fast, key=lambda d: d["days_to_sell"]))[:6],
        "haggle": once(sorted(haggle, key=lambda d: (-(d["cut_pct"] or 0), -(d.get("sitting_days") or 0))))[:6],
        "supply": once(sorted((d for d in priced if d.get("live")), key=lambda d: -d["live"]))[:6],
    }
    try:
        SNAPSHOT.write_text(json.dumps(data))
    except OSError:
        pass
    logger.info("Analytics rebuilt: %d listings over %d terms in %.1fs", data["listings"], len(out), data["seconds"])
    return data


def _tip(d: dict) -> str:
    t, fast, cut, sit = d.get("trend"), d.get("days_to_sell"), d.get("cut_pct"), d.get("sitting_days")
    if not d.get("count"):
        return "No listings yet."
    if d.get("mixed"):
        t = None  # a mix of different gear: its median moving isn't a trend
    if fast is not None and fast <= 3:
        lead = "Sells fast: message sellers the moment one shows up."
    elif t is not None and t <= -TREND_PCT:
        lead = "Prices falling: no rush, offer low."
    elif t is not None and t >= TREND_PCT:
        lead = "Prices rising: buy a good one soon."
    elif cut is not None and cut >= 25:
        lead = "Sellers often cut prices: make offers."
    elif sit is not None and sit >= 30:
        lead = "Ads sit a while: older ones are open to offers."
    else:
        lead = "Steady market."
    return lead


_state = {"running": False}


def load(cfg: dict, background: bool = True) -> Optional[dict]:
    """The last analytics (instantly), rebuilding in the background when
    it's more than an hour old. None until the first build finishes."""
    try:
        data = json.loads(SNAPSHOT.read_text())
    except Exception:
        data = None
    stale = not data or (datetime.now(timezone.utc) - _dt(data.get("built"))).total_seconds() > MAX_AGE
    if stale and not _state["running"]:
        if not background:
            return build(cfg)
        import threading

        def run():
            try:
                build(cfg)
            except Exception:
                logger.exception("Analytics build failed")
            finally:
                _state["running"] = False
        _state["running"] = True  # before the thread starts, so two page loads don't both build
        threading.Thread(target=run, daemon=True).start()
    return data
