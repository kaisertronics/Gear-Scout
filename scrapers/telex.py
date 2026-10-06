"""
Telex List sweep: actively searches each Telex term on the sites where a
listing can hide from the regular scrapes — the ones that are browsed as a
feed rather than read in full (Facebook Marketplace in all 8 regions, your
Facebook groups, OfferUp, Craigslist, Kijiji, ShopGoodwill, Reverb, eBay, Long &
McQuade). Each hourly cycle searches the next few terms, so the whole list
comes around several times a day. Finds are stored like any scrape result:
they show on the Telex page and the Dashboard and go into the next digest.
"""
import logging
import time
from datetime import datetime, timezone

from scrapers.store import _conn, mark_seen

logger = logging.getLogger(__name__)

SEARCH_TYPES = {"facebook_marketplace_region", "facebook", "offerup", "craigslist_region", "kijiji",
                "shopgoodwill", "long_mcquade"}
DEFAULT_PER_HOUR = 4


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS telex_state (
        term TEXT PRIMARY KEY, searched_at TEXT, found INTEGER, new INTEGER)""")


def terms(cfg: dict) -> list[str]:
    return [str(t).strip() for t in (cfg.get("telex_list") or []) if str(t).strip()]


def sweep_sources(cfg: dict) -> list[dict]:
    return [s for s in cfg.get("sources", []) if s.get("enabled", True) and (
        s.get("type") in SEARCH_TYPES or "reverb.com" in (s.get("url") or "")
        or "ebay.com" in (s.get("url") or ""))]


def state() -> dict[str, dict]:
    with _conn() as conn:
        _ensure(conn)
        return {t: {"searched_at": a, "found": f, "new": n}
                for t, a, f, n in conn.execute("SELECT term, searched_at, found, new FROM telex_state")}


def next_terms(cfg: dict, n: int) -> list[str]:
    """Never-searched terms first, then the ones searched longest ago."""
    st = state()
    return sorted(terms(cfg), key=lambda t: (st.get(t, {}).get("searched_at") or ""))[:n]


FB_TYPES = {"facebook_marketplace_region", "facebook"}


def search_term(term: str, cfg: dict, on_progress=None, fast_only: bool = False) -> dict:
    """Searches one term on every search-based site. fast_only leaves out
    Facebook (8 regions + groups take ~2 minutes per term)."""
    from scrapers.enrich import drop_excluded
    from scrapers.runner import run_sources
    sources = [s for s in sweep_sources(cfg) if not (fast_only and s.get("type") in FB_TYPES)]
    results = run_sources(sources, [term], cfg, on_progress=on_progress)
    found = new = 0
    for r in results:
        for l in drop_excluded(r.listings, cfg):
            found += 1
            if mark_seen(l) == "new":
                new += 1
    with _conn() as conn:
        _ensure(conn)
        conn.execute("INSERT OR REPLACE INTO telex_state VALUES (?, ?, ?, ?)",
                     (term, datetime.now(timezone.utc).isoformat(), found, new))
        conn.commit()
    return {"term": term, "found": found, "new": new}


# Correct spellings, and budget brands where a misspelled ad saves little.
_TYPO_SKIP = {"teletronix", "electrovoice", "behringher", "beringer", "behringner", "bheringer",
              "focusright", "presounus", "presonous"}


def typo_queries() -> list[str]:
    """Misspelled brand names sellers actually type ("nueman", "sennhieser",
    "telefunkin", "pulltec"), taken from the spelling fixes Gear Scout
    already applies when reading titles. Ads spelled like this rarely show up
    in buyers' searches, so they sit longer and sell cheaper."""
    import re
    from scrapers.base import _BRAND_FIXES
    out = []
    for pattern, _right in _BRAND_FIXES:
        group = re.search(r"\(\?:([^()]*)\)", pattern.pattern)
        for alt in (group.group(1).split("|") if group else []):
            alt = alt.replace("+", "")
            if re.fullmatch(r"[a-z]{5,}", alt) and alt not in _TYPO_SKIP:  # plain words, no "a.k.g."
                out.append(alt)
    return list(dict.fromkeys(out))


