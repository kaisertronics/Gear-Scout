"""Turns listings into ranked flip opportunities and saves the latest scan to
/data/gearflip/scan.json, so the Gear Flip tab opens instantly (the page only
reads that file; the scan runs in the background)."""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from gearflip import economics, feedback, ledger, photos, portfolio, settings as S, source, verify

logger = logging.getLogger(__name__)

NEW_RE = re.compile(r"^\W*new\b|\[new\]|\bnew (?:in|with|old)\b|\bnos\b|\bbrand new\b|\bsealed\b|\bunopened\b", re.I)
MAX_KEEP = 150
FAKE_RISK_VALUE = 400   # pricey gear (pro mics especially) is what gets faked or misdescribed
FAKE_RISK_SHARE = 0.65  # ...when it asks under this share of its used value


def fake_risk(ask: float, value: float, repair, confirmed: bool) -> str:
    """A warning when expensive working gear is priced well under market and no
    photo has confirmed it's the real unit."""
    if repair or confirmed or value < FAKE_RISK_VALUE or ask >= value * FAKE_RISK_SHARE:
        return ""
    return f"fake risk: {ask / value * 100:.0f}% of its ~${value:,.0f} value, check photos and serial first"
_lock = threading.Lock()


def scan_path():
    return S.data_dir() / "scan.json"


def describe(m: dict) -> str:
    """One plain-English line with the money story."""
    parts = [f"Pay ~${m['buy_total']:,}"]
    if m["repair"]:
        parts.append(f"fix ({m['repair']}, ~${m['repair_cost']:,}, {m['repair_odds']}% odds it works)")
    parts.append(f"sell ~${m['resale']:,} on {m['channel_label']}")
    parts.append(f"fees ${m['fees']:,}" + (f" + shipping ${m['ship_out']:,}" if m["ship_out"] else ""))
    return ", ".join(parts) + f" = ~${m['expected']:,} profit ({m['roi']:.0f}% ROI) in ~{m['days']} days."


def _miss(m: dict, st: dict) -> str:
    """Which of your minimums a flip missed first (for the "why so few" line)."""
    if m["expected"] < float(st.get("min_profit", 40)):
        return "not enough profit after tax, fees and shipping"
    if m["roi"] < float(st.get("min_roi", 25)):
        return "return (ROI) under your minimum"
    if m["confidence"] < float(st.get("min_confidence", 35)):
        return "confidence under your minimum"
    return "too slow to sell"


def analyze_rows(rows: list[dict], cfg: dict, st: dict, speed: Optional[dict] = None,
                 prep=None, now: Optional[datetime] = None, pics: Optional[dict] = None,
                 dropped: Optional[list] = None, funnel: Optional[dict] = None) -> list[dict]:
    """Every listing that clears your minimums, with its numbers, best first.
    `pics`: photo-check results by link; ads the photo check rejects go to `dropped`."""
    prep = prep or source.prepare
    speed = speed if speed is not None else {}
    pics = pics if pics is not None else {}
    fb = feedback.load()
    hidden, rules = feedback.hidden_urls(fb), feedback.learned(fb)
    out, seen = [], set()
    why = funnel if funnel is not None else {}
    for r in rows:
        if r.get("url") in hidden:  # you ✕'d it
            continue
        try:
            p = prep(r, cfg, st.get("include_repairs", True), now, why) if prep is source.prepare else \
                prep(r, cfg, st.get("include_repairs", True), now)
        except Exception:
            logger.exception("Gear Flip: reading %s", r.get("url"))
            continue
        if not p:
            continue
        ad = (re.sub(r"\s+", " ", p["title"].lower()), round(p["ask"]))
        if ad in seen:  # the same ad cross-posted
            continue
        seen.add(ad)
        pic = pics.get(r.get("url"))
        fx = photos.effect(pic)
        if fx["drop"]:
            if dropped is not None:
                dropped.append({"url": r.get("url"), "title": p["title"], "why": fx["drop"]})
            continue
        repair = p["repair"] or fx["repair"]
        confirmed = bool(pic and pic.get("is_item") == "yes" and pic.get("matches") == "yes" and not pic.get("stock"))
        if not repair and p["ask"] < p["value"] * fx["value_x"] * verify.TOO_CHEAP_UNCHECKED and not confirmed:
            if dropped is not None:
                dropped.append({"url": r.get("url"), "title": p["title"], "image": r.get("image_url"),
                                "held": pic is None, "why": "priced too far under value to trust until a photo confirms it"})
            continue
        risk = fake_risk(p["ask"], p["value"] * fx["value_x"], repair, confirmed)
        you = feedback.adjust(rules, model=p["model"], title=p["title"], repair=repair, seller=r.get("seller"), photo=pic,
                              ask=p["ask"], value=p["value"])
        if you["drop"]:
            if dropped is not None:
                dropped.append({"url": r.get("url"), "title": p["title"], "image": r.get("image_url"),
                                "held": pic is None and "photo" in you["drop"], "why": "from your ✕s: " + you["drop"]})
            continue
        m = economics.analyze(title=p["title"], ask=p["ask"], value=p["value"] * fx["value_x"] * you["value_x"],
                              n=p["n"], sold_basis=p["sold_basis"], ship_in=p["ship_in"], local=p["local"],
                              repair=repair, days_base=speed.get(p["model"]), age_days=p["age_days"],
                              flags=p["flags"], settings=st,
                              conf_adj=fx["conf"] + you["conf"] + (-10 if risk else 0) + (-15 if p.get("basis") == "similar" else 0))
        if not m:
            continue
        m["score"] = int(round(m["score"] * you["score_x"]))
        if not economics.passes(m, st):
            why[_miss(m, st)] = why.get(_miss(m, st), 0) + 1
            continue
        out.append({"url": r.get("url"), "title": p["title"], "source": (r.get("source_name") or "").split(" — ")[0],
                    "source_full": r.get("source_name"), "image": r.get("image_url"), "location": r.get("location"),
                    "posted": r.get("posted_at") or r.get("first_seen"), "first_seen": r.get("first_seen"),
                    "age_days": round(p["age_days"], 1) if p["age_days"] is not None else None,
                    "offers_ok": r.get("offers_ok"), "model": p["model"], "seller": r.get("seller"),
                    "basis": p.get("basis", "model"),
                    "flags": (p.get("flag_text") or []) + fx["tags"] + ([risk] if risk else []),
                    "photo": pic, "m": m, "why": describe(m)})
    out.sort(key=lambda o: (-o["m"]["score"], -o["m"]["expected"]))
    return out


