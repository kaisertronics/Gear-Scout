"""Resale profit estimate: used value (market_value.value) x 0.92 net of
Reverb fees, minus shipping, minus the ask. Card tag only."""
from typing import Optional

NET_FACTOR = 0.92       # ~5% Reverb fee + ~3% payment processing
SHIP_MIC = 25
SHIP_CHEAP = 15         # asking price under $150
CHEAP_BELOW = 150
MIN_PROFIT = 50
MIN_COMPS = 3


def estimate(title: Optional[str], description: Optional[str], ask) -> Optional[dict]:
    """{"net","profit","value"} or None. Never raises."""
    try:
        if not title or not ask or ask <= 0:
            return None
        from scrapers import market_value as mv
        v, n = mv.value(title, description)
        if not v or v <= 0 or (n or 0) < MIN_COMPS:
            return None
        ship = SHIP_CHEAP if ask < CHEAP_BELOW else SHIP_MIC
        net = v * NET_FACTOR - ship
        profit = net - ask
        if profit < MIN_PROFIT:
            return None
        return {"net": round(net), "profit": round(profit), "value": round(v),
                "ship": ship, "n": n}
    except Exception:
        return None