def misspellings(term: str, n: int = 2) -> list[str]:
    """The term with its brand misspelled the way sellers do ("Neumann U47"
    -> "Nueman U47", "Neuman U47")."""
    import re
    from scrapers.base import _BRAND_FIXES
    low = term.lower()
    out = []
    for pattern, right in _BRAND_FIXES:
        right = right.replace("-", " ")
        if right not in low.replace("-", " "):
            continue
        group = re.search(r"\(\?:([^()]*)\)", pattern.pattern)
        for alt in (group.group(1).split("|") if group else []):
            alt = alt.replace("+", "")
            if re.fullmatch(r"[a-z ]{4,}", alt) and alt != right:
                out.append(re.sub(re.escape(right), alt, low.replace("-", " "), count=1))
    return out[:n]


def search_links(term: str) -> list[tuple[str, str]]:
    """One-tap links to search a term yourself on sites Gear Scout reads only
    partly or can't read: Facebook Marketplace, eBay (sold prices and
    auctions ending soon), Reverb, HiBid / LiveAuctioneers / GovDeals, plus
    the misspelled versions on eBay and Marketplace."""
    from urllib.parse import quote_plus
    q = quote_plus(term)
    links = [("Marketplace", f"https://www.facebook.com/marketplace/search/?query={q}&sortBy=creation_time_descend"),
             ("eBay sold", f"https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1&_sop=13"),
             ("eBay auctions ending", f"https://www.ebay.com/sch/i.html?_nkw={q}&LH_Auction=1&_sop=1"),
             ("Reverb used", f"https://reverb.com/marketplace?query={q}&condition=used&sort=published_at%7Cdesc"),
             # Auction houses, estate and government-surplus sales: they don't
             # allow automated searching, so these open in your own browser.
             ("HiBid", f"https://hibid.com/lots?q={q}"),
             ("LiveAuctioneers", f"https://www.liveauctioneers.com/search/?keyword={q}&status=online"),
             ("GovDeals", f"https://www.govdeals.com/en/search?kWord={q}")]
    for typo in misspellings(term):
        tq = quote_plus(typo)
        links.append((f"“{typo}” eBay", f"https://www.ebay.com/sch/i.html?_nkw={tq}&_sop=10"))
        links.append((f"“{typo}” Marketplace", f"https://www.facebook.com/marketplace/search/?query={tq}"))
    return links


TYPO_PER_HOUR = 3


def run_typo_sweep(cfg: dict) -> list[dict]:
    """Searches a few misspelled brand names an hour on the fast sites
    (never Facebook), oldest first — every misspelling comes around about
    twice a day. Finds land in the normal listings, where titles are read as
    if spelled right, so they show on the Telex List, Steals and Dashboard."""
    if (cfg.get("telex_sweep") or {}).get("typos", True) is False:
        return []
    st = state()
    out = []
    for q in sorted(typo_queries(), key=lambda t: st.get(t, {}).get("searched_at") or "")[:TYPO_PER_HOUR]:
        try:
            res = search_term(q, cfg, fast_only=True)
            out.append(res)
            logger.info("Misspelling search %r: %d matches, %d new", q, res["found"], res["new"])
        except Exception:
            logger.exception("Misspelling search failed for %r", q)
    return out


def run_sweep(cfg: dict) -> list[dict]:
    try:
        from scrapers.market import refresh_telex_term_values
        refresh_telex_term_values(terms(cfg))
    except Exception:
        logger.exception("Telex term price lookup failed")
    out = _run_sweep(cfg)
    if groups(cfg):
        out += run_group_sweep(cfg)
    out += run_typo_sweep(cfg)
    return out


