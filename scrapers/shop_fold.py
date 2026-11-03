"""Shop-stock fold: Reverb shops' brand-new / B-stock copies of Lauten and Warm
Audio gear fill the Dashboard (and the owner's hide list). They fold into one
collapsed line per model (count + lowest price) unless a copy is FOLD_UNLESS_PCT
or more under the model's used value. With no known value a shop copy stays
folded. Starred ads and non-shop ads are never folded.

Pure: nothing is read or written except through the helper functions passed in
(defaults: scrapers.market_value / scrapers.enrich)."""
from __future__ import annotations

import re
from typing import Callable, Optional

FOLD_UNLESS_PCT = 25
_BRANDS = re.compile(r"\b(?:lauten(?: audio)?|warm audio)\b", re.I)
_SHOP_CONDITIONS = {"brand new", "b-stock", "b stock", "bstock"}


def condition_of(listing: dict) -> str:
    """Reverb stores the condition as the start of the description: 'B-Stock · ...'."""
    desc = listing.get("description") or ""
    head = desc.split(" · ", 1)[0] if " · " in desc else ""
    return head.strip().lower()


def is_shop_listing(listing: dict) -> bool:
    """A Reverb ad whose condition is Brand New or B-Stock (private sellers list used)."""
    return str(listing.get("source_name") or "").lower().startswith("reverb") \
        and condition_of(listing) in _SHOP_CONDITIONS


def _defaults():
    from scrapers import market_value
    from scrapers.enrich import model_key, parse_price
    return (lambda t, d=None: market_value.value(t, d)[0]), model_key, parse_price


def fold_shop_stock(listings: list, value_fn: Optional[Callable] = None, key_fn: Optional[Callable] = None,
                    price_fn: Optional[Callable] = None) -> tuple[list, list]:
    """(kept, groups). groups: [{'key', 'model', 'count', 'low', 'items'}] sorted
    by count; items cheapest first. 'low' is the lowest price as a float (or None)."""
    if value_fn is None or key_fn is None or price_fn is None:
        dv, dk, dp = _defaults()
        value_fn, key_fn, price_fn = value_fn or dv, key_fn or dk, price_fn or dp
    kept, by_key = [], {}
    for l in listings:
        if l.get("favorite") or not _BRANDS.search(l.get("title") or "") or not is_shop_listing(l):
            kept.append(l)
            continue
        price = price_fn(l.get("price"))
        used = value_fn(l.get("title"), l.get("description"))
        if used and price and (1 - price / used) * 100 >= FOLD_UNLESS_PCT:
            kept.append(l)
            continue
        key = (key_fn(l.get("title")) or "").split("|")[0] or (l.get("title") or "").lower()
        g = by_key.setdefault(key, {"key": key, "model": l.get("title") or key, "items": []})
        g["items"].append((price, l))
    groups = []
    for g in by_key.values():
        g["items"].sort(key=lambda p: (p[0] is None, p[0] or 0))
        prices = [p for p, _ in g["items"] if p]
        g["low"] = min(prices) if prices else None
        g["model"] = g["items"][0][1].get("title") or g["model"]
        g["items"] = [l for _, l in g["items"]]
        g["count"] = len(g["items"])
        groups.append(g)
    groups.sort(key=lambda g: (-g["count"], g["model"]))
    return kept, groups
