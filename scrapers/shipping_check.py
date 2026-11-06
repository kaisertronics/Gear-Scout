"""Shipping-cost reality check: warns when listed shipping is big enough to
make the deal worse than it looks. Card tag only; the card already shows the
delivered price and free / local pickup lines."""
from typing import Optional

MIN_PCT = 0.08      # shipping >= 8% of the price ...
MIN_DOLLARS = 30    # ... or >= $30


def check(price, shipping, used_value=None) -> Optional[dict]:
    """{"ship","real","pct_under"} (pct_under None without a used value), or
    None when shipping is unknown, free or small. Never raises."""
    try:
        if isinstance(price, bool) or isinstance(shipping, bool):
            return None
        if not isinstance(price, (int, float)) or not isinstance(shipping, (int, float)):
            return None
        if price <= 0 or shipping <= 0:
            return None
        if shipping < MIN_DOLLARS and shipping < price * MIN_PCT:
            return None
        real = price + shipping
        pct = None
        if isinstance(used_value, (int, float)) and not isinstance(used_value, bool) and used_value > 0:
            pct = round((used_value - real) / used_value * 100)
        return {"ship": round(shipping), "real": round(real), "pct_under": pct}
    except Exception:
        return None


def label(info: Optional[dict]) -> Optional[str]:
    """Short tag text for check()'s result."""
    if not info:
        return None
    s = f"Ships ${info['ship']:,}: real price ${info['real']:,}"
    p = info.get("pct_under")
    if p is None:
        return s
    if p >= 0:
        return f"{s}, {p}% under used value"
    return f"{s}, {-p}% over used value"