def _run_sweep(cfg: dict) -> list[dict]:
    per_hour = int((cfg.get("telex_sweep") or {}).get("per_hour", DEFAULT_PER_HOUR) or DEFAULT_PER_HOUR)
    out = []
    for term in next_terms(cfg, max(1, per_hour)):
        start = time.time()
        try:
            res = search_term(term, cfg)
            out.append(res)
            logger.info("Telex sweep %r: %d matches, %d new (%.0fs)", term, res["found"], res["new"], time.time() - start)
        except Exception:
            logger.exception("Telex sweep failed for %r", term)
    return out


# ---------------------------------------------------------------------------
# Wanted sweep: every Wanted term (your Telex List) searched directly, often.
# Each site group keeps its own clock per term, so a slow site never holds
# back a fast one.
# ---------------------------------------------------------------------------

FAST_EVERY_MIN = 30      # Reverb, Craigslist, Kijiji, ShopGoodwill, Long & McQuade, OfferUp
EBAY_EVERY_MIN = 120     # eBay's API has a daily allowance (sold checks use it too)
TYPO_EVERY_MIN = 360
FB_TERMS_PER_CYCLE = 4   # Marketplace (8 regions) + your groups: ~2 min a term
TYPOS_PER_CYCLE = 2
FAST_BUDGET_SECONDS = 600
SWEEP_MARKER = "/data/last_sweep.json"


def _site(source: dict) -> str:
    if source.get("type") in FB_TYPES:
        return "fb"
    if "ebay.com" in (source.get("url") or "") or source.get("type") == "ebay":
        return "ebay"
    return "fast"


