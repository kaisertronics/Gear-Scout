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
FEED_DAYS = 90  # still shown by a site within this many days (same as the database keeps them)


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


# Words that make a listing audio gear. A broad term ("DIY", "Golden age")
# or a short model code with no brand ("se8") only matches listings that
# look like gear — otherwise "DIY" brings tiny homes and "Golden age" die-cast
# cars, and "se8" a network switch.
_GEAR_WORD = re.compile(
    r"\b(?:mics?|microphones?|condensers?|ribbons?|dynamic mic|capsules?|pre-?amps?|pre-?amplifiers?|"
    r"compressors?|limiters?|eqs?|equali[sz]ers?|consoles?|mixers?|mixing|channel ?strips?|modules?|"
    r"500[\s-]?series|lunchbox|interfaces?|converters?|reverbs?|delays?|effects? processors?|processors?|"
    r"monitors?|speakers?|recorders?|reel[\s-]to[\s-]reel|tape (?:machine|deck)|amps?|amplifiers?|"
    r"transformers?|di box|direct box|patch ?bays?|outboard|pro audio|studio|recording|harmonizer|"
    r"synth(?:esizer)?s?|tube mic|valve mic|snakes?|multicore|breakout|patchbays?)\b", re.I)
_MODEL_CODE = re.compile(r"\b(?=[a-z-]*\d)(?=[\d-]*[a-z])[a-z0-9]+(?:-[a-z0-9]+)*\b", re.I)
_NOT_MODEL = re.compile(r"^\d+x\d+$|^\d+(?:in|ft|mm|cm|btu|k|gb|tb|mp|hp|pc|pcs|port|v|w)$", re.I)
# Dealers that only sell studio gear: anything there is gear. (Not Reverb:
# it also lists records, guitars and network switches.)
_GEAR_SITES = ("vintage king", "long & mcquade", "alto music", "rudy", "perfect circuit",
               "kijiji — pro audio", "groupdiy", "gear page", "fb — pro audio", "fb — vintage pro audio")


# A model written with a space ("Peluso SR 14", "Warm Audio Wa 19 N",
# "SSL AWS 948") — not "set of 6" / "no 5".
_SPACED_MODEL = re.compile(r"\b(?!(?:of|no|nr|x|and|for|to|in|at|by|on|or|the|qty|lot|set|pk|pcs?|ch|vol|ep|lp)\b)"
                           r"[a-z]{1,3}[\s-]\d{1,4}[a-z]?\b", re.I)


def _looks_like_gear(title: str, source: Optional[str], term: Optional[str] = None) -> bool:
    from scrapers.enrich import canonical_brand, is_generic_term
    if source and any(s in source.lower() for s in _GEAR_SITES):
        return True
    if _GEAR_WORD.search(title) or re.search(r"\bse electronics\b", title, re.I):
        return True
    has_model = any(not _NOT_MODEL.match(m.group(0)) for m in _MODEL_CODE.finditer(title)) \
        or bool(_SPACED_MODEL.search(title))
    if has_model and canonical_brand(title):
        return True
    # The ad leads with the one-word maker you asked for ("SSL Big Six",
    # "2 SSL Sixes", "Audioscape EQPA") — unlike "Bruce R. Stam oil painting".
    # Not phrases ("Golden age" pianos and comics) or kinds of gear ("DIY").
    if term and re.fullmatch(r"[A-Za-z]+", term.strip()) and not is_generic_term(term):
        return bool(re.match(r"\W*(?:\d+\s*x?\s+)?" + re.escape(term.strip().lower()) + r"\b", title.lower()))
    return False


def _needs_gear_check(term: str, specific: bool) -> bool:
    from scrapers.enrich import canonical_brand
    if not specific:
        return True
    compact = re.sub(r"[^a-z0-9]", "", term.lower())
    return len(compact) <= 4 and not canonical_brand(term)


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


# A term that IS a cable / snake / patchbay ("XLR snake", "Hosa DTP-805",
# "patchbay"): those listings are what you want, not accessories to skip,
# and they're often under the broad-term price floor.
_CABLE_TERM = re.compile(r"\b(?:cables?|snakes?|looms?|breakouts?|patch ?bays?|patchbays?|patch cables?|"
                         r"db-?25|harness|multicore|fan ?outs?|tie ?lines?|connectors?|jacks?|feed ?throughs?|"
                         r"canare|mogami|gotham|belden|neutrik|switchcraft|rean)\b|"
                         r"\b(?:dtp|css|stx|xlr|cpp)-?\d{3}|\b(?:nc3|na3|np3|ne8|nys|nl4)\w*", re.I)


