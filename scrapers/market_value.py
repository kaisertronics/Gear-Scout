"""
Used market value per exact model: the middle of what single, used, US
listings of that model have asked over the last year, with the extreme
highs and lows trimmed. Needs 3+ listings, else there's no value.

Why (Oct 31 audit): a quarter of the old values were 50%+ off: Telex List
target prices and new prices used as "used", pairs priced as one mic,
bundles and vintage/reissue/clone models lumped together. This leaves out
pairs, lots, bundles, kits, new-in-box ads, parts and accessories, and
counts an ad posted on several sites once.

Also builds the landing page's "best deals": everything on your Wanted list
posted in the last LANDING_DAYS days, judged the same way.
"""
import logging
import re
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MIN_N = 3              # listings needed before a value counts
YEAR_DAYS = 365
DEAL_PCT = 50          # a deal is at least this far under the used value
TOO_GOOD_PCT = 75      # past this it's usually a scam or a mix-up: "check these"
MIN_DEAL_PRICE = 100   # 50% off a $60 item isn't worth a slot
LANDING_DAYS = 150

_SKIP = re.compile(
    r"\bpairs?\b|\(2\)|\b2\s?x\b|\bx\s?2\b|\b\d\s?-?(?:pack|pk)\b|\blot\b|\bset of\b|\bbundle\b|\bkit\b|"
    r"\bbrand new\b|\bnew in box\b|\bnib\b|\[new\]|\bsealed\b|\bunopened\b|\bmatched\b|\bquad\b|\btrio\b|"
    r"&|\bwith .*\band\b", re.I)

_cache: dict = {"at": 0.0, "idx": None, "sold": None, "tiers": None}
_build_lock = threading.Lock()

_MINT = re.compile(r"\bmint\b|\blike[- ]new\b|\bexcellent condition\b|\bpristine\b|\bbarely used\b|\bopen[- ]box\b", re.I)
_ROUGH = re.compile(r"\bas[- ]is\b|\bcosmetic (?:wear|damage)\b|\bheavy wear\b|\bscratched\b|\bdents?\b|\bwell used\b|"
                    r"\brough\b|\bbeat[- ]up\b|\bworn\b", re.I)


def _tier(title: Optional[str], desc: Optional[str] = None) -> str:
    """'mint', 'rough' or 'normal' from the title, then the first 300 chars
    of the description. Rough beats mint."""
    text = (title or "") + "\n" + (desc or "")[:300]
    if _ROUGH.search(text):
        return "rough"
    return "mint" if _MINT.search(text) else "normal"


