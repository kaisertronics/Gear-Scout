"""The best set of buys for the cash you have. Pure (no Gear Scout imports).

A budget-limited pick (a 0/1 knapsack): each opportunity costs its cash in
(buy + the full repair bill, so the plan never overspends) and is worth its expected profit trimmed by
confidence, so a shaky $500 isn't preferred over a solid $400. At most
`max_per_model` of the same model, so one bad value can't sink the plan."""
import math
from typing import Optional

STEP = 10  # dollars per budget unit (keeps it fast for big budgets)
MAX_UNITS = 6000


def _worth(o: dict) -> float:
    m = o["m"]
    return max(0.0, m["expected"] * m["confidence"] / 100)


def optimize(opps: list[dict], cash: float, max_per_model: int = 1, max_candidates: int = 200) -> list[dict]:
    """The chosen opportunities (best first). Each item is {"m": metrics, "model": key, ...}."""
    if cash <= 0 or not opps:
        return []
    step = max(STEP, cash / MAX_UNITS)
    cap = int(cash // step)
    pool, per_model = [], {}
    for o in sorted(opps, key=lambda o: -_worth(o)):
        if _worth(o) <= 0 or o["m"]["cash_need"] > cash:
            continue
        key = o.get("model") or o.get("url")
        if per_model.get(key, 0) >= max_per_model:
            continue
        per_model[key] = per_model.get(key, 0) + 1
        pool.append(o)
        if len(pool) >= max_candidates:
            break
    if not pool:
        return []
    costs = [max(1, math.ceil(o["m"]["cash_need"] / step)) for o in pool]
    best = [0.0] * (cap + 1)
    keep = [bytearray(cap + 1) for _ in pool]
    for i, o in enumerate(pool):
        w, c = _worth(o), costs[i]
        if c > cap:
            continue
        row = keep[i]
        for b in range(cap, c - 1, -1):
            v = best[b - c] + w
            if v > best[b]:
                best[b] = v
                row[b] = 1
    chosen, b = [], cap
    for i in range(len(pool) - 1, -1, -1):
        if keep[i][b]:
            chosen.append(pool[i])
            b -= costs[i]
    chosen.sort(key=lambda o: -o["m"]["score"])
    return chosen


def totals(chosen: list[dict], cash: Optional[float] = None) -> dict:
    """Plan-level numbers: what goes in, what comes out, how fast."""
    t = {"n": len(chosen), "cash_in": 0, "buy": 0, "repair": 0, "fees": 0, "ship": 0, "resale": 0,
         "expected": 0, "downside": 0}
    for o in chosen:
        m = o["m"]
        t["cash_in"] += m["cash_need"]
        t["buy"] += m["buy_total"]
        t["repair"] += m["repair_cost"] or 0
        t["fees"] += m["fees"]
        t["ship"] += m["ship_out"]
        t["resale"] += m["resale"]
        t["expected"] += m["expected"]
        t["downside"] += m["downside"]
    t["roi"] = round(t["expected"] / t["cash_in"] * 100, 1) if t["cash_in"] else 0.0  # on all the cash set aside
    # Money-weighted days to sell, and what that return works out to per month
    t["days"] = round(sum(o["m"]["days"] * o["m"]["cash_need"] for o in chosen) / t["cash_in"]) if t["cash_in"] else 0
    t["roi_month"] = round(t["roi"] * 30 / max(t["days"], 1), 1) if t["days"] else 0.0
    t["confidence"] = round(sum(o["m"]["confidence"] * o["m"]["cash_need"] for o in chosen) / t["cash_in"]) if t["cash_in"] else 0
    if cash is not None:
        t["cash_left"] = round(cash - t["cash_in"])
        t["used_pct"] = round(t["cash_in"] / cash * 100) if cash else 0
    return t