def _ensure_sweep(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS wanted_sweep (
        term TEXT, site TEXT, searched_at TEXT, found INTEGER, new INTEGER, PRIMARY KEY (term, site))""")


def sweep_state() -> dict[tuple[str, str], dict]:
    with _conn() as conn:
        _ensure_sweep(conn)
        return {(t, s): {"searched_at": a, "found": f, "new": n}
                for t, s, a, f, n in conn.execute("SELECT term, site, searched_at, found, new FROM wanted_sweep")}


def search_query(term: str) -> str:
    """What to type into a site's search box for a term. A long,
    descriptive term ("Warm audio WA-412 API 4 channel pre") finds nothing
    as a whole phrase, so it's searched by its brand + model words — the
    same words that decide a match (see term_matcher)."""
    import re
    words = [w for w in term.split() if re.search(r"[A-Za-z0-9]", w)]
    if len(words) >= 4:
        key = [words[0]] + [w for w in words[1:] if re.search(r"\d", w) and len(w) >= 2]
        if len(key) >= 2:
            return " ".join(key)
    return term


def _search_sites(term: str, cfg: dict, sites: set[str]) -> dict:
    """Searches one term on the given site groups and stores what it finds."""
    from scrapers.enrich import drop_excluded
    from scrapers.runner import run_sources
    sources = [s for s in sweep_sources(cfg) if _site(s) in sites]
    site_of = {s["name"]: _site(s) for s in sources}
    results = run_sources(sources, [search_query(term)], cfg)
    counts = {s: [0, 0] for s in sites}
    for r in results:
        c = counts.setdefault(site_of.get(r.source_name, "fast"), [0, 0])
        for l in drop_excluded(r.listings, cfg):
            c[0] += 1
            if mark_seen(l) == "new":
                c[1] += 1
    now = datetime.now(timezone.utc).isoformat()
    found, new = sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())
    with _conn() as conn:
        _ensure_sweep(conn)
        _ensure(conn)
        for site, (f, n) in counts.items():
            conn.execute("INSERT OR REPLACE INTO wanted_sweep VALUES (?, ?, ?, ?, ?)", (term, site, now, f, n))
        conn.execute("INSERT OR REPLACE INTO telex_state VALUES (?, ?, ?, ?)", (term, now, found, new))
        conn.commit()
    return {"term": term, "sites": sorted(sites), "found": found, "new": new}


def run_wanted_sweep(cfg: dict) -> list[dict]:
    """One cycle (every 30 minutes): every Wanted term that's due on the
    fast sites (and eBay every 2 hours), then the few terms searched longest
    ago on Facebook, then a couple of misspelled versions of your terms."""
    import json
    from pathlib import Path
    from scrapers.wanted import terms as wanted_terms
    ts = wanted_terms(cfg)
    if not ts:
        return []
    try:
        from scrapers.market import refresh_telex_term_values
        refresh_telex_term_values(terms(cfg))
    except Exception:
        logger.exception("Telex term price lookup failed")
    wcfg = cfg.get("wanted") or {}
    fb_per_cycle = int(wcfg.get("fb_terms_per_cycle", FB_TERMS_PER_CYCLE))
    st = sweep_state()
    now = datetime.now(timezone.utc)

    def due(term, site, minutes):
        last = (st.get((term, site)) or {}).get("searched_at")
        if not last:
            return True
        return (now - datetime.fromisoformat(last)).total_seconds() >= minutes * 60 - 120

    def run(term, sites, label):
        t0 = time.time()
        try:
            res = _search_sites(term, cfg, sites)
            logger.info("Wanted sweep %s %r: %d matches, %d new (%.0fs)", label, term, res["found"], res["new"],
                        time.time() - t0)
            return res
        except Exception:
            logger.exception("Wanted sweep failed for %r (%s)", term, label)

    out, start = [], time.time()
    for term in sorted(ts, key=lambda t: (st.get((t, "fast")) or {}).get("searched_at") or ""):
        if time.time() - start > FAST_BUDGET_SECONDS:
            logger.info("Wanted sweep: time's up for this cycle; the rest go first next time")
            break
        sites = {s for s, m in (("fast", FAST_EVERY_MIN), ("ebay", EBAY_EVERY_MIN)) if due(term, s, m)}
        if sites:
            out.append(run(term, sites, "+".join(sorted(sites))))
    for term in sorted(ts, key=lambda t: (st.get((t, "fb")) or {}).get("searched_at") or "")[:max(0, fb_per_cycle)]:
        out.append(run(term, {"fb"}, "fb"))
    typos = list(dict.fromkeys(q for t in ts for q in misspellings(t)))
    for q in sorted(typos, key=lambda q: (st.get((q, "fast")) or {}).get("searched_at") or "")[:TYPOS_PER_CYCLE]:
        if due(q, "fast", TYPO_EVERY_MIN):
            out.append(run(q, {"fast"}, "misspelling"))
    try:
        Path(SWEEP_MARKER).write_text(json.dumps({"finished": datetime.now(timezone.utc).isoformat(),
                                                  "new": sum(r["new"] for r in out if r)}))
    except OSError:
        pass
    return [r for r in out if r]


def term_matcher(term: str):
    """A fast "does this (lower-cased, brand-spelling-fixed) title match the
    Telex term" test: every word in any order, like the live search. A long,
    descriptive term ("Warm audio WA-412 API 4 channel pre") rarely has every
    word in a title, so its brand + model numbers decide."""
    import re
    from scrapers.base import _keyword_pattern, _word_matches
    words = [w for w in term.lower().split() if re.search(r"[a-z0-9]", w)]
    if len(words) >= 4:
        key = [words[0]] + [w for w in words[1:] if re.search(r"\d", w) and len(w) >= 2]
        words = key if len(key) >= 2 else words
    exact = _keyword_pattern(term.lower())
    plain = max((w for w in words if w.isalpha() and len(w) >= 3), key=len, default=None)
    return lambda t: (plain is None or plain in t) and (
        bool(exact and exact.search(t)) or all(_word_matches(w, t) for w in words))


def model_matcher(term: str):
    """Stricter matcher for imported model lists (Telex groups): the brand
    (any of its spellings — "EV" for Electro-Voice) plus the exact model
    number as its own token. "Shure 300" doesn't match "$300" or "VA 300-C";
    "RCA 77-D" doesn't match a 77-DX; "RE-20" matches "RE20" and "RE 20"."""
    import re
    from scrapers.base import fix_brand_spelling
    from scrapers.enrich import _ALIAS_TO_BRAND, canonical_brand
    t = fix_brand_spelling(term.lower())
    words = t.split()
    model_i = next((i for i, w in enumerate(words) if re.search(r"\d", w)), None)
    if model_i is None:
        return term_matcher(term)  # no model number ("Reslo RB"): every word
    model = re.sub(r"/\d+$", "", words[model_i])
    extra = [w for w in words[model_i + 1:] if w.isalpha() and len(w) >= 3]  # "Turner 77 ribbon"
    runs = re.findall(r"[a-z]+|\d+", model)
    pat = re.compile(r"(?<![\w$])" + r"[- ]?".join(map(re.escape, runs)) + r"(?![\w]|-\w)")
    brand = canonical_brand(t)
    if brand:
        aliases = [a for a, c in _ALIAS_TO_BRAND.items() if c == brand]
    else:
        head = " ".join(words[:model_i]) or words[0]
        aliases = [head]
        if head in ("bang and olufsen", "bang & olufsen"):
            aliases += ["bang & olufsen", "b&o", "b & o", "bang olufsen"]
    brand_pat = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9])")
    digits = max(re.findall(r"\d+", model), key=len)
    return lambda x: (digits in x and bool(pat.search(x)) and bool(brand_pat.search(x))
                      and all(w in x for w in extra))


