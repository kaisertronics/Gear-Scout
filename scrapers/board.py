"""
Price Board: one row per Telex term (and per tracked Lowest-Price item),
built hourly from what Gear Scout has already collected — no extra searching.

Each row: the cheapest one for sale right now (price per piece; no parts,
accessories, pedals/plugins unless asked, auctions or placeholder prices),
what the gear usually sells for used, whether that cheapest one is actually a
deal, how many are for sale (and how many were posted this week), the
cheapest posted this week, and the trend of the lowest price over time.

Saved to /data/price_board.json for the dashboard.
"""
import json
import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
BOARD_PATH = Path("/data/price_board.json")
FRESH_DAYS = 7
# "Cheapest anywhere": any ad still for sale, however long ago it was posted.
VERIFY_HOURS = 3            # re-check the cheapest is still up if not confirmed this recently
MIN_GENERIC = 300  # owner's budget is $500–$2,000; a little room below


def _age_days(r: dict):
    for field in ("posted_at", "first_seen"):
        try:
            d = datetime.fromisoformat(r.get(field) or "")
        except (TypeError, ValueError):
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)
    return None


def _is_specific(term: str) -> bool:
    """A particular model ("WA-412", "Apollo X8P", "Distressor", "Cascade
    Fathead") rather than a brand or a kind of gear ("stam", "tube mic")."""
    import re
    from scrapers.enrich import canonical_brand, is_generic_term
    low = term.lower()
    if re.search(r"[a-z]*\d", low):
        return True
    brand = canonical_brand(term) or ""
    words = [w for w in re.findall(r"[a-z]+", low) if w not in brand.replace("-", " ").split()
             and not is_generic_term(w) and w not in ("audio", "labs", "mic", "microphone", "clone", "pro")]
    # A known brand plus a model/line word ("Cascade Fathead", "Distressor"),
    # or two telling words without a known brand ("Overstayer Stereo Field").
    # One unknown word ("phoenix audio", "daking") is a brand, not a model.
    return bool(words) and (bool(brand) or len(words) >= 2)


# Add-ons sold for a unit, not the unit ("PCM 80 Pitch FX ROM", "algorithm card").
_ADDON_WORDS = re.compile(r"\b(?:roms?|eproms?|cards?|cartridges?|chips?|firmware|upgrade kit|algorithms?|"
                          r"grill(?:e|es|s)?|replacement|knobs?|faceplates?|(?:oled |lcd )?displays?|lcd|oled|screens?|"
                          r"manuals?|service manual|schematics?|remote only|power supply only|psu only)\b", re.I)


_INCLUDED = re.compile(r"\s(?:w/|with|incl\.?|includes?|including|plus|\+)\s?", re.I)


def _is_addon(title: str) -> bool:
    """A part or accessory sold on its own. Words after "w/", "with",
    "includes", "+" describe what comes WITH the item ("VR2 w/ gold XLR
    connector", "… + shock mount"), so they don't count."""
    from scrapers.steals import _PARTS
    t = title or ""
    m = _INCLUDED.search(t)
    cut = m.start() if m else len(t)
    return any(hit.start() < cut for rx in (_PARTS, _ADDON_WORDS) for hit in rx.finditer(t))


def _brief(r: dict, unit: float, n: int) -> dict:
    return {"title": r["title"], "price": r["price"], "unit": unit, "qty": n, "url": r["url"],
            "image_url": r.get("image_url"), "source": (r.get("source_name") or "").split(" — ")[0],
            "location": (r.get("location") or "").split(" @")[0] or None, "age_days": _age_days(r),
            "age_known": bool(r.get("posted_at")), "shipping": r.get("shipping"),
            "total": unit + ((r.get("shipping") or 0) / max(n or 1, 1))}


