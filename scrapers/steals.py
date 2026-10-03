"""
Steals: listings priced far under what the gear really sells for used
(60%+ off by default) — across everything Gear Scout has collected for your
search terms and Telex List, still for sale.

A discount that big is usually a mistake (wrong comp, a part, a pedal
version), so the filters here are strict: no parts, add-ons, repair jobs,
pedals/plugins, auctions or placeholder prices, and the comp has to be a
real one (sold prices, Reverb/eBay used, B-stock, your local history) — not
an estimate from "similar" listings.
"""
import logging
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MIN_PCT = 60
MIN_PRICE = 20.0   # under this it's a placeholder ("$1 — make an offer") or a part
MIN_REF = 100.0    # 60% off a $60 item isn't a steal worth a page
MAX_PCT = 92       # beyond this it's almost always the wrong comp

# Parts and pieces that the general parts check lets through but that pull
# in the whole unit's price ("Studer A80 headblock", "TLM67 internal
# electronics", "Neumann case w/ TLM 103 foam").
_PARTS = re.compile(
    r"\b(?:head\s?(?:set\s)?block|headstack|assembly|electronics|internals?|pcb|circuit board|boards?|"
    r"option card|expansion card|card for|case(?: only)?\s+(?:w/|with|for)|foam|insert|faceplate|front panel|"
    r"capsule only|socket|connection base|connectors?|pickups?|humbuckers?|power suppl(?:y|ies) only|psu only|cable only|shock ?mount only|manual|"
    r"(?:stand|mount|clip|cable|case|bag|cover|adapter|holder|cradle|grille)s?\s+for)\b", re.I)
# Clones / "style" copies get compared with the real thing far too easily.
_CLONE = re.compile(r"\b(?:clone|replica|copy|style|inspired|tribute|diy|kit|type)\b", re.I)


def _terms(cfg: dict) -> tuple:
    from scrapers.learning import keywords_with_learned
    telex = [str(t).strip() for t in (cfg.get("telex_list") or []) if str(t).strip()]
    return tuple(dict.fromkeys(list(keywords_with_learned(cfg)) + telex))


