"""
Comps: what a listing should be compared against, and whether it's worth
showing (at least 10% under). Shared by the dashboard, the email digests
and the Facebook roundup.

Comp chain, most trustworthy first:
  1. what the model sold for (listings Gear Scout watched sell)
  2. Reverb / eBay used price for the model
  3. Gear Scout's own price history for the model (all sites, incl. sold)
  4. Reverb / eBay price of a specific Telex term (same brand)
  5. similar past listings sharing the item's key words (estimate)
  6. a rougher Reverb / eBay lookup (estimate)
A model with a B-stock value is judged against that instead.
"""
import re
import statistics
import time
from typing import Optional

from scrapers.store import _conn, all_priced_rows

WORTH_RATIO = 0.9   # 10%+ under
FLOOR_RATIO = 0.2   # under a fifth of the comp = a mismatch or placeholder

_index_cache: dict = {"at": 0.0, "index": None}


def price_index(force: bool = False) -> dict:
    """Typical prices from Gear Scout's own history, refreshed every 15
    minutes — in the background: a page never waits for the rebuild (it
    reads every priced listing), it uses the last one."""
    from scrapers.enrich import build_price_index
    if force or _index_cache["index"] is None:
        _index_cache.update(index=build_price_index(all_priced_rows()), at=time.time())
    elif time.time() - _index_cache["at"] > 900 and not _index_cache.get("running"):
        import threading

        def rebuild():
            _index_cache["running"] = True
            try:
                _index_cache.update(index=build_price_index(all_priced_rows()), at=time.time())
            finally:
                _index_cache["running"] = False
        threading.Thread(target=rebuild, name="price-index", daemon=True).start()
    return _index_cache["index"]


def believable(value: float, ref: float, est: bool) -> bool:
    """A rough estimate ("similar listings", a loose lookup) mixes in other
    models, so it may say "55-80% off" when the item is just its normal price
    (an SSL 1 vs. pricier SSL gear, a Lynx Aurora 8 vs. newer Auroras). It
    can back a modest discount, not a huge one."""
    return not (est and value < ref * 0.6)


def best_comp(r, market, index, similar, term_comp, term_brand):
    """(comp price, where it came from, is it only an estimate) for a
    listing — the most trustworthy source first:
      1. what the model actually sold for (listings Gear Scout watched sell)
      2. Reverb / eBay used price for the model
      3. Gear Scout's own price history for the model (all sites, incl. sold)
      4. Reverb / eBay price of the Telex term (specific products, same brand)
      5. similar past listings sharing the item's key words (estimate)"""
    from scrapers.enrich import canonical_brand, is_partial, model_key, value_key
    title = r.get("title") or ""
    mkey, key = model_key(title), value_key(title)
    unreliable = market.rough | market.mixed | market.distrusted
    if mkey and mkey in market.sold and mkey not in market.distrusted:
        return market.sold[mkey], "sold", False
    if key and key[:2] not in ("t:", "p:") and key in market and key not in unreliable:
        return market[key], f"{market.sources.get(key) or 'Reverb'} used", False
    if mkey and mkey in index and not is_partial(title) and mkey not in market.distrusted:
        return index[mkey], "Gear Scout history", False
    if term_comp and canonical_brand(title) == term_brand:
        return term_comp[0], term_comp[1], False
    est = similar_price(similar, title, r.get("url"))
    if est:
        return est, "similar listings", True
    # A rougher Reverb/eBay lookup (few listings, or a looser search) is still
    # better than nothing — as an estimate. Disputed or mixed-up ones aren't.
    if key and key in market and key not in market.distrusted and key not in market.mixed:
        return market[key], f"{market.sources.get(key) or 'Reverb'}", True
    return None


_similar_cache: dict = {"at": 0.0, "data": None}
_SIM_SKIP = {"vintage", "used", "new", "mint", "excellent", "great", "good", "condition", "with", "and",
             "for", "the", "sale", "selling", "black", "silver", "white", "pair", "works", "working",
             "tested", "audio", "pro", "professional", "studio", "original", "rare", "box", "case",
             "free", "shipping", "local", "pickup", "obo", "price", "each", "only", "like", "series"}


