"""Price history per model: how the used asking price of a mic model has moved.

For each model (enrich.model_key, e.g. "neumann:u87ai") with enough ads in the
last HISTORY_DAYS days: the median asking price per month (per week when the
data is dense), a one-line verdict comparing the last 30 days with the whole
window, and, when sold-price memory (`confirmed_sales`) has 3+ sales of the
model, their median.

Ads are dated and filtered the way market_pulse does it (posted date when known,
else first_seen; hidden, duplicate, auction, Canadian and accessory-priced ads
left out). Reads the local DB only; no network calls. A model without MIN_BUCKETS
periods holding MIN_N ads each gets no chart at all: nothing beats a misleading
line from a handful of ads.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Optional

from scrapers.market_pulse import MIN_ASK, MIN_N, _ts

logger = logging.getLogger(__name__)

HISTORY_DAYS = 183   # about six months
MIN_BUCKETS = 3      # periods with MIN_N+ ads before a line is drawn
WEEKLY_MIN = 6       # weeks with MIN_N+ ads before the chart goes weekly
RECENT_DAYS = 30     # "asking now" = ads from this many days back
SAME_PCT = 3         # within this of the window median counts as "in line"
W, H, PAD = 240, 60, 4
_TTL = 30 * 60
_cache: dict = {"at": 0.0, "out": None}


def _bucket(when: datetime, now: datetime, weekly: bool) -> str:
    """Sort key for the period an ad falls in: ISO week start or YYYY-MM."""
    if weekly:
        return (now - timedelta(days=((now - when).days // 7 + 1) * 7)).strftime("%Y-%m-%d")
    return when.strftime("%Y-%m")


def series(items: list, now: datetime) -> list[dict]:
    """items = [(date, price)] -> [{label, n, median}] oldest first, weekly when
    at least WEEKLY_MIN weeks have MIN_N+ ads, else monthly; only periods with
    MIN_N+ ads are kept. Empty when fewer than MIN_BUCKETS periods remain."""
    for weekly in (True, False):
        buckets: dict = {}
        for when, price in items:
            buckets.setdefault(_bucket(when, now, weekly), []).append(price)
        pts = [{"label": k, "n": len(v), "median": round(median(v))}
               for k, v in sorted(buckets.items()) if len(v) >= MIN_N]
        if len(pts) >= (WEEKLY_MIN if weekly else MIN_BUCKETS):
            return pts
    return []


def verdict(items: list, now: datetime) -> Optional[str]:
    """One plain-English line: the last RECENT_DAYS days of asks against the
    median of the whole window, or None with fewer than MIN_N recent ads."""
    recent = [p for w, p in items if (now - w).days < RECENT_DAYS]
    if len(recent) < MIN_N or not items:
        return None
    base = median(p for _, p in items)
    pct = round((median(recent) - base) / base * 100) if base else 0
    span = f"{round(HISTORY_DAYS / 30)}-month median"
    if abs(pct) < SAME_PCT:
        return f"Asking prices are in line with the {span} (${round(base):,})."
    return f"Asking prices are {abs(pct)}% {'above' if pct > 0 else 'below'} the {span} (${round(base):,})."


def _scale(pts: list[dict]) -> list[dict]:
    """Add x/y (pixels in the W x H chart) to each point."""
    lo, hi = min(p["median"] for p in pts), max(p["median"] for p in pts)
    span = (hi - lo) or 1
    step = (W - 2 * PAD) / max(len(pts) - 1, 1)
    for i, p in enumerate(pts):
        p["x"] = round(PAD + i * step, 1)
        p["y"] = round(H - PAD - (p["median"] - lo) / span * (H - 2 * PAD), 1)
    return pts


def build(cfg: dict, now: Optional[datetime] = None) -> dict:
    """{model_key: {label, weekly, points:[{label, n, median, x, y}], line,
    lo, hi, verdict, sold:{median, n} or None}} for models with enough history."""
    from scrapers import mic_focus
    from scrapers.enrich import model_key, parse_price
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    floors = {t["key"]: max(t.get("min_price") or 0, MIN_ASK) for t in targets}
    cut = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    per: dict = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, price, COALESCE(NULLIF(posted_at, ''), first_seen), description FROM seen"
            " WHERE hidden = 0 AND duplicate = 0 AND price IS NOT NULL AND price NOT LIKE 'C$%'"
            " AND (country IS NULL OR country = 'US') AND COALESCE(NULLIF(posted_at, ''), first_seen) >= ?", (cut,)).fetchall()
        try:
            sales = conn.execute("SELECT title, price FROM confirmed_sales").fetchall()
        except Exception:
            sales = []  # table appears with the first recorded sale
    for title, price, found, desc in rows:
        if (desc or "").startswith("Auction"):
            continue  # a current bid isn't an asking price
        t = mic_focus.classify(title or "", targets)
        v, when = parse_price(price), _ts(found)
        mk = (model_key(title) or "") if t else ""
        if not mk or not v or not when or when > now or v < floors[t["key"]]:
            continue
        per.setdefault(mk, []).append((when, v))
    sold: dict = {}
    for title, price in sales:
        mk = model_key(title)
        if mk and price:
            sold.setdefault(mk, []).append(price)
    out = {}
    for mk, items in per.items():
        pts = series(items, now)
        if not pts:
            continue
        prices = [p["median"] for p in pts]
        _scale(pts)
        s = sold.get(mk) or []
        out[mk] = {"label": mk.split("|")[0].split(":")[-1].upper(), "weekly": len(pts[0]["label"]) == 10,
                   "points": pts, "line": " ".join(f"{p['x']},{p['y']}" for p in pts),
                   "lo": min(prices), "hi": max(prices), "verdict": verdict(items, now),
                   "sold": {"median": round(median(s)), "n": len(s)} if len(s) >= MIN_N else None}
    return dict(sorted(out.items(), key=lambda kv: -sum(p["n"] for p in kv[1]["points"])))  # best-covered first


def history(cfg: dict) -> dict:
    """build(), cached for 30 minutes; never raises ({} on trouble)."""
    if _cache["out"] is not None and time.time() - _cache["at"] < _TTL:
        return _cache["out"]
    try:
        out = build(cfg)
    except Exception:
        logger.exception("Price history failed")
        return {}
    _cache["out"], _cache["at"] = out, time.time()
    return out