def build(cfg: dict, only: list[str] | None = None) -> list[dict]:
    """Rebuilds the whole board (hourly), or with `only`: just those terms,
    merged into the saved board (seconds, when you add a term), dropping
    terms no longer on your lists."""
    from scrapers.base import fix_brand_spelling
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import (exclude_match, is_accessory_only, is_not_audio, is_partial, item_form,
                                 parse_price, quantity)
    from scrapers.lowest import tracked_queries
    from scrapers.market import load_market
    from scrapers.store import _conn
    from scrapers.enrich import title_says_sold
    from scrapers.telex import model_matcher, term_matcher, terms

    telex = terms(cfg)
    tracked = [q for q in tracked_queries(cfg) if q.lower() not in {t.lower() for t in telex}]
    all_terms = [(t, "telex") for t in telex] + [(q, "tracked") for q in tracked]
    wanted = [t for t, _ in all_terms]
    if only is not None:
        all_terms = [(t, k) for t, k in all_terms if t in only]
    exclude = cfg.get("exclude_words") or []
    # The same listings the Feed shows: still for sale, shown by a site in
    # the last 45 days, the same ad stored twice counted once.
    from scrapers.wanted import recent_rows
    rows, seen_ads = [], set()
    for r in recent_rows():
        ad = ((r.get("title") or "").lower().strip(), r.get("price"))
        if r.get("price") and r["url"].startswith("http") and ad not in seen_ads:
            seen_ads.add(ad)
            rows.append(r)
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS board_history (term TEXT, day TEXT, lowest REAL, PRIMARY KEY (term, day))")
        history = {}
        for term, day, low in conn.execute("SELECT term, day, lowest FROM board_history"):
            history.setdefault(term, {})[day] = low
    market, index, similar = load_market(), price_index(), similar_index()
    # Same rules as the Feed: which listings count for a term (scrapers/wanted.py)
    # and what counts as a deal (scrapers/steals.py, as on the Deals page and
    # in the steal alerts) — so the board's numbers match everywhere else.
    from scrapers import wanted as wanted_list
    from scrapers.steals import find as find_deals
    week_ago = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).isoformat()
    deals = {d["url"]: d["steal"] for d in find_deals(cfg, since=week_ago, min_pct=10)["trusted"]}
    today = datetime.now(timezone.utc).date()
    board = []
    for term, kind in all_terms:
        # Specific models match brand + exact model number ("v72" isn't a
        # guitar pick with "V72" somewhere in the title).
        one = [(term, term_matcher(term), item_form(term), _is_specific(term))]
        cands = []
        for r in rows:
            title = r["title"] or ""
            if not wanted_list.match(title, r["price"], one, r.get("source_name")):
                continue
            if (title_says_sold(title)
                    or is_not_audio(title) or exclude_match(title, exclude)
                    or (r.get("description") or "").startswith("Auction")):
                continue
            value = parse_price(r["price"])
            if not value or value < 20:
                continue
            # Price per piece, by the same rule the Feed uses ("3 available —
            # $2,800" near one unit's usual price means each).
            from scrapers.enrich import price_context
            ctx = price_context(title, r["price"], index, market, description=r.get("description"))
            n = ctx.get("qty") or quantity(title, r.get("description"))
            cands.append((ctx.get("unit_value") or value / max(n, 1), n, r))
        cands.sort(key=lambda c: c[0])
        # "Cheapest anywhere" is where junk floats up (a record named
        # "Distressor", a pedal): ignore anything under a quarter of what this
        # term's listings typically ask.
        if len(cands) >= 5:
            typical = statistics.median(u for u, _, _ in cands)
            cands = [c for c in cands if c[0] >= typical * 0.25]
        fresh = [(u, n, r) for u, n, r in cands if (_age_days(r) or 0) <= FRESH_DAYS]
        # Best deal this week: the biggest real discount among ads posted in
        # the last week (a solid comp, 10-80% under — beyond 80% is almost
        # always a mismatch or a scam).
        best = None
        specific = _is_specific(term)
        for unit, n, r in fresh:
            # Brand / category terms ("tube mic", "stam"): only gear near the
            # owner's budget ($500–$2,000), not $30 mics.
            if not specific and unit < MIN_GENERIC:
                continue
            s = deals.get(r["url"])
            if not s or s["est"]:
                continue
            if best is None or s["pct"] > best[0]:
                best = (s["pct"], _brief(r, s["unit"], n), {"ref": s["ref"], "label": s["label"]})
        best_deal = ({**best[1], "pct": best[0], "ref": best[2]["ref"], "label": best[2]["label"]} if best else None)
        units = [u for u, _, _ in cands]
        # "Cheapest anywhere" only means something for a specific model
        # ("Avalon 737", "Distressor"), not a brand or a category ("stam",
        # "tube mic") — there it's just the cheapest junk.
        # The cheapest working one of each model, cheapest first — the same
        # picks the Feed and the Telex page mark (scrapers/models.py). The
        # first is the term's "cheapest anywhere".
        from scrapers import models as model_picks
        rows_by_url = {}
        unit_rows = []
        for c_unit, c_n, c_r in cands:
            rows_by_url[c_r["url"]] = (c_unit, c_n, c_r)
            unit_rows.append({**c_r, "unit": c_unit, "qty": c_n})
        models = []
        for p in model_picks.picks(term, unit_rows, exclude):
            c_unit, c_n, c_r = rows_by_url[p["url"]]
            models.append({**_brief(c_r, c_unit, c_n), "model": p["model"], "model_count": p["count"]})
        if models:
            first = models[0]
            cheapest = {k: v for k, v in first.items() if k not in ("model", "model_count")}
            comp = evaluate(first["title"], first["price"], None, first["url"], market, index, similar)
        else:
            cheapest = comp = None
        row = {"term": term, "kind": kind, "count": len(cands), "fresh_count": len(fresh), "specific": specific,
               "models": models[:15],
               "best": best_deal,
               "median": statistics.median(units) if units else None,
               "cheapest": cheapest,  # every term, brands too (owner asked)
               "comp": ({"ref": comp["ref"], "label": comp["label"], "est": comp["est"], "pct": comp["pct"]}
                        if comp else None)}
        # Trend: today's lowest vs. a week and a month ago.
        h = history.get(term, {})
        if cheapest and specific:
            h[today.isoformat()] = min(h.get(today.isoformat(), 1e12), cheapest["unit"])
        for label, days in (("week", 7), ("month", 30)):
            past = [v for d, v in h.items() if d <= (today - timedelta(days=days)).isoformat()]
            row[f"was_{label}"] = past[-1] if past else None
        row["history"] = [h[d] for d in sorted(h)[-30:]]
        board.append(row)
    with _conn() as conn:
        conn.executemany("INSERT OR REPLACE INTO board_history VALUES (?, ?, ?)",
                         [(r["term"], today.isoformat(), r["cheapest"]["unit"]) for r in board if r.get("cheapest")])
        conn.commit()
    if only is not None:
        merged = {r["term"]: r for r in load().get("rows") or []}
        merged.update({r["term"]: r for r in board})
        board = [merged[t] for t in wanted if t in merged]
    BOARD_PATH.write_text(json.dumps({"built": datetime.now(timezone.utc).isoformat(), "rows": board}))
    logger.info("Price Board: %d rows", len(board))
    return board


