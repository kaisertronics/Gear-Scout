"""
The Wanted list — your Telex List terms, and nothing else — and which
stored listings match it. This is the one definition the Feed, the
"new match" phone alerts and the fast searching all share, so they can't
disagree about what you're looking for.

Imported model groups (Cole Picks etc.) and the long keyword list in
config.yaml are NOT part of it: they still feed price comparisons, but
they don't put listings in front of you.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from scrapers.store import _conn

logger = logging.getLogger(__name__)

ALERT_STATE = Path("/data/wanted_alert.json")
# Broad terms (a bare brand, "ribbon mic") match lots of cheap odds and ends:
# those need at least this price to count. Specific models match at any price.
BROAD_MIN_PRICE = 150
FEED_DAYS = 45  # how far back (found by Gear Scout) the feed looks


def terms(cfg: dict) -> list[str]:
    """Telex List entries, with comma-separated entries ("lexicon 300,
    lexicon 480") split into separate terms."""
    out, seen = [], set()
    for entry in cfg.get("telex_list") or []:
        for t in str(entry).split(","):
            t = t.strip()
            if len(t) >= 2 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return out


def matchers(cfg: dict) -> list[tuple[str, Callable[[str], bool], Optional[str], bool]]:
    """[(term, match(text), item form the term asks for, is_specific)]"""
    from scrapers.board import _is_specific
    from scrapers.enrich import item_form
    from scrapers.telex import term_matcher
    return [(t, term_matcher(t), item_form(t), _is_specific(t)) for t in terms(cfg)]


# Spare vacuum tubes whose ads stuff model names into the title ("4 NOS
# Telefunken EF804 /Grail Mic Tube /V72 V76") — the tube, not the gear.
_TUBE_TYPE = re.compile(r"\b(?:ef ?8[0-9]{1,2}s?|ef ?14|ef ?12|vf ?14m?|ac ?701k?|12a[xtu]7|ecc ?8[1-3]|e8[0-9]cc|"
                        r"6072a?|5751|6sj7|6sn7|el ?34|el ?84|6l6|kt ?88|5ar4|gz ?34|6v6|6au6|6as7)\b", re.I)
_TUBE_SALE = re.compile(r"\b(?:nos|new old stock|tubes|valves?|matched (?:pair|set|quad)|sealed box|tested|"
                        r"(?:mic|amp|preamp) tube)\b", re.I)
_WHOLE_UNIT = re.compile(r"\b(?:amplifier|preamp(?:lifier)?|microphone|compressor|limiter|equali[sz]er|module|"
                         r"channel strip|console|mixer|rack)\b", re.I)


def is_spare_tube(title: str) -> bool:
    return bool(_TUBE_TYPE.search(title) and _TUBE_SALE.search(title) and not _WHOLE_UNIT.search(title))


def match(title: Optional[str], price: Optional[str], ms) -> list[str]:
    """Wanted terms a listing title matches (empty = not wanted). Parts,
    accessories, and pedal/plugin versions (unless the term asks) never
    match; broad terms need a real price."""
    from scrapers.base import fix_brand_spelling
    from scrapers.enrich import is_accessory_only, is_partial, item_form, parse_price
    if not title:
        return []
    text = fix_brand_spelling(title.lower())
    hits = [m for m in ms if m[1](text)]
    if not hits or is_partial(title) or is_accessory_only(title) or is_spare_tube(title):
        return []
    form = item_form(title)
    value = parse_price(price) or 0
    out = []
    for term, _, term_form, specific in hits:
        if form in ("pedal", "plugin") and form != term_form:
            continue
        if not specific and value < BROAD_MIN_PRICE:
            continue
        out.append(term)
    return out


def recent_rows(days: int = FEED_DAYS) -> list[dict]:
    """Every stored listing still for sale found in the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND COALESCE(hidden, 0) = 0
               AND COALESCE(duplicate, 0) = 0 AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0
               AND first_seen >= ? ORDER BY first_seen DESC""", (since,)).fetchall()]


def posted_age_days(row: dict) -> Optional[float]:
    for field in ("posted_at", "first_seen"):
        try:
            d = datetime.fromisoformat(row.get(field) or "")
        except (TypeError, ValueError):
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)
    return None


# ---------------------------------------------------------------------------
# Phone alert for every new Wanted match, within minutes of it being found
# ---------------------------------------------------------------------------

ALERT_MAX_AGE_DAYS = 7    # an ad the seller posted weeks ago isn't "new" news
ALERT_MAX_SINGLE = 6      # more than this in one go → one summary push


def alert_new(cfg: dict) -> int:
    """Pushes each listing found since the last check that matches the
    Wanted list. The first run only sets the starting point."""
    from scrapers.enrich import exclude_match, is_not_audio, title_says_sold
    from scrapers.notify import push_enabled, send_push
    now = datetime.now(timezone.utc).isoformat()
    try:
        since = json.loads(ALERT_STATE.read_text())["since"]
    except Exception:
        ALERT_STATE.write_text(json.dumps({"since": now}))
        return 0
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM seen WHERE first_seen > ? AND url IS NOT NULL AND COALESCE(hidden, 0) = 0
               AND COALESCE(duplicate, 0) = 0 AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0
               ORDER BY first_seen""", (since,)).fetchall()]
    newest = max([r["first_seen"] for r in rows], default=since)
    ms = matchers(cfg)
    exclude = cfg.get("exclude_words") or []
    hits, seen_ads = [], set()
    for r in rows:
        title = r.get("title") or ""
        if exclude_match(title, exclude) or is_not_audio(title) or title_says_sold(title):
            continue
        age = posted_age_days(r)
        if age is not None and age > ALERT_MAX_AGE_DAYS:
            continue
        found = match(title, r.get("price"), ms)
        ad = (title.lower().strip(), r.get("price"))
        if found and ad not in seen_ads:
            seen_ads.add(ad)
            hits.append((r, found))
    sent = 0
    if hits and push_enabled(cfg):
        if len(hits) <= ALERT_MAX_SINGLE:
            for r, found in hits:
                where = (r.get("location") or "").split(" @")[0] or r.get("source_name") or ""
                if send_push(cfg, f"New: {r['title'][:80]}",
                             f"{r.get('price') or 'no price'} · {where}\nWanted: {found[0]}",
                             url=r["url"], tags=["new"]):
                    sent += 1
        else:
            lines = [f"• {r['title'][:60]} — {r.get('price') or '?'}" for r, _ in hits[:12]]
            more = f"\n…and {len(hits) - 12} more" if len(hits) > 12 else ""
            dash = ((cfg.get("email") or {}).get("dashboard_url") or "").rstrip("/") or None
            if send_push(cfg, f"{len(hits)} new on your Wanted list", "\n".join(lines) + more,
                         url=dash, tags=["new"]):
                sent = len(hits)
        logger.info("Wanted: %d new matches, %d pushed", len(hits), sent)
    ALERT_STATE.write_text(json.dumps({"since": newest}))
    return sent
