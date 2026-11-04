"""Studio build planner: "This week's plan" for the Shopping page.

Per shopping-list item still needed it says Buy now / Wait / Watching, using only
data Gear Scout already has (no new queries; shopping_list.build() is itself cached):

  - Buy now : the cheapest live listing (price + shipping, before tax) is BUY_UNDER
              or more under the item's used value, or at/below the owner's own
              price target for it (price_targets.json, matched by the target's words
              appearing in the item name).
  - Wait    : wait_signal says prices are above their usual level for a price-history
              model whose label appears in the item's name/key.
  - Watching: everything else, including every item with missing data.

Budget remaining = sum of the target price (owner's target, else the item's used
value) x pieces still needed, for items not yet bought. The result is cached for
CACHE_TTL seconds in memory.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

BUY_UNDER = 0.25    # a live listing this far under used value is a buy
CACHE_TTL = 300

_cache: dict = {"at": 0.0, "key": None, "out": None}
_lock = threading.Lock()


def _target_for(item: dict, targets: list) -> dict | None:
    """The owner's own price target whose words all appear in this item's name."""
    from scrapers.price_targets import _matches, _words
    for t in targets:
        w = _words(t["text"])
        if w and _matches(w, item["name"]):
            return t
    return None


def _wait_for(item: dict, waits: dict, history: dict) -> dict | None:
    """A 'wait' signal for a price-history model named in the item (never guessed)."""
    from scrapers.price_targets import _words
    text = "".join(_words(item["name"] + " " + item["key"]))
    for mk, sig in (waits or {}).items():
        if sig.get("tone") != "wait":
            continue
        label = "".join(_words((history.get(mk) or {}).get("label") or ""))
        if len(label) >= 3 and label in text:
            return sig
    return None


def _compute(cfg: dict) -> dict:
    from scrapers import price_history, price_targets, shopping_list, wait_signal
    st = shopping_list.load_state()
    targets = price_targets.load()
    try:
        history = price_history.history(cfg)
        waits = wait_signal.signals(history)
    except Exception:
        logger.exception("Build planner: wait signals unavailable")
        history, waits = {}, {}
    rows, budget_left, bought_n = [], 0.0, 0
    for it in shopping_list.build()["items"]:
        got = min(max(int(st["bought"].get(it["key"], 0) or 0), 0), it["qty"])
        left = it["qty"] - got
        if left <= 0:
            bought_n += 1
            continue
        tgt = _target_for(it, targets)
        tprice = float(tgt["max_price"]) if tgt else float(it["used_unit"])
        budget_left += tprice * left
        best = (it.get("matches") or [None])[0]
        status, why, link = "Watching", "No deal yet; still watching.", None
        if best:
            cost = best["unit"] + best["ship"]
            link = best["url"]
            used = it["used_unit"]
            if tgt and cost <= tprice:
                status, why = "Buy now", f"Live at ${cost:,.0f}, at or under your ${tprice:,.0f} target."
            elif used and cost <= used * (1 - BUY_UNDER):
                status = "Buy now"
                why = f"Live at ${cost:,.0f}, {round((1 - cost / used) * 100)}% under used value (~${used:,.0f})."
        if status == "Watching":
            sig = _wait_for(it, waits, history)
            if sig:
                status, why = "Wait", sig["why"]
        rows.append({"key": it["key"], "name": it["name"], "left": left, "status": status, "why": why,
                     "url": link, "target": round(tprice, 2), "own_target": bool(tgt)})
    order = {"Buy now": 0, "Wait": 1, "Watching": 2}
    rows.sort(key=lambda r: order[r["status"]])
    return {"items": rows, "budget_left": round(budget_left, 2), "bought_items": bought_n,
            "counts": {s: sum(1 for r in rows if r["status"] == s) for s in order}}


def plan(cfg: dict) -> dict:
    """The weekly plan; cached ~5 minutes (and recomputed when the bought list changes). Never raises."""
    from scrapers import shopping_list
    key = repr(sorted(shopping_list.load_state()["bought"].items()))
    with _lock:
        if _cache["out"] is not None and _cache["key"] == key and time.time() - _cache["at"] < CACHE_TTL:
            return _cache["out"]
    try:
        out = _compute(cfg)
    except Exception:
        logger.exception("Build planner failed")
        return {"items": [], "budget_left": 0.0, "bought_items": 0, "counts": {}}
    with _lock:
        _cache.update(at=time.time(), key=key, out=out)
    return out
