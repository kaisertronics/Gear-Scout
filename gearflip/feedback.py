"""Your ✕ on a suggestion, and why, so Gear Flip picks better next time.

Each dismissal is kept in /data/gearflip/feedback.json with the reason you
chose. The ad is hidden for good, and the reasons become rules:

  wrong item / wrong model  -> that model's ads need more confidence; 2+ = the
                               photo/model must be confirmed (stricter)
  resale too optimistic     -> that model's resale estimate drops 8% each time (down to 70%)
  repair too risky          -> that kind of repair loses confidence; 3+ = left out
  too big / heavy           -> that size class loses score; 2+ = left out
  not gear I'd flip         -> that brand loses confidence; 2+ = left out
  sketchy seller            -> that seller is left out
  already sold / other      -> just hidden (your note is kept)

The tab lists what it learned, and you can undo any rule. Pure rules (no AI)."""
import re
from datetime import datetime, timezone
from typing import Optional

from gearflip import economics, settings as S

REASONS = {
    "wrong_item": "Not the real item (box, case, part, wrong thing)",
    "wrong_model": "Wrong model / not what the title says",
    "price_off": "Resale estimate too high: it won't sell for that",
    "risky": "Repair too risky",
    "too_big": "Too big or heavy to deal with",
    "not_my_gear": "Not gear I'd flip",
    "seller": "Sketchy seller or ad",
    "sold": "Already sold / gone",
    "other": "Something else",
}


def _path():
    return S.data_dir() / "feedback.json"


def load() -> dict:
    d = S.read_json(_path(), {}) or {}
    return {"items": d.get("items") or [], "undone": d.get("undone") or []}


def brand(model: Optional[str]) -> str:
    return (model or "").split(":")[0]


def dismiss(opp: dict, reason: str, note: str = "") -> dict:
    reason = reason if reason in REASONS else "other"
    m = opp.get("m") or {}
    d = load()
    d["items"].append({
        "at": datetime.now(timezone.utc).isoformat(), "url": str(opp.get("url") or "")[:500],
        "title": str(opp.get("title") or "")[:140], "model": opp.get("model") or "", "brand": brand(opp.get("model")),
        "size": m.get("size") or economics.size_class(opp.get("title")), "repair": m.get("repair"),
        "seller": (opp.get("seller") or "")[:80], "source": (opp.get("source") or "")[:60],
        "reason": reason, "note": re.sub(r"\s+", " ", note or "").strip()[:300],
        "ask": m.get("ask"), "resale": m.get("resale"), "expected": m.get("expected")})
    S.write_json(_path(), d)
    return d["items"][-1]


def undo(rule: str) -> None:
    """Forget one learned rule (e.g. "price_off:neumann:tlm103")."""
    d = load()
    if rule and rule not in d["undone"]:
        d["undone"].append(rule)
        S.write_json(_path(), d)


def hidden_urls(d: Optional[dict] = None) -> set:
    return {i["url"] for i in (d or load())["items"] if i.get("url")}


def learned(d: Optional[dict] = None) -> dict:
    """{rule id: {"kind", "key", "n", "text"}} from the dismissals (minus undone ones)."""
    d = d or load()
    count: dict = {}
    for i in d["items"]:
        r = i["reason"]
        key = {"wrong_item": i.get("model"), "wrong_model": i.get("model"), "price_off": i.get("model"),
               "risky": i.get("repair"), "too_big": i.get("size"), "not_my_gear": i.get("brand"),
               "seller": i.get("seller") or None}.get(r)
        if key:
            rid = f"{'model_check' if r in ('wrong_item', 'wrong_model') else r}:{key}"
            count[rid] = count.get(rid, 0) + 1
    out = {}
    for rid, n in count.items():
        if rid in d["undone"]:
            continue
        kind, key = rid.split(":", 1)
        nice = key.replace(":", " ").replace("-", " ")
        text = {"model_check": f"{nice}: be stricter (you flagged wrong item/model {n}×)",
                "price_off": f"{nice}: resale estimate cut {round(100 - _price_x(n) * 100)}%",
                "risky": f"'{key}' repairs: " + ("left out" if n >= 3 else f"less confident ({n}×)"),
                "too_big": f"{key} gear: " + ("left out" if n >= 2 else "ranked lower"),
                "not_my_gear": f"{nice}: " + ("left out" if n >= 2 else "ranked lower"),
                "seller": f"seller {key}: left out"}.get(kind, rid)
        out[rid] = {"kind": kind, "key": key, "n": n, "text": text}
    return out


def _price_x(n: int) -> float:
    return max(0.70, 1 - 0.08 * n)


def adjust(rules: dict, *, model: str, title: str, repair: Optional[str], seller: Optional[str],
           photo: Optional[dict] = None) -> dict:
    """What your past dismissals do to one listing: {"drop": reason|None, "value_x", "conf", "score_x"}."""
    e = {"drop": None, "value_x": 1.0, "conf": 0, "score_x": 1.0}
    b, size = brand(model), economics.size_class(title)
    if seller and (r := rules.get(f"seller:{seller[:80]}")):
        e["drop"] = "a seller you flagged"
        return e
    if (r := rules.get(f"not_my_gear:{b}")):
        if r["n"] >= 2:
            e["drop"] = f"{b} isn't gear you flip"
            return e
        e["conf"] -= 10
        e["score_x"] *= 0.75
    if (r := rules.get(f"too_big:{size}")):
        if r["n"] >= 2:
            e["drop"] = f"{size} gear is too big for you"
            return e
        e["score_x"] *= 0.7
    if repair and (r := rules.get(f"risky:{repair}")):
        if r["n"] >= 3:
            e["drop"] = f"you find {repair} repairs too risky"
            return e
        e["conf"] -= 10 * r["n"]
    if (r := rules.get(f"price_off:{model}")):
        e["value_x"] *= _price_x(r["n"])
    if (r := rules.get(f"model_check:{model}")):
        e["conf"] -= 10 * min(r["n"], 3)
        confirmed = photo and photo.get("matches") == "yes" and photo.get("is_item") == "yes"
        if r["n"] >= 2 and not confirmed:
            e["drop"] = "this model often wasn't the real thing; waiting for a photo that confirms it"
    return e