def _ensure_sales(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS confirmed_sales (url TEXT PRIMARY KEY, title TEXT, price REAL, at TEXT)")


def record_sale(url: str, amount) -> None:
    """A Reverb listing that really sold (its API says "sold", not "ended"):
    keep its last price. Reverb no longer shows accepted-offer prices or its
    price guide, so these are the best sold prices there are."""
    from scrapers.store import _conn
    try:
        price = float(amount)
    except (TypeError, ValueError):
        return
    with _conn() as conn:
        _ensure_sales(conn)
        row = conn.execute("SELECT title FROM seen WHERE url = ?", (url,)).fetchone()
        if row and row[0]:
            conn.execute("INSERT OR IGNORE INTO confirmed_sales VALUES (?, ?, ?, ?)",
                         (url, row[0], price, datetime.now(timezone.utc).isoformat()))
            conn.commit()


# A channel strip, module or fader from a console isn't the console.
_MODULE = re.compile(r"\bchannel ?strips?\b|\bmodules?\b|\bbuckets?\b|\bchannels?\b(?! ?(?:mic|pre))|\bcards?\b", re.I)
_PART_WORDS = re.compile(r"\bfaders?\b|\bknobs?\b|\bmeters?\b|\bbezel\b|\bpcb\b|\bfaceplate\b|\bpower supply\b|"
                         r"\bpsu\b|\bcapsule\b|\bschematics?\b|\bmanual\b|\bbrochure\b", re.I)
# "3 brand new in box…", "lot of 4": a price for several that can't be split reliably
_SEVERAL = re.compile(r"^\s*\(?\d+\)?\s+(?:brand new|new|nos|x\b)|\blot\b|\bset of [3-9]\b|\b[3-9]\s?x\b|\bx\s?[3-9]\b", re.I)


def _key(title: str) -> Optional[str]:
    from scrapers.enrich import model_key
    k = (model_key(title) or "").split("|")[0] or None
    if k and _MODULE.search(title):
        k += ":module"
    return k


def skip_listing(title: str) -> bool:
    """Parts, and ads selling several at one price — judged neither way."""
    return bool(_PART_WORDS.search(title) or _SEVERAL.search(title))


def index(max_age: float = 1800) -> dict:
    """{model key: (value, n)} — rebuilt at most every 30 minutes."""
    if _cache["idx"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["idx"]
    # One thread rebuilds; the rest serve the old index (or wait on first build)
    # instead of all rebuilding at once and stalling the dashboard.
    if _cache["idx"] is not None:
        if not _build_lock.acquire(blocking=False):
            return _cache["idx"]
    else:
        _build_lock.acquire()
    try:
        if _cache["idx"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["idx"]
        return _build_index()
    finally:
        _build_lock.release()


def _build_index() -> dict:
    from scrapers.enrich import is_accessory_only, is_partial, needs_repair, parse_price
    from scrapers.store import _conn
    cut = (datetime.now(timezone.utc) - timedelta(days=YEAR_DAYS)).isoformat()
    asks: dict = {}
    tier_asks: dict = {}
    seen_ads = set()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, price, description FROM seen WHERE first_seen >= ? AND price IS NOT NULL"
            " AND duplicate = 0 AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND COALESCE(description, '') NOT LIKE 'Auction%'", (cut,)).fetchall()
    for title, price, desc in rows:
        if not title or _SKIP.search(title) or _PART_WORDS.search(title) or is_partial(title) or is_accessory_only(title) \
                or needs_repair(title, desc):
            continue
        v = parse_price(price)
        if not v or v < 25:
            continue
        ad = (re.sub(r"\s+", " ", title.lower()).strip(), round(v))
        if ad in seen_ads:
            continue
        seen_ads.add(ad)
        k = _key(title)
        if k:
            asks.setdefault(k, []).append(v)
            tier_asks.setdefault(k, {}).setdefault(_tier(title, desc), []).append(v)
    with _conn() as conn:
        _ensure_sales(conn)
        sales = conn.execute("SELECT title, price FROM confirmed_sales WHERE at >= ?", (cut,)).fetchall()
    sold: dict = {}
    for title, v in sales:
        if title and v and not _SKIP.search(title) and not is_partial(title) and not is_accessory_only(title):
            k = _key(title)
            if k:
                sold.setdefault(k, []).append(v)
    _cache["sold"] = {k: (round(statistics.median(vs)), len(vs)) for k, vs in sold.items()}
    idx = {}
    for k, vs in asks.items():
        vs.sort()
        cut_n = len(vs) // 10 if len(vs) >= 10 else (1 if len(vs) >= 5 else 0)
        core = vs[cut_n:len(vs) - cut_n] if cut_n else vs
        idx[k] = (round(statistics.median(core)), len(vs))
    tiers = {}
    for k, by in tier_asks.items():
        tiers[k] = {t: (round(statistics.median(vs)), len(vs)) for t, vs in by.items()}
    _cache.update(at=time.time(), idx=idx, tiers=tiers)
    return idx


def value(title: str, desc: Optional[str] = None) -> tuple[Optional[float], int]:
    """(used value, listings behind it) — value is None under MIN_N.
    3+ confirmed Reverb sales of the model beat asking prices (and only ever
    lower the value: what sold is the real market).
    A mint or rough ad is compared with same-condition ads when there are
    MIN_N+ of them, never above the overall value (rough) or below it (mint)."""
    k = _key(title or "")
    if not k:
        return None, 0
    v, n = index().get(k, (None, 0))
    base, bn = _overall(k, v, n)
    t = _tier(title, desc)
    if t != "normal" and base:
        tv, tn = ((_cache.get("tiers") or {}).get(k) or {}).get(t, (None, 0))
        if tv and tn >= MIN_N:
            sv, sn = (_cache.get("sold") or {}).get(k, (None, 0))
            if sn >= MIN_N:
                tv = min(tv, sv)
            tv = min(tv, base) if t == "rough" else max(tv, base)
            return tv, tn
    return base, bn


def _overall(k, v, n):
    sv, sn = (_cache.get("sold") or {}).get(k, (None, 0))
    if sn >= MIN_N:
        return (min(sv, v) if v and n >= MIN_N else sv), sn
    return (v if n >= MIN_N else None), n


def basis(title: str) -> str:
    k = _key(title or "")
    sn = (_cache.get("sold") or {}).get(k, (None, 0))[1] if k else 0
    return f"{sn} sales" if sn >= MIN_N else f"{index().get(k, (None, 0))[1] if k else 0} asking prices"


def judge(title: str, unit: float, desc: Optional[str] = None) -> dict:
    """{'used', 'n', 'pct', 'kind'}: kind is 'deal' (50–75% under), 'too_good'
    (over 75%), 'no_value' (too few listings) or None (not a deal)."""
    used, n = value(title, desc)
    if not used:
        return {"used": None, "n": n, "pct": None, "kind": "no_value", "basis": ""}
    pct = round((1 - unit / used) * 100)
    kind = "too_good" if pct > TOO_GOOD_PCT else ("deal" if pct >= DEAL_PCT else None)
    return {"used": used, "n": n, "pct": pct, "kind": kind, "basis": basis(title), "tier": _tier(title, desc)}


def landing_deals(cfg: dict) -> list[dict]:
    """Best deals on everything you hunt: live US listings matching your
    Wanted list, posted in the last LANDING_DAYS days, 50–75% under their
    used value, biggest discount first."""
    from scrapers import wanted
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form, needs_repair,
                                 parse_price, price_basis, title_says_sold, wants_pedals)
    from scrapers.store import _conn
    cut = (datetime.now(timezone.utc) - timedelta(days=LANDING_DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
            " AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND url NOT LIKE '%search/posts%' AND COALESCE(description, '') NOT LIKE 'Auction%'"
            " AND COALESCE(posted_at, first_seen) >= ?", (cut,))]
    terms = wanted.matchers(cfg)
    exclude_words = cfg.get("exclude_words") or []
    pedals_ok = wants_pedals(cfg)
    out, seen_ads = [], set()
    for r in rows:
        title = r.get("title") or ""
        src = (r.get("source_name") or "").lower()
        if "kijiji" in src or "mcquade" in src:
            continue
        if skip_listing(title) or title_says_sold(title) or exclude_match(title, exclude_words) or is_accessory_only(title) \
                or is_partial(title) or is_not_audio(title) or needs_repair(title, r.get("description")):
            continue
        form = item_form(title)
        if form == "plugin" or (form == "pedal" and not pedals_ok):
            continue
        value_ = parse_price(r.get("price"))
        if not value_ or value_ < MIN_DEAL_PRICE:
            continue
        if not wanted.match(title, r.get("price"), terms, r.get("source_name")):
            continue
        _, qty, unit = price_basis(title, None, value_)
        unit = unit or value_
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        delivered = round(unit + (ship or 0) / max(int(qty or 1), 1), 2)
        j = judge(title, delivered, r.get("description"))
        if j["kind"] != "deal":
            continue
        ad = (title.lower().strip(), round(value_))
        if ad in seen_ads:
            continue
        seen_ads.add(ad)
        r["deal"] = dict(j, delivered=delivered)
        out.append(r)
    out.sort(key=lambda r: -r["deal"]["pct"])
    return out