def groups(cfg: dict) -> list[tuple[str, list[str]]]:
    """Imported model lists on the Telex List ("Vintage mics (Cole Picks
    sold)"): matched against everything collected and swept a few at a time,
    but not added to every scrape (hundreds of terms would slow every scrape
    and use up eBay's daily allowance)."""
    out = []
    for g in cfg.get("telex_groups") or []:
        name = str(g.get("name") or "").strip()
        ts = [str(t).strip() for t in g.get("terms") or [] if str(t).strip()]
        if name and ts:
            out.append((name, ts))
    return out


GROUP_PER_HOUR = 10


def run_group_sweep(cfg: dict) -> list[dict]:
    """Searches the next few group models (never-searched first) on the
    fast sites — about 10 an hour, so a 400-model group comes around every
    couple of days."""
    st = state()
    pool = [t for _, ts in groups(cfg) for t in ts]
    per_hour = int((cfg.get("telex_sweep") or {}).get("group_per_hour", GROUP_PER_HOUR) or GROUP_PER_HOUR)
    out = []
    for term in sorted(dict.fromkeys(pool), key=lambda t: st.get(t, {}).get("searched_at") or "")[:per_hour]:
        start = time.time()
        try:
            res = search_term(term, cfg, fast_only=True)
            out.append(res)
            logger.info("Telex group sweep %r: %d matches, %d new (%.0fs)", term, res["found"], res["new"], time.time() - start)
        except Exception:
            logger.exception("Telex group sweep failed for %r", term)
    return out


# ---------------------------------------------------------------------------
# New-lowest-price alerts for every Telex term
# ---------------------------------------------------------------------------

