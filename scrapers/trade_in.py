"""
Gear trade-in value check (shown under the resale draft on the Saved page).

Compares a shop trade-in / cash offer (a fraction of the used value) with
selling it yourself on Reverb (used value minus fee and shipping). Pure; never
raises; returns None when the used value is missing.
"""
from __future__ import annotations

TRADE_IN_PCT = 0.60       # shops usually offer ~55-65% of used value
REVERB_FEE = 0.05         # Reverb selling fee
SHIP_COST = 30            # typical shipping you pay


def compare(used) -> dict | None:
    """{"used", "trade", "self", "best", "diff", "line"} or None."""
    try:
        u = float(str(used).replace("$", "").replace(",", "").strip())
        if u != u or u <= 0 or u == float("inf"):
            return None
        trade = round(u * TRADE_IN_PCT)
        selfnet = round(u * (1 - REVERB_FEE) - SHIP_COST)
        best = "reverb" if selfnet > trade else "trade-in"
        diff = abs(selfnet - trade)
        line = (f"Trade-in about ${trade:,}; selling on Reverb nets about ${selfnet:,} "
                f"(after ~{round(REVERB_FEE * 100)}% fee + ${SHIP_COST} shipping). ")
        line += (f"Reverb nets ~${diff:,} more." if best == "reverb"
                 else f"Trade-in nets ~${diff:,} more and is hassle-free.")
        return {"used": round(u), "trade": trade, "self": selfnet, "best": best,
                "diff": diff, "line": line}
    except Exception:
        return None