def is_cable_term(term: str) -> bool:
    return bool(_CABLE_TERM.search(term))


# Gear that merely comes with a cable ("Shure PGA48 Microphone with XLR
# Cable", "Mackie 1402 Mixer + (14) XLR Cables", "Shure 55S … w XLR cable"):
# the mic or mixer is for sale, not cables. Decided by what comes before the
# first "with" / "+" / "," — a unit there ("Microphone") and nothing cable-ish
# ("XLR Mic & Instrument Cable", "Neutrik … Mic Female Panel Connectors",
# "Lot of XLR Microphone cables" stay cables).
_COMES_WITH = re.compile(r"\bw/|\bw\s|\bwith\b|\bincl(?:udes?|uded|uding|\.)?\b|\bplus\b|\band\b|\+|&|,|\s[-–]\s", re.I)
_STRONG_WITH = re.compile(r"^(?:w/|w\s|with|incl)", re.I)
_UNIT_WORD = re.compile(
    r"\b(?:microphones?|mics?|mixers?|recorders?|interfaces?|pre-?amps?|compressors?|consoles?|channel strips?|"
    r"speakers?|monitors?|headphones?|amplifiers?|amps?|synth(?:esizer)?s?|keyboards?|turntables?|modules?|"
    r"headsets?|switchers?)\b"
    r"(?!\s*(?:cables?|cords?|leads?|snakes?|splitters?|adapters?|connectors?))", re.I)
_CABLEISH = re.compile(r"\b(?:cables?|cabling|cords?|leads?|snakes?|looms?|connectors?|plugs?|jacks?|panels?|"
                       r"adapters?|interconnects?|pins?|female|male|lot|pack|set|canare|mogami|gotham|belden|"
                       r"neutrik|switchcraft|rean|hosa|pro-?co|monster|seismic|patch ?bays?|patchbays?|wall ?plates?|"
                       r"plates?|multicores?|breakouts?)\b", re.I)


def is_gear_with_cable(title: str) -> bool:
    m = _COMES_WITH.search(title)
    if not m:
        return False
    head = title[:m.start()]
    if not head.strip() or _CABLEISH.search(head):
        return False
    return bool(_UNIT_WORD.search(head) or _STRONG_WITH.match(m.group(0)))


def match(title: Optional[str], price: Optional[str], ms, source: Optional[str] = None) -> list[str]:
    """Wanted terms a listing title matches (empty = not wanted). Parts,
    accessories, and pedal/plugin versions (unless the term asks) never
    match; broad terms need a real price."""
    from scrapers.base import fix_brand_spelling
    from scrapers.enrich import is_accessory_only, is_partial, item_form, parse_price
    if not title:
        return []
    text = fix_brand_spelling(title.lower())
    hits = [m for m in ms if m[1](text)]
    if not hits or is_partial(title) or is_spare_tube(title):
        return []
    accessory = is_accessory_only(title)
    form = item_form(title)
    value = parse_price(price) or 0
    out = []
    for term, _, term_form, specific in hits:
        cable = is_cable_term(term)
        if accessory and not cable:
            continue
        if cable and not _UNIT_WORD.search(term) and is_gear_with_cable(title):
            continue
        if form in ("pedal", "plugin") and form != term_form:
            continue
        if not specific and value < (20 if cable else BROAD_MIN_PRICE):
            continue
        if _needs_gear_check(term, specific) and not _looks_like_gear(title, source, term):
            continue
        out.append(term)
    return out


def recent_rows(days: int = FEED_DAYS) -> list[dict]:
    """Every stored listing still for sale that a site has shown in the last
    `days` days (an ad up for months stays, as long as it's still listed)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            """SELECT * FROM seen WHERE url IS NOT NULL AND url != '' AND COALESCE(hidden, 0) = 0
               AND COALESCE(duplicate, 0) = 0 AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0
               AND COALESCE(last_seen, first_seen) >= ? AND (country IS NULL OR country IN ('US', 'CA'))
               ORDER BY first_seen DESC""", (since,)).fetchall()]


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
               AND (country IS NULL OR country IN ('US', 'CA'))
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
        found = match(title, r.get("price"), ms, r.get("source_name"))
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
                             url=r["url"], tags=["new"], image=r.get("image_url")):
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