def verify_cheapest(cfg: dict, rounds: int = 3) -> int:
    """Makes sure each term's "cheapest anywhere" is still for sale: any not
    confirmed in the last few hours is checked now (sites directly, Facebook
    in its browser); gone ones are marked sold and those terms rebuilt, so
    the next cheapest takes over. Returns how many were gone."""
    from scrapers.sold_check import _record, check_fb, check_url
    from scrapers.store import _conn
    gone_total = 0
    for _ in range(rounds):
        rows = load().get("rows") or []
        cheapest = {r["cheapest"]["url"]: r["term"] for r in rows if r.get("cheapest") and r["cheapest"].get("url")}
        try:  # and what the Feed / Telex page mark as each model's cheapest
            for u in Path("/data/lowest_picks.txt").read_text().split("\n"):
                if u.startswith("http"):
                    cheapest.setdefault(u, None)
        except OSError:
            pass
        for r in rows:  # and each model's cheapest
            for m in r.get("models") or []:
                if m.get("url"):
                    cheapest.setdefault(m["url"], r["term"])
        if not cheapest:
            return gone_total
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=VERIFY_HOURS)).isoformat()
        urls = list(cheapest)
        with _conn() as conn:
            fresh = {u for (u,) in conn.execute(
                f"SELECT url FROM seen WHERE url IN ({','.join('?' * len(urls))}) AND confirmed_live_at >= ?",
                (*urls, cutoff))}
        todo = [u for u in urls if u not in fresh]
        if not todo:
            return gone_total
        gone = []
        for u in [u for u in todo if "facebook.com" not in u]:
            try:
                g = check_url(u)
            except Exception:
                g = None
            _record(u, g)
            if g:
                gone.append(u)
        for u, g in check_fb([u for u in todo if "facebook.com/marketplace/item" in u]):
            _record(u, g)
            if g:
                gone.append(u)
        if not gone:
            return gone_total
        gone_total += len(gone)
        logger.info("Price Board: %d cheapest listings were gone — picking the next cheapest", len(gone))
        build(cfg, only=[cheapest[u] for u in gone if cheapest.get(u)])
    return gone_total


def load() -> dict:
    try:
        return json.loads(BOARD_PATH.read_text())
    except Exception:
        return {"built": None, "rows": []}
