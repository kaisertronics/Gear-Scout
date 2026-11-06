"""Posting-time boost (owner proposal): learns the Pacific hours when ads for
the owner's gear (mics and the clone makers) get posted, weekday vs weekend, and
tells the sweep when it's a busy hour so the fast sites are checked every
15 minutes instead of 30.

The profile is rebuilt once a day from the listings table (one read, in the
scraper process, never on a page load) and saved to /data/posting_times.json.
hot_now() only reads that small file."""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
PROFILE = "/data/posting_times.json"
DAYS = 60
MIN_ADS = 150        # fewer ads than this and there's no pattern worth acting on
HOT_FACTOR = 1.3     # a bucket this far above the average hour is "busy"
REBUILD_S = 22 * 3600


def bucket(dt: datetime) -> str:
    """'wd-19' / 'we-10': weekday-or-weekend + Pacific hour."""
    from zoneinfo import ZoneInfo
    p = dt.astimezone(ZoneInfo("America/Los_Angeles"))
    return f"{'we' if p.weekday() >= 5 else 'wd'}-{p.hour}"


def hot_buckets(times: list) -> list:
    """Pure. times: aware datetimes of ads posted. Buckets (per hour of a weekday
    or weekend day) whose ads per day beat the average hour by HOT_FACTOR."""
    if len(times) < MIN_ADS:
        return []
    counts: dict = {}
    for t in times:
        b = bucket(t)
        counts[b] = counts.get(b, 0) + 1
    # per-day normalising: 5 weekdays vs 2 weekend days
    per_day = {b: n / (5 if b.startswith("wd") else 2) for b, n in counts.items()}
    avg = sum(per_day.values()) / 48
    return sorted(b for b, v in per_day.items() if v >= avg * HOT_FACTOR and counts[b] >= 5)


def _times() -> list:
    from scrapers import clone_makers
    from scrapers.store import DB_PATH
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("SELECT title, COALESCE(posted_at, first_seen) FROM seen WHERE first_seen >= ?"
                            " AND duplicate = 0 AND hidden = 0", (since,)).fetchall()
    finally:
        conn.close()
    out = []
    for title, at in rows:
        low = (title or "").lower()
        if not (clone_makers.maker_of(title) or "microphone" in low or " mic" in low or "ribbon" in low):
            continue
        try:
            d = datetime.fromisoformat(str(at))
        except ValueError:
            continue
        out.append(d if d.tzinfo else d.replace(tzinfo=timezone.utc))
    return out


def rebuild() -> dict:
    prof = {"built": datetime.now(timezone.utc).isoformat(), "hot": []}
    try:
        t = _times()
        prof.update(hot=hot_buckets(t), ads=len(t))
        tmp = PROFILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prof, f)
        os.replace(tmp, PROFILE)
    except Exception:
        logger.exception("Posting-time profile failed")
    return prof


def load() -> dict:
    try:
        return json.load(open(PROFILE))
    except Exception:
        return {}


def hot_now(refresh: bool = True) -> bool:
    """True when this Pacific hour is a busy posting hour. Rebuilds the profile
    when it's a day old (only the scraper passes refresh=True)."""
    prof = load()
    if refresh:
        try:
            age = time.time() - datetime.fromisoformat(prof["built"]).timestamp()
        except Exception:
            age = REBUILD_S + 1
        if age > REBUILD_S:
            prof = rebuild()
    return bucket(datetime.now(timezone.utc)) in (prof.get("hot") or [])


def summary() -> dict:
    """For the dashboard: {"hot_now": bool, "weekday": [hours], "weekend": [hours]} from the saved file only."""
    prof = load()
    hot = prof.get("hot") or []
    return {"hot_now": bucket(datetime.now(timezone.utc)) in hot,
            "weekday": sorted(int(b[3:]) for b in hot if b.startswith("wd")),
            "weekend": sorted(int(b[3:]) for b in hot if b.startswith("we")), "ads": prof.get("ads", 0)}