def scan(cfg: Optional[dict] = None, st: Optional[dict] = None) -> dict:
    """One full pass over the listings. Saves and returns the result."""
    if not _lock.acquire(blocking=False):
        return latest()
    try:
        t0 = time.time()
        cfg = cfg if cfg is not None else source.load_config()
        st = st or S.load()
        rows = source.listing_rows()
        dropped: list = []
        funnel: dict = {}
        opps = analyze_rows(rows, cfg, st, source.sell_speed(), pics=photos.results(), dropped=dropped, funnel=funnel)
        for d in dropped:
            k = "waiting for a photo check" if d.get("held") else (
                "left out because of your ✕s" if d["why"].startswith("from your") else "hidden by the photo check or price rules")
            funnel[k] = funnel.get(k, 0) + 1
        opps = source.drop_stolen_photos(opps[:MAX_KEEP * 2])[:MAX_KEEP]
        res = {"at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
               "rows": len(rows), "found": len(opps), "settings": st, "opps": opps,
               "photo_dropped": dropped[:60], "held": [d for d in dropped if d.get("held")][:100],
               "funnel": sorted(funnel.items(), key=lambda kv: -kv[1]), "photo_ai": bool(source.gemini_key(cfg)),
               "photo_paused": source.gemini_paused()}
        S.write_json(scan_path(), res)
        logger.info("Gear Flip scan: %d listings -> %d opportunities in %.1fs", len(rows), len(opps), res["seconds"])
        return res
    finally:
        _lock.release()


def scan_in_background(cfg: Optional[dict] = None) -> bool:
    if _lock.locked():
        return False
    threading.Thread(target=lambda: _safe_scan(cfg), daemon=True, name="gearflip-scan").start()
    return True


def _safe_scan(cfg):
    try:
        scan(cfg)
    except Exception:
        logger.exception("Gear Flip scan failed")


def scanning() -> bool:
    return _lock.locked()


def latest() -> dict:
    return S.read_json(scan_path(), {}) or {}


PHOTO_WAIT_MIN = 30


def photo_wait(o: dict, res: dict, now: Optional[datetime] = None) -> bool:
    """True while a flip should wait for its picture check: only when picture
    analysis is on and not resting, and for at most PHOTO_WAIT_MIN minutes after
    Gear Scout found the ad. After that it's shown and pushed marked
    "photo not checked yet" rather than held back for hours."""
    if not res.get("photo_ai") or o.get("photo") or res.get("photo_paused"):
        return False
    try:
        found = datetime.fromisoformat(str(o.get("first_seen")))
        found = found if found.tzinfo else found.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return ((now or datetime.now(timezone.utc)) - found).total_seconds() < PHOTO_WAIT_MIN * 60


def plan(res: Optional[dict] = None, st: Optional[dict] = None, book: Optional[dict] = None) -> dict:
    """The best set of buys for the cash you have free right now."""
    res = res if res is not None else latest()
    st = st or S.load()
    book = book or ledger.bankroll(st["budget"])
    owned = ledger.open_urls() | feedback.hidden_urls()
    opps = [o for o in (res.get("opps") or []) if o.get("url") not in owned and economics.passes(o["m"], st)]
    # With picture analysis on, a brand-new flip waits a little for its photo check
    opps = [o for o in opps if not photo_wait(o, res)]
    cash = max(0.0, book["cash"])
    chosen = portfolio.optimize(opps, cash, int(st.get("max_per_model", 1)))
    return {"cash": cash, "items": chosen, "t": portfolio.totals(chosen, cash),
            "rank": {o["url"]: i + 1 for i, o in enumerate(chosen)}}
