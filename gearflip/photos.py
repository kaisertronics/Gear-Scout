"""Picture analysis: an AI looks at each strong flip's photo before it's
pushed to you or put in your plan.

It answers: is this the actual unit (not a box, case, manual or accessory)?
Is it the model in the title (not a look-alike or cheaper model)? Is it a
stock/catalogue photo? What model can it read on the gear? What condition is
it in, and is there visible damage or anything missing?

What that does to a flip:
  wrong item / wrong model  -> dropped (listed under "Hidden by the photo check")
  stock photo               -> lower confidence (you can't see the real unit)
  worn / damaged            -> lower resale; damage on "working" gear adds a check-over repair
  missing parts             -> lower resale
  confirmed real photo      -> higher confidence

Uses the owner's own free Gemini key (Settings → AI in Gear Scout); no paid
API. A few new photos per pass, each checked once and remembered in
/data/gearflip/photos.json. Without a key, nothing changes and the cards say
"photo not checked"."""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from gearflip import settings as S, source

logger = logging.getLogger(__name__)

PER_PASS = 10
PROMPT = """You are checking the photo of a used pro-audio listing for someone who wants to buy it and resell it.
Listing title: {title}
Answer ONLY in JSON:
{{"is_item": "yes"|"no"|"unsure",
 "matches_model": "yes"|"no"|"unsure",
 "seen_model": "<brand/model text you can actually read on the gear, or empty>",
 "stock_photo": true|false,
 "condition": "mint"|"good"|"worn"|"damaged"|"unknown",
 "damage": ["<visible problems: dents, cracks, broken knobs, rust, corrosion, torn grille, burn marks...>"],
 "missing": ["<parts that look missing: knobs, grille, power supply, cable, rack ears...>"],
 "note": "<under 15 words, plain English>"}}
"is_item" = the photo shows the actual unit itself, not ONLY a box, case, manual, cable, shock mount or other accessory.
"matches_model" = it is the brand and model in the title (not a different, cheaper or look-alike model, not a clone sold as the original). Use "unsure" if you can't tell.
"stock_photo" = a catalogue or manufacturer image rather than a photo of this particular unit."""

COND_VALUE = {"mint": 1.03, "good": 1.0, "worn": 0.93, "damaged": 0.85, "unknown": 1.0}
MISSING_CUT = 0.05      # resale cut per missing part, up to 3


def _path():
    return S.data_dir() / "photos.json"


def results() -> dict:
    return S.read_json(_path(), {}) or {}


def _clean(out: dict) -> dict:
    def yn(v):
        v = str(v or "unsure").lower()
        return v if v in ("yes", "no", "unsure") else "unsure"
    cond = str(out.get("condition") or "unknown").lower()
    lst = lambda v: [str(x)[:60] for x in (v if isinstance(v, list) else []) if str(x).strip()][:5]
    return {"is_item": yn(out.get("is_item")), "matches": yn(out.get("matches_model")),
            "seen_model": str(out.get("seen_model") or "")[:60], "stock": bool(out.get("stock_photo")),
            "condition": cond if cond in COND_VALUE else "unknown", "damage": lst(out.get("damage")),
            "missing": lst(out.get("missing")), "note": str(out.get("note") or "")[:140]}


def effect(p: Optional[dict]) -> dict:
    """How one photo result changes the flip: {"drop": reason|None, "value_x", "conf", "repair", "tags"}."""
    e = {"drop": None, "value_x": 1.0, "conf": 0, "repair": None, "tags": []}
    if not p:
        return e
    if p["is_item"] == "no":
        e["drop"] = "the photo isn't the unit itself" + (f" ({p['note']})" if p["note"] else "")
        return e
    if p["matches"] == "no":
        e["drop"] = "the photo shows a different model" + (f": {p['seen_model']}" if p["seen_model"] else "")
        return e
    e["value_x"] = COND_VALUE.get(p["condition"], 1.0) * (1 - MISSING_CUT * min(len(p["missing"]), 3))
    if p.get("none"):
        e["conf"] -= 10
        e["tags"].append("no photo")
    elif p["stock"]:
        e["conf"] -= 15
        e["tags"].append("stock photo")
    elif p["matches"] == "yes" and p["is_item"] == "yes":
        e["conf"] += 8
    else:
        e["conf"] -= 5
    if p["condition"] == "damaged" or p["damage"]:
        e["repair"] = "check-over"
        e["tags"].append("visible damage")
    if p["missing"]:
        e["tags"].append("missing: " + ", ".join(p["missing"][:3]))
    return e


def check(cfg: dict, opps: list[dict], limit: int = PER_PASS) -> int:
    """Looks at the photos of the best flips not checked yet. Returns how many it checked."""
    key = source.gemini_key(cfg)
    if not key or not opps or source.gemini_paused():
        return 0  # no key, or Gemini is resting (shared with Gear Scout's other photo jobs)
    done = results()
    n = 0
    for o in opps:
        if n >= limit:
            break
        url, img = o.get("url"), o.get("image")
        if not url or url in done:
            continue
        photo = source.photo_bytes(img) if img else None
        if not photo:  # nothing to look at: counts as checked, with lower confidence
            done[url] = dict(_clean({"note": "no photo to check"}), none=True,
                             at=datetime.now(timezone.utc).isoformat())
            n += 1
            continue
        try:
            raw = source.ai_json(key, PROMPT.format(title=o.get("title") or ""), photo)
            res = _clean(json.loads(raw) if isinstance(raw, str) else raw)
        except Exception as e:
            logger.info("Gear Flip photo check stopped (%s); the rest wait for the next pass", e)
            source.gemini_backoff(e)
            break  # rate limit or outage
        res["at"] = datetime.now(timezone.utc).isoformat()
        done[url] = res
        n += 1
    if n:
        if len(done) > 5000:  # keep the newest
            done = dict(sorted(done.items(), key=lambda kv: kv[1].get("at", ""))[-4000:])
        S.write_json(_path(), done)
    return n
