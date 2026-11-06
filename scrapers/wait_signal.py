"""Wait signal per model: is waiting smart, or is now a good time to buy?

Built on price_history's series (the same points the "Asking price history"
panel draws), so there are no queries of its own. "Usual level" is the median
of all period medians in the window; "lately" is the median of the newest
RECENT_WEEKS weekly points (or the newest monthly point). Rules, in order:

  - lately SAME_PCT+ under the usual level   -> Buy now
  - lately SAME_PCT+ over the usual level    -> Wait (says whether it is still
                                                 rising or already falling)
  - otherwise                                -> Steady

A model price_history gave no chart for gets no signal. The window is six
months, so nothing seasonal can be claimed from it; no "prices ease after the
holidays" line is ever made up.
"""
from statistics import median
from typing import Optional

from scrapers.price_history import SAME_PCT

RECENT_WEEKS = 4   # weekly charts: "lately" = the newest this-many weekly points


def _pct(now: float, base: float) -> int:
    return round((now - base) / base * 100) if base else 0


def signal(h: dict) -> Optional[dict]:
    """One price_history.build() entry -> {tone: buy|wait|steady, label, why},
    or None when it has no usable points."""
    prices = [p["median"] for p in (h.get("points") or [])]
    if len(prices) < 3:
        return None
    k = RECENT_WEEKS if h.get("weekly") else 1
    lately, before = prices[-k:], prices[-2 * k:-k]
    base = median(prices)
    pct = _pct(median(lately), base)
    trend = _pct(median(lately), median(before)) if before else 0
    if pct <= -SAME_PCT:
        return {"tone": "buy", "label": "Buy now",
                "why": f"Asking prices are {abs(pct)}% under their usual level (${round(base):,})."}
    if pct >= SAME_PCT:
        move = ("and still rising" if trend >= SAME_PCT else
                "but already falling" if trend <= -SAME_PCT else "and holding")
        return {"tone": "wait", "label": "Wait",
                "why": f"Asking prices are {pct}% above their usual level (${round(base):,}), {move}."}
    return {"tone": "steady", "label": "Steady",
            "why": f"Asking prices are in line with their usual level (${round(base):,})."}


def signals(history: dict) -> dict:
    """price_history.history() output -> {model_key: signal} for models that get one."""
    out = {}
    for mk, h in (history or {}).items():
        s = signal(h)
        if s:
            out[mk] = s
    return out
