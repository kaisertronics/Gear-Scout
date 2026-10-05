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


def _unit(r: dict, unit: float, n: int, c) -> tuple[float, int]:
    """Price per piece for a several-piece ad: "Selling 3 Apollo x8p —
    C$2,800" is almost certainly C$2,800 each (a mint x8p goes for ~$1,800),
    not three for C$2,800. Uses the same rule as everywhere else: a price
    near ONE unit's usual price is per piece."""
    if n < 2 or not c:
        return unit, n
    from scrapers.enrich import parse_price, price_basis
    value = parse_price(r["price"]) or unit * n
    basis, qty, per = price_basis(r["title"], r.get("description"), value, c["ref"])
    return per, (qty if basis.startswith("each") or basis == "set" else n)


def _brief(r: dict, unit: float, n: int) -> dict:
    return {"title": r["title"], "price": r["price"], "unit": unit, "qty": n, "url": r["url"],
            "image_url": r.get("image_url"), "source": (r.get("source_name") or "").split(" — ")[0],
            "location": (r.get("location") or "").split(" @")[0] or None, "age_days": _age_days(r),
            "age_known": bool(r.get("posted_at"))}


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
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            """SELECT title, price, url, source_name, image_url, location, description, posted_at, first_seen
               FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0
               AND COALESCE(pending, 0) = 0 AND price IS NOT NULL""")]
        conn.execute("CREATE TABLE IF NOT EXISTS board_history (term TEXT, day TEXT, lowest REAL, PRIMARY KEY (term, day))")
        history = {}
        for term, day, low in conn.execute("SELECT term, day, lowest FROM board_history"):
            history.setdefault(term, {})[day] = low
    texts = [fix_brand_spelling((r["title"] or "").lower()) for r in rows]
    market, index, similar = load_market(), price_index(), similar_index()
    today = datetime.now(timezone.utc).date()
    board = []
    for term, kind in all_terms:
        # Specific models match brand + exact model number ("v72" isn't a
        # guitar pick with "V72" somewhere in the title).
        match = model_matcher(term) if _is_specific(term) and re.search(r"\d", term) else term_matcher(term)
        form_wanted = item_form(term)
        cands = []
        for r, text in zip(rows, texts):
            if not match(text):
                continue
            title = r["title"] or ""
            form = item_form(title)
            if ((form in ("pedal", "plugin") and form != form_wanted) or is_partial(title) or is_accessory_only(title)
                    or title_says_sold(title)
                    or is_not_audio(title) or exclude_match(title, exclude)
                    or (r.get("description") or "").startswith("Auction")):
                continue
            value = parse_price(r["price"])
            if not value or value < 20:
                continue
            n = quantity(title, r.get("description"))
            cands.append((value / max(n, 1), n, r))
        cands.sort(key=lambda c: c[0])
        # The cheapest believable one: placeholders and mismatches (under a
        # fifth of the item's own comp) are skipped.
        cheapest = comp = None
        checked = []
        for unit, n, r in cands[:12]:
            c = evaluate(r["title"], r["price"], r.get("description"), r["url"], market, index, similar)
            unit, n = _unit(r, unit, n, c)
            if c and unit < c["ref"] * 0.2:
                continue
            checked.append((unit, n, r, c))
        if checked:  # re-sorted after working out per-piece prices
            unit, n, r, c = min(checked, key=lambda x: x[0])
            cheapest, comp = _brief(r, unit, n), c
        fresh = [(u, n, r) for u, n, r in cands if (_age_days(r) or 0) <= FRESH_DAYS]
        # Best deal this week: the biggest real discount among ads posted in
        # the last week (a solid comp, 10-80% under — beyond 80% is almost
        # always a mismatch or a scam).
        best = None
        specific = _is_specific(term)
        for unit, n, r in fresh[:400]:
            # Brand / category terms ("tube mic", "stam"): only gear near the
            # owner's budget ($500–$2,000), not $30 mics.
            if not specific and unit < MIN_GENERIC:
                continue
            c = evaluate(r["title"], r["price"], r.get("description"), r["url"], market, index, similar)
            if not c or c["est"] or c["ref"] < 100:
                continue
            unit, n = _unit(r, unit, n, c)
            pct = round((1 - unit / c["ref"]) * 100)
            if 10 <= pct <= 80 and (best is None or pct > best[0]):
                best = (pct, _brief(r, unit, n), c)
        best_deal = ({**best[1], "pct": best[0], "ref": best[2]["ref"], "label": best[2]["label"]} if best else None)
        units = [u for u, _, _ in cands]
        # "Cheapest anywhere" only means something for a specific model
        # ("Avalon 737", "Distressor"), not a brand or a category ("stam",
        # "tube mic") — there it's just the cheapest junk.
        row = {"term": term, "kind": kind, "count": len(cands), "fresh_count": len(fresh), "specific": specific,
               "best": best_deal,
               "median": statistics.median(units) if units else None,
               "cheapest": cheapest if specific else None,
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


def load() -> dict:
    try:
        return json.loads(BOARD_PATH.read_text())
    except Exception:
        return {"built": None, "rows": []}