def check_telex_lowest(cfg: dict) -> int:
    """For each Telex term: the lowest price currently listed anywhere (the
    real item — no parts, pedals/plugins unless the term asks, accessories,
    placeholders). When a listing comes in below the previous lowest, sends
    a phone push + email. The first check of a term only records its lowest.
    Uses listings already collected — no extra searching. Returns alerts sent."""
    import sqlite3
    from scrapers.base import fix_brand_spelling
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form,
                                 needs_repair, parse_price, quantity)
    from scrapers.lowest import notify_new_lowest
    from scrapers.market import load_market

    term_list = terms(cfg)
    if not term_list:
        return 0
    exclude = cfg.get("exclude_words") or []
    with _conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS telex_lowest (
            term TEXT PRIMARY KEY, lowest REAL, url TEXT, checked_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS telex_lowest_alerted (
            term TEXT, url TEXT, price REAL, PRIMARY KEY (term, url))""")
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT title, price, url, source_name, image_url, location, description FROM seen
               WHERE url IS NOT NULL AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0
               AND COALESCE(pending, 0) = 0 AND price IS NOT NULL""")]
        state_rows = {r["term"]: dict(r) for r in conn.execute("SELECT * FROM telex_lowest")}
        alerted = {(r["term"], r["url"]): r["price"] for r in conn.execute("SELECT * FROM telex_lowest_alerted")}
    texts = [fix_brand_spelling((r["title"] or "").lower()) for r in rows]
    market, index, similar = load_market(), price_index(), similar_index()
    now = datetime.now(timezone.utc).isoformat()
    sent = 0
    for term in term_list:
        match, form_wanted = term_matcher(term), item_form(term)
        best = None
        for r, text in zip(rows, texts):
            if not match(text):
                continue
            title = r["title"] or ""
            form = item_form(title)
            if (form in ("pedal", "plugin") and form != form_wanted) or is_partial(title) \
                    or is_accessory_only(title) or is_not_audio(title) or exclude_match(title, exclude) \
                    or (r.get("description") or "").startswith("Auction"):
                continue
            value = parse_price(r["price"])
            if not value or value < 20:
                continue
            n = quantity(title, r.get("description"))
            if n > 1:
                # Per piece — unless the price is close to one unit's usual
                # price, which means it's per piece already.
                c_q = evaluate(title, r["price"], r.get("description"), r["url"], market, index, similar)
                from scrapers.enrich import price_basis
                value = price_basis(title, r.get("description"), value, c_q["ref"] if c_q else None)[2]
            if best is not None and value >= best[0]:
                continue
            # Not a real price for the item: under a fifth of its comp.
            c = evaluate(title, r["price"], r.get("description"), r["url"], market, index, similar)
            if c and value < c["ref"] * 0.2:
                continue
            best = (value, r)
        if not best:
            continue
        value, r = best
        prev = state_rows.get(term)
        # Only worth a push when the new lowest is also 10%+ under what the
        # gear usually sells for (a new lowest that's still overpriced isn't news).
        c_best = evaluate(r["title"], r["price"], r.get("description"), r["url"], market, index, similar)
        real_deal = bool(c_best and not c_best["est"] and value <= c_best["ref"] * 0.9)
        if prev and prev["lowest"] and value < prev["lowest"] - 0.5 and r["url"] != prev["url"] and real_deal:
            key = (term, r["url"])
            if key not in alerted or value < (alerted[key] or 1e12) - 0.5:
                n = quantity(r["title"] or "")
                shown = f"{r['price']} ({n} × ${value:,.0f} each)" if n > 1 else r["price"]
                alert = {"title": r["title"], "price": shown, "url": r["url"], "source_name": r["source_name"],
                         "image_url": r.get("image_url"), "location": (r.get("location") or "").split(" @")[0] or None,
                         "needs_repair": needs_repair(r["title"], r.get("description")), "is_clone": False}
                try:
                    notify_new_lowest(cfg, term, alert, prev["lowest"], from_telex=True)
                    sent += 1
                except Exception:
                    logger.exception("Telex lowest-price alert failed for %r", term)
                with _conn() as conn:
                    conn.execute("INSERT OR REPLACE INTO telex_lowest_alerted VALUES (?, ?, ?)", (term, r["url"], value))
                    conn.commit()
        with _conn() as conn:
            conn.execute("INSERT OR REPLACE INTO telex_lowest VALUES (?, ?, ?, ?)", (term, value, r["url"], now))
            conn.commit()
    if sent:
        logger.info("Telex: %d new-lowest-price alerts sent", sent)
    return sent
