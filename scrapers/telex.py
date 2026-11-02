"""
Telex List = your Wanted list (see scrapers/wanted.py for what counts as a
match). This module searches each term directly on the sites that have a
search box — Reverb, eBay, Craigslist, Kijiji, ShopGoodwill, Long & McQuade,
OfferUp every 30 minutes (eBay every 2 hours) and Facebook Marketplace (8
regions) + your groups in rotation — and keeps the per-term, per-site
search times the Health page shows. Finds are stored like any scrape result.
Also: the Telex List's new-lowest-price alerts and its imported model groups
(reference only).
"""
import logging
import re
import time
from datetime import datetime, timezone

from scrapers.store import _conn, mark_seen

logger = logging.getLogger(__name__)

SEARCH_TYPES = {"facebook_marketplace_region", "facebook", "offerup", "craigslist_region", "kijiji",
                "shopgoodwill", "long_mcquade"}


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS telex_state (
        term TEXT PRIMARY KEY, searched_at TEXT, found INTEGER, new INTEGER)""")


def terms(cfg: dict) -> list[str]:
    # One list everywhere: comma-joined entries split ("lexicon 300, lexicon 480").
    from scrapers.wanted import terms as wanted_terms
    return wanted_terms(cfg)


def sweep_sources(cfg: dict) -> list[dict]:
    return [s for s in cfg.get("sources", []) if s.get("enabled", True) and (
        s.get("type") in SEARCH_TYPES or "reverb.com" in (s.get("url") or "")
        or "ebay.com" in (s.get("url") or ""))]


def state() -> dict[str, dict]:
    with _conn() as conn:
        _ensure(conn)
        return {t: {"searched_at": a, "found": f, "new": n}
                for t, a, f, n in conn.execute("SELECT term, searched_at, found, new FROM telex_state")}


FB_TYPES = {"facebook_marketplace_region", "facebook"}


def search_term(term: str, cfg: dict, on_progress=None, fast_only: bool = False) -> dict:
    """Searches one term now (the Telex page's search buttons): the fast
    sites and eBay, plus Facebook (8 regions + groups, ~2 minutes) unless
    fast_only. Same search as the automatic 30-minute sweep."""
    sites = {"fast", "ebay"} if fast_only else {"fast", "ebay", "fb"}
    res = _search_sites(term, cfg, sites)
    return {"term": term, "found": res["found"], "new": res["new"]}

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


# ---------------------------------------------------------------------------
# Wanted sweep: every Wanted term (your Telex List) searched directly, often.
# Each site group keeps its own clock per term, so a slow site never holds
# back a fast one.
# ---------------------------------------------------------------------------

FAST_EVERY_MIN = 30      # Reverb, Craigslist, Kijiji, ShopGoodwill, Long & McQuade, OfferUp
EBAY_EVERY_MIN = 120     # eBay's API has a daily allowance (sold checks use it too)
TYPO_EVERY_MIN = 360
FB_CABLE_EVERY_MIN = 72 * 60  # cable/connector terms on Facebook (see run_wanted_sweep)
FB_TERMS_PER_CYCLE = 2  # Marketplace (8 regions) + your groups: ~2 min a term (gentler on the account)
TYPOS_PER_CYCLE = 2
FAST_BUDGET_SECONDS = 600
CYCLE_TARGET_SECONDS = 840  # refresh + sweep + Facebook should end before the next 15-minute start
FB_SECONDS_PER_TERM = 150  # one Facebook term (Marketplace regions + groups) takes ~2-2.5 min
SITE_WORKERS = 6  # sites searched at once for one term (not Facebook)
CANADIAN_TYPES = {"kijiji", "long_mcquade"}
TERMS_AT_ONCE = 2  # wanted-sweep terms searched in parallel (not Facebook)
SWEEP_MARKER = "/data/last_sweep.json"


def _site(source: dict) -> str:
    if source.get("type") in FB_TYPES:
        return "fb"
    if "ebay.com" in (source.get("url") or "") or source.get("type") == "ebay":
        return "ebay"
    return "fast"


def _cable_part_number(term: str) -> bool:
    """A cable / connector term naming a part number ("Neutrik NC3FXX",
    "Hosa DTP-805", "Canare L-4E6S"), not a kind of cable ("XLR cable")."""
    from scrapers.wanted import is_cable_term
    return is_cable_term(term) and any(re.search(r"\d", w) and re.search(r"[a-z]", w, re.I) and
                                       not re.fullmatch(r"db-?25", w, re.I) for w in term.split())


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
    words = [w for w in term.split() if re.search(r"[A-Za-z0-9]", w) and not w.startswith("-")]
    if len(words) >= 4:
        key = [words[0]] + [w for w in words[1:] if re.search(r"\d", w) and len(w) >= 2]
        if len(key) >= 2:
            return " ".join(key)
    return " ".join(words) if words else term


def _search_sites(term: str, cfg: dict, sites: set[str]) -> dict:
    """Searches one term on the given site groups and stores what it finds."""
    from scrapers.enrich import drop_excluded
    from scrapers.runner import run_sources
    from scrapers.fb_posts_background import facebook_paused, pause_facebook
    if "fb" in sites and facebook_paused():
        sites = sites - {"fb"}  # Facebook asked to slow down: wait out the pause
    sources = [s for s in sweep_sources(cfg) if _site(s) in sites
               and s.get("type") not in CANADIAN_TYPES]  # owner excludes Canadian stores
    site_of = {s["name"]: _site(s) for s in sources}
    kw = [search_query(term)]
    from scrapers import mic_focus
    if mic_focus.enabled(cfg) or term in mic_focus.sweep_terms(cfg):
        from scrapers.base import LooseTerm
        kw = LooseTerm(kw)  # "dachman microphone" also keeps "Dachman DA87"
    # Non-Facebook sites can run many at once; Facebook stays gentle (one at a time).
    results = run_sources(sources, kw, cfg, other_workers=int(
        (cfg.get("wanted") or {}).get("site_workers", SITE_WORKERS)))
    if "fb" in sites and any(not r.success and re.search(r"checkpoint|temporarily blocked|slow down|confirm",
                                                          f"{r.error} {r.fix_hint}", re.I)
                             for r in results if site_of.get(r.source_name) == "fb"):
        pause_facebook("Facebook asked to slow down or confirm the account", cfg)
    mp = [r for r in results if r.source_name.startswith("FB Marketplace")]
    if mp and all((r.fix_hint or "") == "EMPTY_MARKETPLACE_SEARCH" for r in mp):
        from scrapers.fb_posts_background import pause_marketplace_search
        pause_marketplace_search(cfg)
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


def run_wanted_sweep(cfg: dict, cycle_start: float | None = None) -> list[dict]:
    """One cycle (every 30 minutes): every Wanted term that's due on the
    fast sites (and eBay every 2 hours), then the few terms searched longest
    ago on Facebook, then a couple of misspelled versions of your terms."""
    import json
    from pathlib import Path
    from scrapers import mic_focus
    from scrapers.wanted import terms as wanted_terms
    focus = mic_focus.enabled(cfg)
    # Mic Focus on (Oct 22): only the mics, fast sites every cycle (15 min),
    # eBay every 30 min; the Telex List is kept but not searched.
    # Mic Focus off (Nov 1): the whole Telex List AND the mic terms, one shared
    # rotation, every term at the same rate (least recently searched first).
    mic_terms = mic_focus.sweep_terms(cfg)
    ts = mic_terms if focus else list(dict.fromkeys(wanted_terms(cfg) + mic_terms))
    fast_every, ebay_every = (15, 45) if focus else (FAST_EVERY_MIN, EBAY_EVERY_MIN)
    if not ts:
        return []
    try:
        from scrapers.market import refresh_telex_term_values
        refresh_telex_term_values(ts)
    except Exception:
        logger.exception("Telex term price lookup failed")
    wcfg = cfg.get("wanted") or {}
    fb_per_cycle = int(wcfg.get("fb_terms_per_cycle", FB_TERMS_PER_CYCLE))
    budget = FAST_BUDGET_SECONDS
    if cycle_start is not None:
        # The whole cycle (hourly refresh + this sweep + the Facebook searches) has to finish inside
        # the 15-minute interval, or the next one is skipped. What the refresh already used comes
        # off this sweep's time; terms not reached go first next cycle (least recently searched).
        from scrapers.fb_posts_background import marketplace_quiet_hours as _quiet
        fb_reserve = 0 if _quiet(cfg) else fb_per_cycle * FB_SECONDS_PER_TERM
        budget = int(min(FAST_BUDGET_SECONDS, max(0, CYCLE_TARGET_SECONDS - (time.time() - cycle_start) - fb_reserve)))
    st = sweep_state()
    now = datetime.now(timezone.utc)

    def due(term, site, minutes):
        last = (st.get((term, site)) or {}).get("searched_at")
        if not last:
            return True
        # Cycles start every 30 minutes but a term is searched partway
        # through one, so allow 15 minutes of slack — otherwise every other
        # cycle finds it "not quite due" and it's really searched hourly.
        return (now - datetime.fromisoformat(last)).total_seconds() >= (minutes - 15) * 60

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
    # A few terms at once (Nov 1): one at a time, ~70 mic terms at ~10 s each
    # ran past the 15-minute cycle, so every other cycle was skipped. Each
    # term's sites already run in parallel; Facebook stays one at a time below.
    def run_due(term):
        if time.time() - start > budget:
            return None
        sites = {s for s, m in (("fast", fast_every), ("ebay", ebay_every)) if due(term, s, m)}
        return run(term, sites, "+".join(sorted(sites))) if sites else None

    from concurrent.futures import ThreadPoolExecutor
    order = sorted(ts, key=lambda t: (st.get((t, "fast")) or {}).get("searched_at") or "")
    with ThreadPoolExecutor(max_workers=int(wcfg.get("terms_at_once", TERMS_AT_ONCE)),
                            thread_name_prefix="sweep") as pool:
        out.extend(pool.map(run_due, order))
    if time.time() - start > budget:
        logger.info("Wanted sweep: time's up for this cycle; the rest go first next time")
    from scrapers.fb_posts_background import marketplace_quiet_hours
    if marketplace_quiet_hours(cfg):
        fb_per_cycle = 0  # overnight: Facebook gets a rest (see marketplace_quiet_hours)
    # Cable / connector part numbers ("Neutrik NC3MXX", "Hosa DTP-805")
    # almost never turn up on Facebook (most had no Facebook match in 30
    # days), so they're searched there every 3 days and their turns go to
    # mics and outboard gear — same number of Facebook searches, mics come
    # round sooner. Plain "XLR cable" / "DB25 snake" stay in the rotation.
    fb_due = [t for t in ts if not _cable_part_number(t) or due(t, "fb", FB_CABLE_EVERY_MIN)]
    if focus:
        fb_due = mic_focus.fb_terms(cfg)  # one broad Facebook search per mic brand
    else:  # the Telex List plus one broad search per mic brand, all in the same rotation
        fb_due = list(dict.fromkeys([t for t in ts if t not in mic_terms and (not _cable_part_number(t) or due(t, "fb", FB_CABLE_EVERY_MIN))]
                                    + mic_focus.fb_terms(cfg)))
    for term in sorted(fb_due, key=lambda t: (st.get((t, "fb")) or {}).get("searched_at") or "")[:max(0, fb_per_cycle)]:
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
    if "," in term:  # a Telex entry holding several terms: any of them
        parts = [term_matcher(p.strip()) for p in term.split(",") if p.strip()]
        return lambda t: any(m(t) for m in parts)
    # "-word" leaves those out: "patchbay -behringer" = any patchbay but Behringer.
    words = term.split()
    neg = [w[1:].lower() for w in words if w.startswith("-") and len(w) > 1]
    if neg:
        base = _term_matcher(" ".join(w for w in words if not w.startswith("-")))
        pats = [re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])") for n in neg]
        return lambda t: base(t) and not any(p.search(t) for p in pats)
    return _term_matcher(term)


def _term_matcher(term: str):
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
    # "<name> audio" is a brand ("Link Audio", "Coil Audio", "Purple Audio"):
    # those two words must be together, or "PS Audio Digital Link" and
    # "Moving Coil phono cartridge … audio" match.
    low = term.lower().split()
    brand = None
    if "audio" in low[1:]:
        i = low.index("audio", 1)
        if low[i - 1].isalpha():
            brand = re.compile(r"(?<![a-z0-9])" + re.escape(low[i - 1]) + r"[\s-]*audio\b")
    return lambda t: (plain is None or plain in t) and (brand is None or bool(brand.search(t))) and (
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