def sim_words(title: str) -> list[str]:
    return [w for w in dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9-]*[a-z0-9]", (title or "").lower()))
            if len(w) >= 3 and w not in _SIM_SKIP and not re.fullmatch(r"(?:19|20)\d\ds?|\d{1,2}|\d+(?:st|nd|rd|th)", w)]


def similar_index():
    """Word -> listings index over every priced listing Gear Scout has ever
    stored (all sites, including ones that sold), for "similar listings"
    comps. Rebuilt every 10 minutes."""
    if _similar_cache["data"] is not None and time.time() - _similar_cache["at"] < 600:
        return _similar_cache["data"]
    from scrapers.enrich import is_bundle, is_not_audio, is_partial, parse_price, quantity
    from scrapers.store import _conn
    prices, urls, words = [], [], {}
    with _conn() as conn:
        rows = conn.execute("SELECT title, price, url FROM seen WHERE price IS NOT NULL AND duplicate = 0").fetchall()
    for title, price, url in rows:
        value = parse_price(price)
        if (not value or value < 20 or is_partial(title) or is_bundle(title) or is_not_audio(title)
                or quantity(title) > 1):
            continue
        i = len(prices)
        prices.append(value)
        urls.append(url)
        for w in sim_words(title):
            words.setdefault(w, set()).add(i)
    data = {"prices": prices, "urls": urls, "words": words}
    _similar_cache.update(at=time.time(), data=data)
    return data


def similar_price(sim, title: str, url: Optional[str]) -> Optional[float]:
    """Median price of stored listings sharing this one's most specific words
    (rarest first): 3 words, else 2, needing 4+ listings with consistent
    prices."""
    import statistics
    from scrapers.enrich import strip_clone_reference
    words = sim_words(strip_clone_reference(title))
    ws = [w for w in words if w in sim["words"] and len(sim["words"][w]) >= 2]
    # A model number ("WA-19B") with too few other listings to go on: the
    # remaining words ("warm", "style", "dynamic") would mix in other models.
    if any(re.search(r"\d", w) and re.search(r"[a-z]", w) and w not in ws for w in words):
        return None
    ws.sort(key=lambda w: len(sim["words"][w]))
    for n in (3, 2):
        if len(ws) < n:
            continue
        ids = set.intersection(*(sim["words"][w] for w in ws[:n]))
        vals = sorted(sim["prices"][i] for i in ids if sim["urls"][i] != url)
        if len(vals) >= 4 and vals[(len(vals) * 3) // 4] <= 3 * vals[len(vals) // 4]:
            return statistics.median(vals)
    # Just the model number ("MK-219", "CM7") when it's the only telling word.
    for w in ws[:2]:
        if re.search(r"\d", w) and re.search(r"[a-z]", w):
            vals = sorted(sim["prices"][i] for i in sim["words"][w] if sim["urls"][i] != url)
            if len(vals) >= 3 and vals[(len(vals) * 3) // 4] <= 3 * vals[len(vals) // 4]:
                return statistics.median(vals)
    return None




def evaluate(title: Optional[str], price: Optional[str], description: Optional[str] = None,
             url: Optional[str] = None, market=None, index=None, similar=None) -> Optional[dict]:
    """{'ref', 'label', 'est', 'pct', 'worth', 'value' (per piece)} for a listing, or None when
    there's nothing to compare it with."""
    from scrapers.enrich import parse_price, price_context
    from scrapers.market import load_market
    market = market if market is not None else load_market()
    index = index if index is not None else price_index()
    similar = similar if similar is not None else similar_index()
    ctx = price_context(title, price, index, market, description=description)
    value = ctx.get("unit_value") or parse_price(price)
    if not value:
        return None
    if ctx.get("bstock") and not ctx.get("rough"):
        ref, label, est = parse_price(ctx["bstock"]), "B-stock", False
    else:
        comp = best_comp({"title": title, "url": url}, market, index, similar, None, None)
        if not comp:
            return None
        ref, label, est = comp
    if not believable(value, ref, est):
        return None
    return {"ref": ref, "label": label, "est": est, "pct": round((1 - value / ref) * 100), "value": value,
            "worth": ref * FLOOR_RATIO <= value <= ref * WORTH_RATIO}
