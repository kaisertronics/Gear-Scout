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


def run_sweep(cfg: dict) -> list[dict]:
    try:
        from scrapers.market import refresh_telex_term_values
        refresh_telex_term_values(terms(cfg))
    except Exception:
        logger.exception("Telex term price lookup failed")
    return _run_sweep(cfg)


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
