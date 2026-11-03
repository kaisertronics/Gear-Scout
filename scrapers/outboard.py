"""Outboard section (owner proposal, Nov 3): current non-mic outboard listings
for the owner's buy list (1176, LA-2A, Fairchild, EQP-1A / Pultec, API 500
modules, CAPI, Distressor, Warm Audio WA-412 / WA-2A, Neve, Neumann / Telex
preamps and EQs) that are asking less than their used value, newest first,
biggest discount first within a day.

Reads only what's already in the listings table and judges each one with
market_value.judge (the same used-value comparison the landing page uses).
Nothing here touches Mic Focus rules, thresholds or config.
"""
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DAYS = 60            # how far back a listing may have been posted
MIN_PCT = 20         # show anything at least this far under the used value (display only)
MIN_PRICE = 100

# The owner's buy list (docs/owner_purchases.md). Each entry: (label, pattern).
TARGETS = (
    ("1176", re.compile(r"\b(?:1176|1178)|\bhairball\b", re.I)),
    ("LA-2A", re.compile(r"\bla-?2a\b|\bwa-?2a\b|\bt-?4\b.*\bleveling\b", re.I)),
    ("Fairchild", re.compile(r"\bfairchild\b|\b670\b(?=.*(?:compressor|limiter))", re.I)),
    ("EQP-1A / Pultec", re.compile(r"\beqp-?1a\b|\bpultec\b|\bmeq-?5\b|\bwa-?(?:eqp|pultec)\b", re.I)),
    ("API / 500 module", re.compile(r"\bapi\b.*\b(?:500|512|312|525|550|560|3124|vision|lunchbox)|\b(?:512|525|550|560)[bc]?\b.*\bapi\b", re.I)),
    ("CAPI", re.compile(r"\bcapi\b|\bvc[\s-]?(?:528|502|504|508)\b|\bmissing link\b", re.I)),
    ("Distressor", re.compile(r"\bdistressor\b", re.I)),
    ("Warm WA-412", re.compile(r"\bwa-?412\b|\bwa-?273\b|\bwa-?12\b|\bwa-?76\b|\bwa-?2a\b", re.I)),
    ("Neve", re.compile(r"\bneve\b.*\b(?:1073|1084|1272|33609|31102|preamp|eq|module)", re.I)),
    ("Neumann / Telex", re.compile(r"\b(?:neumann|telefunken|siemens|telex)\b.*\b(?:v\d{2,3}|w\d{3}|eq|pre-?amp|compressor|u73|u84|ela)\b", re.I)),
    ("G Buss / SSL", re.compile(r"\bg[\s-]?buss\b|\bssl\b.*\b(?:compressor|bus comp|g\s?comp|xlogic)", re.I)),
)

# A mic with one of these words in the title is the mic, not outboard.
_MIC = re.compile(r"\bmicrophone\b|\bmic\b(?!\s?-?pre)|\bcondenser\b|\bribbon\b|\bcapsule\b|\bu\s?47\b|\bu\s?87\b|\bc-?12\b", re.I)
_OUTBOARD_WORD = re.compile(r"\b1176|\b(?:pre-?amps?|compressor|limiter|eq|equali[sz]er|module|channel ?strip|500|series|rack|"
                            r"1176|la-?2a|distressor|fairchild|eqp|pultec|wa-?412|1073|1084)\b", re.I)


# Frames, boards, pairs / "1 of 4" splits and small desktop clones aren't the unit being valued.
_NOT_UNIT = re.compile(r"\blunchbox\b|\bframes?\b|\bchassis\b|\bcircuit boards?\b|\bboards?\b|\bpairs?\b|\b\d\s?of\s?\d\b|"
                       r"\b\d\s?units\b|\bhotone\b|\bkomp\b|\bplug-?ins?\b|\bclone kit\b|\bdiy\b", re.I)


def target_of(title: str):
    """The buy-list label this title matches, or None."""
    if not title or _NOT_UNIT.search(title) or(_MIC.search(title) and not re.search(r"\bpre-?amp|compressor|limiter|distressor\b", title, re.I)):
        return None
    if not _OUTBOARD_WORD.search(title):
        return None
    for label, pat in TARGETS:
        if pat.search(title):
            return label
    return None


def find(cfg: dict) -> list[dict]:
    """Live US listings that match the buy list and ask at least MIN_PCT under
    their used value, newest first (then biggest discount)."""
    from scrapers import market_value as MV
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form,
                                 needs_repair, parse_price, price_basis, title_says_sold)
    from scrapers.store import _conn
    cut = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
            " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
            " AND price NOT LIKE 'C$%' AND (country IS NULL OR country = 'US')"
            " AND url NOT LIKE '%search/posts%' AND COALESCE(description, '') NOT LIKE 'Auction%'"
            " AND COALESCE(posted_at, first_seen) >= ?", (cut,))]
    exclude_words = cfg.get("exclude_words") or []
    out, seen_ads = [], set()
    for r in rows:
        title = r.get("title") or ""
        label = target_of(title)
        if not label:
            continue
        if MV.skip_listing(title) or title_says_sold(title) or exclude_match(title, exclude_words) \
                or is_accessory_only(title) or is_partial(title) or is_not_audio(title) \
                or needs_repair(title, r.get("description")) or item_form(title) in ("plugin", "pedal", "kit"):
            continue
        ask = parse_price(r.get("price"))
        if not ask or ask < MIN_PRICE:
            continue
        _, qty, unit = price_basis(title, None, ask)
        unit = unit or ask
        ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
        delivered = round(unit + (ship or 0) / max(int(qty or 1), 1), 2)
        j = MV.judge(title, delivered, r.get("description"))
        if j["kind"] == "no_value" or j["pct"] is None or j["pct"] < MIN_PCT:
            continue
        ad = (title.lower().strip(), round(ask))
        if ad in seen_ads:
            continue
        seen_ads.add(ad)
        r["outboard"] = label
        r["deal"] = dict(j, delivered=delivered)
        r["_when"] = r.get("posted_at") or r.get("first_seen") or ""
        out.append(r)
    out.sort(key=lambda r: (str(r["_when"])[:10], r["deal"]["pct"]), reverse=True)
    return out