def find(cfg: dict, since: Optional[str] = None, min_pct: int = MIN_PCT) -> dict:
    """{'trusted': [...], 'rough': [...]} — rows from the seen table with a
    'steal' dict {pct, ref, label, est, saved}, biggest dollar savings first.
    `rough` holds the ones whose comp is only an estimate (double-check)."""
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import (exclude_match, is_accessory_only, is_bundle, is_not_audio, is_partial, is_relevant,
                                 item_form, needs_repair, parse_price, wants_pedals)
    from scrapers.market import load_market
    from scrapers.store import _conn
    sql = ("SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0"
           " AND COALESCE(sold, 0) = 0 AND COALESCE(pending, 0) = 0 AND price IS NOT NULL"
           " AND url NOT LIKE '%search/posts%'")
    args: list = []
    if since:
        sql += " AND first_seen >= ?"
        args.append(since)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY first_seen DESC", args)]
    terms = _terms(cfg)
    exclude_words = cfg.get("exclude_words") or []
    pedals_ok = wants_pedals(cfg)
    market, index, similar = load_market(), price_index(), similar_index()
    from scrapers.comps import similar_price
    from scrapers.enrich import model_key
    trusted, rough, seen_urls, seen_ads = [], [], set(), set()
    for r in rows:
        title = r.get("title") or ""
        value = parse_price(r.get("price"))
        if not value or value < MIN_PRICE or r["url"] in seen_urls:
            continue
        if (r.get("description") or "").startswith("Auction"):
            continue  # a current bid isn't a price
        if (exclude_match(title, exclude_words) or is_not_audio(title) or is_accessory_only(title)
                or is_partial(title) or is_bundle(title) or _PARTS.search(title) or needs_repair(title, r.get("description"))):
            continue
        form = item_form(title)
        if form == "plugin" or (form == "pedal" and not pedals_ok):
            continue
        if not is_relevant(title, r.get("price"), terms):
            continue
        c = evaluate(title, r.get("price"), r.get("description"), r["url"], market, index, similar)
        if not c or c["ref"] < MIN_REF or c["pct"] < min_pct:
            continue
        # Second opinion: what other listings of the same gear ask. A $235
        # Berlant ribbon isn't 89% off when Berlants go for ~$250 — the comp
        # was the vintage RCA it imitates.
        peer = similar_price(similar, title, r["url"])
        mkey = model_key(title)
        if not peer and mkey and mkey in index and c["label"] != "Gear Scout history":
            peer = index[mkey]
        # Judged against used prices: the lower of the comp (B-stock is a
        # near-new price) and what other listings of the same gear ask.
        unit = c["value"]  # per piece when an ad sells several
        ref = min(c["ref"], peer) if peer else c["ref"]
        pct = round((1 - unit / ref) * 100)
        if ref < MIN_REF or not (min_pct <= pct <= MAX_PCT):
            continue
        ad = (title.lower().strip(), round(value))
        if ad in seen_ads:
            continue  # the same ad posted twice / on two sites
        seen_urls.add(r["url"])
        seen_ads.add(ad)
        r["steal"] = {"pct": pct, "ref": ref, "comp": c["ref"], "label": c["label"], "est": c["est"],
                      "saved": ref - unit, "peer": peer, "unit": unit,
                      "clone": bool(_CLONE.search(title))}
        # Estimated comps, clones and anything with no second opinion go in
        # the "double-check" list.
        (rough if c["est"] or r["steal"]["clone"] or not peer else trusted).append(r)
    # Scam pattern: several separate ads for the same model at the same
    # impossible price, posted within a couple of days ("Genuine Neumann TLM
    # 103" at ~$100, four times). Those are left out; anything else 80%+ off
    # goes to "double-check".
    from collections import defaultdict
    groups = defaultdict(list)
    for r in trusted + rough:
        k = model_key(r["title"]) or r["title"].lower()[:40]
        groups[(k, round(math.log(max(r["steal"]["unit"] or 1, 1)) / 0.15))].append(r)
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    scam = {id(r) for g in groups.values()
            if len({x["url"] for x in g}) >= 2 and min(x["steal"]["pct"] for x in g) >= 70
            and all((x.get("first_seen") or "") >= recent for x in g) for r in g}
    if scam:
        logger.info("Steals: left out %d likely scam listings", len(scam))
    trusted = [r for r in trusted if id(r) not in scam]
    rough = [r for r in rough if id(r) not in scam]
    too_good = [r for r in trusted if r["steal"]["pct"] >= 80]
    trusted = [r for r in trusted if r["steal"]["pct"] < 80]
    for r in too_good:
        r["steal"]["too_good"] = True
    rough += too_good
    key = lambda r: -r["steal"]["saved"]
    return {"trusted": sorted(trusted, key=key), "rough": sorted(rough, key=key)}


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS steal_alerted (url TEXT PRIMARY KEY, at TEXT)")


def alert_new(cfg: dict) -> int:
    """Push + email for new steals (found in the last 3 hours, trusted comp
    only), once per listing. Runs hourly in the scraper."""
    from scrapers.lowest import notify_new_lowest
    from scrapers.store import _conn
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    found = find(cfg, since=since)["trusted"]
    if not found:
        return 0
    with _conn() as conn:
        _ensure(conn)
        done = {u for (u,) in conn.execute("SELECT url FROM steal_alerted")}
    sent = 0
    for r in found:
        if r["url"] in done:
            continue
        s = r["steal"]
        alert = {"title": r["title"], "price": r["price"], "url": r["url"], "image_url": r.get("image_url"),
                 "source_name": r.get("source_name") or "", "location": (r.get("location") or "").split(" @")[0]}
        try:
            notify_new_lowest(cfg, f"Steal — {s['pct']}% under {s['label']} ~${s['ref']:,.0f}", alert, None,
                              from_telex=True)
            sent += 1
        except Exception:
            logger.exception("Steal alert failed")
        with _conn() as conn:
            _ensure(conn)
            conn.execute("INSERT OR IGNORE INTO steal_alerted VALUES (?, ?)",
                         (r["url"], datetime.now(timezone.utc).isoformat()))
            conn.commit()
    if sent:
        logger.info("Steal alerts sent: %d", sent)
    return sent
