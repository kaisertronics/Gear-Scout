"""Offer log: the owner taps a listing card to record an offer they made and the
seller's reply (accepted / countered / declined / no reply). /offers shows which
discounts and sellers actually worked, and the average accepted discount is a
hint on the offer helper. Lives in /data/offers.json (never touches the DB or
config); OFFER_LOG_PATH overrides the file for tests."""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PATH = Path(os.environ.get("OFFER_LOG_PATH") or "/data/offers.json")
REPLIES = ("accepted", "countered", "declined", "no_reply")
LABELS = {"accepted": "Accepted", "countered": "Countered", "declined": "Declined", "no_reply": "No reply"}
MIN_FOR_HINT = 3  # accepted offers needed before the helper shows a hint

_cache: dict = {"mtime": None, "path": None, "items": []}


def _num(v) -> Optional[float]:
    try:
        n = float(re.sub(r"[^\d.]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return None
    return n if n > 0 else None


def load() -> list[dict]:
    try:
        items = json.loads(PATH.read_text())
    except (OSError, ValueError):
        return []
    return [o for o in items if isinstance(o, dict) and o.get("id")] if isinstance(items, list) else []


def _save(items: list[dict]) -> None:
    tmp = Path(str(PATH) + ".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, PATH)


def add(url, title, source, seller, asking, offered, reply, counter=None) -> Optional[dict]:
    """Records one offer. None if the numbers or reply are unusable."""
    asking, offered, counter = _num(asking), _num(offered), _num(counter)
    if reply not in REPLIES or not asking or not offered or offered > asking:
        return None
    if reply == "countered" and not counter:
        return None
    o = {"id": uuid.uuid4().hex[:8], "at": datetime.now(timezone.utc).isoformat(),
         "url": str(url or "")[:500], "title": re.sub(r"\s+", " ", str(title or "")).strip()[:120],
         "source": str(source or "").split(" — ")[0][:60], "seller": str(seller or "")[:60],
         "asking": asking, "offered": offered, "reply": reply,
         "counter": counter if reply == "countered" else None}
    items = load()
    items.append(o)
    _save(items)
    return o


def remove(oid: str) -> bool:
    items = load()
    kept = [o for o in items if o["id"] != oid]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def discount(o: dict) -> float:
    """Percent the offer was under the asking price."""
    return round((o["asking"] - o["offered"]) / o["asking"] * 100, 1)


def counter_discount(o: dict) -> Optional[float]:
    """Percent the seller's counter was under the asking price."""
    c = o.get("counter")
    return round((o["asking"] - c) / o["asking"] * 100, 1) if c else None


def _avg(xs: list) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _group(items: list[dict], key: str) -> list[dict]:
    """Per source / seller: offers made, how many were accepted, avg accepted discount."""
    g: dict = {}
    for o in items:
        k = o.get(key) or ""
        if k:
            g.setdefault(k, []).append(o)
    out = [{"name": k, "n": len(v), "accepted": sum(1 for o in v if o["reply"] == "accepted"),
            "avg_accepted": _avg([discount(o) for o in v if o["reply"] == "accepted"])}
           for k, v in g.items()]
    return sorted(out, key=lambda x: (-x["accepted"], -x["n"], x["name"]))


def summary(items: Optional[list[dict]] = None) -> dict:
    items = load() if items is None else items
    acc = [o for o in items if o["reply"] == "accepted"]
    cnt = [o for o in items if o["reply"] == "countered"]
    # accepted/countered/declined by how deep the offer was
    bands = []
    for lo, hi in ((0, 10), (10, 15), (15, 20), (20, 100)):
        b = [o for o in items if lo <= discount(o) < hi]
        bands.append({"label": f"{lo}-{hi}%" if hi < 100 else f"{lo}%+", "n": len(b),
                      "accepted": sum(1 for o in b if o["reply"] == "accepted")})
    return {"n": len(items), "accepted": len(acc), "countered": len(cnt),
            "avg_accepted": _avg([discount(o) for o in acc]),
            "avg_counter": _avg([counter_discount(o) for o in cnt]),
            "bands": bands, "sources": _group(items, "source"), "sellers": _group(items, "seller"),
            "recent": sorted(items, key=lambda o: o["at"], reverse=True)}


def hint() -> Optional[str]:
    """One line for the offer helper, once enough accepted offers exist. Never raises."""
    try:
        m = PATH.stat().st_mtime
        if _cache["mtime"] != m or _cache["path"] != str(PATH):
            _cache.update(mtime=m, path=str(PATH), items=load())
        acc = [o for o in _cache["items"] if o["reply"] == "accepted"]
        if len(acc) < MIN_FOR_HINT:
            return None
        return f"your {len(acc)} accepted offers averaged {_avg([discount(o) for o in acc]):g}% under asking"
    except Exception:
        return None
