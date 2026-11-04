"""Shared bits for the Home finders that hunt across ads (pair-up, lot hunter,
wanted-ad sniper, fixer-upper): one windowed read of the listings table, the
ready-to-send message wording, and the local-pickup test. Read-only; nothing
here sends anything."""
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

LOCAL_RE = re.compile(r"(?:FB |Facebook|Craigslist|OfferUp|Kijiji|seattle group)", re.I)


def is_local(source_name: Optional[str]) -> bool:
    return bool(LOCAL_RE.match(source_name or ""))


def recent_rows(days: int, like_terms: list, now: Optional[datetime] = None, limit: int = 4000,
                priced: Optional[bool] = None, local_only: bool = False, no_real_price: bool = False) -> list:
    """Live (not hidden / duplicate / sold / pending) US+Canada ads first seen in
    the last `days` days whose title contains any of `like_terms` (lower-case
    LIKE patterns). One range read on first_seen; newest first."""
    from scrapers.store import _conn
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    sql = ("SELECT * FROM seen WHERE first_seen >= ? AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
           " AND COALESCE(pending, 0) = 0 AND (country IS NULL OR country IN ('US', 'CA'))")
    args = [since]
    if priced is True:
        sql += " AND price IS NOT NULL AND price != ''"
    elif priced is False:
        sql += " AND (price IS NULL OR price = '')"
    if no_real_price:  # no price at all, or a $0-$5 stand-in
        sql += (" AND (price IS NULL OR price = '' OR (price NOT LIKE 'C%' AND"
                " CAST(REPLACE(REPLACE(price, '$', ''), ',', '') AS REAL) <= 5))")
    if local_only:
        sql += (" AND (source_name LIKE 'FB %' OR source_name LIKE 'Facebook%' OR source_name LIKE 'Craigslist%'"
                " OR source_name LIKE 'OfferUp%' OR source_name LIKE 'Kijiji%' OR source_name LIKE 'seattle group%')")
    if like_terms:
        sql += " AND (" + " OR ".join(["LOWER(title) LIKE ?"] * len(like_terms)) + ")"
        args += like_terms
    sql += " ORDER BY first_seen DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args)]


def total_cost(r: dict) -> Optional[float]:
    """Price + listed shipping in USD (None when there is no usable price)."""
    from scrapers.enrich import parse_price
    p = parse_price(r.get("price"))
    if not p or p <= 0:
        return None
    s = r.get("shipping")
    return p + (s if isinstance(s, (int, float)) and s > 0 else 0)


def clean(rows: list, cfg: dict) -> list:
    """Drops the ads Home never shows: exclude words, non-audio, 'sold' titles."""
    from scrapers.enrich import exclude_match, is_not_audio, title_says_sold
    ex = cfg.get("exclude_words") or []
    return [r for r in rows if not exclude_match(r.get("title") or "", ex) and not is_not_audio(r.get("title") or "")
            and not title_says_sold(r.get("title") or "")]


def short(title: Optional[str], n: int = 70) -> str:
    return re.sub(r"\s+", " ", title or "it").strip()[:n]


# The owner's buy list (docs/owner_purchases.md): classic-console outboard, 500 modules, classic mics, interfaces, monitors.
BUYLIST = re.compile(
    r"\b(?:1176|la[\s-]?2a|la[\s-]?3a|eqp[\s-]?1a|pultec|fairchild|660|670|260|bt[\s-]?50|spx\s?\d*|distressor|"
    r"capi|vc528|missing\s+link|api\s?(?:500|3\d\d|5\d\d)|500[\s-]?series|lunchbox|neve|1073|ssl|g[\s-]?buss|"
    r"apollo|oryx|antelope|hedd|genelec|neumann|u\s?47|u\s?67|u\s?87|km\s?84|grundig|advent|sony\s+c[\s-]?\d+|"
    r"c[\s-]?56|c[\s-]?800|peluso|vanguard|d12\s?vr|wa[\s-]?(?:412|19|47|73|76|2a)|warm\s+audio|hairball|"
    r"tape\s+machine|reel[\s-]to[\s-]reel|ribbon|avalon|daking|cranborne|manley|chandler|retro)\b", re.I)
