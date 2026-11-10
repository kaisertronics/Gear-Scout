"""Your flips and your bankroll, in /data/gearflip/ledger.json (Gear Flip's
own file; Gear Scout's database and its older Flips tracker are untouched).

Bankroll = starting budget + profit from flips you've sold - money tied up
in flips you haven't sold yet. The plan only spends the cash that's free."""
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from gearflip import economics, settings as S


def _path():
    return S.data_dir() / "ledger.json"


def _num(v) -> Optional[float]:
    try:
        n = float(re.sub(r"[^\d.]", "", str(v if v is not None else "")) or "nan")
    except ValueError:
        return None
    return n if n == n and n >= 0 else None


def load() -> list[dict]:
    items = S.read_json(_path(), [])
    return [i for i in items if isinstance(i, dict) and i.get("id")] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    S.write_json(_path(), items)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def buy(opp: dict, paid=None) -> Optional[dict]:
    """Records "I bought this" from an opportunity (its numbers are kept, so
    you can compare the estimate with what really happened)."""
    m = opp.get("m") or {}
    paid = _num(paid) or m.get("buy_total")
    if not paid:
        return None
    items = load()
    for i in items:
        if opp.get("url") and i.get("url") == opp["url"] and i["status"] != "sold":
            return i
    i = {"id": uuid.uuid4().hex[:8], "at": _now(), "url": str(opp.get("url") or "")[:500],
         "title": str(opp.get("title") or "")[:140], "source": str(opp.get("source") or "")[:60],
         "paid": round(paid, 2), "repair_est": m.get("repair_cost") or 0, "repair_paid": None,
         "est": {k: m.get(k) for k in ("resale", "fees", "ship_out", "expected", "days", "channel", "roi")},
         "status": "bought", "sold_price": None, "sold_ship": None, "sold_fees": None, "sold_channel": None,
         "sold_at": None}
    items.append(i)
    _save(items)
    return i


def repaired(fid: str, cost) -> Optional[dict]:
    cost = _num(cost)
    if cost is None:
        return None
    items = load()
    for i in items:
        if i["id"] == fid:
            i["repair_paid"] = round(cost, 2)
            _save(items)
            return i
    return None


def sold(fid: str, price, ship=0, channel: str = "reverb") -> Optional[dict]:
    """Marks a flip sold. Fees are worked out from the channel's rates."""
    price, ship = _num(price), _num(ship) or 0
    if not price:
        return None
    channel = channel if channel in economics.CHANNELS else "reverb"
    c = economics.CHANNELS[channel]
    items = load()
    for i in items:
        if i["id"] == fid:
            i.update(status="sold", sold_price=round(price, 2), sold_ship=round(ship, 2), sold_channel=channel,
                     sold_fees=round(price * c["pct"] + c["fixed"], 2), sold_at=_now())
            _save(items)
            return i
    return None


def remove(fid: str) -> bool:
    items = load()
    kept = [i for i in items if i["id"] != fid]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def cost_of(i: dict) -> float:
    return i["paid"] + (i["repair_paid"] if i.get("repair_paid") is not None else 0)


def profit_of(i: dict) -> Optional[float]:
    if i.get("status") != "sold" or i.get("sold_price") is None:
        return None
    return round(i["sold_price"] - (i.get("sold_fees") or 0) - (i.get("sold_ship") or 0) - cost_of(i), 2)


def _days(a, b) -> Optional[float]:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def bankroll(budget: float, items: Optional[list[dict]] = None) -> dict:
    items = load() if items is None else items
    done = [i for i in items if i.get("status") == "sold"]
    open_ = [i for i in items if i.get("status") != "sold"]
    realized = sum(profit_of(i) or 0 for i in done)
    tied = sum(cost_of(i) + (0 if i.get("repair_paid") is not None else (i.get("repair_est") or 0)) for i in open_)
    put_in = sum(cost_of(i) for i in done)
    held = [d for d in (_days(i["at"], i["sold_at"]) for i in done) if d is not None]
    est = [i["est"].get("expected") for i in done if (i.get("est") or {}).get("expected") is not None]
    for i in done:
        i["profit"] = profit_of(i)
    return {"budget": budget, "realized": round(realized, 2), "tied": round(tied, 2),
            "cash": round(budget + realized - tied, 2), "value": round(budget + realized, 2),
            "sold": len(done), "open": len(open_),
            "roi": round(realized / put_in * 100, 1) if put_in else None,
            "avg_days": round(sum(held) / len(held)) if held else None,
            "vs_estimate": round(realized - sum(est), 2) if est and len(est) == len(done) else None,
            "open_items": sorted(open_, key=lambda i: i["at"], reverse=True),
            "sold_items": sorted(done, key=lambda i: i.get("sold_at") or "", reverse=True)}


def open_urls() -> set:
    return {i["url"] for i in load() if i.get("url") and i.get("status") != "sold"}
