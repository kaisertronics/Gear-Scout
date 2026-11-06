"""Turns listings into ranked flip opportunities and saves the latest scan to
/data/gearflip/scan.json, so the Gear Flip tab opens instantly (the page only
reads that file; the scan runs in the background)."""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from gearflip import economics, feedback, ledger, photos, portfolio, settings as S, source

logger = logging.getLogger(__name__)

NEW_RE = re.compile(r"^\W*new\b|\[new\]|\bnew (?:in|with|old)\b|\bnos\b|\bbrand new\b|\bsealed\b|\bunopened\b", re.I)
MAX_KEEP = 150
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


def analyze_rows(rows: list[dict], cfg: dict, st: dict, speed: Optional[dict] = None,
                 prep=None, now: Optional[datetime] = None, pics: Optional[dict] = None,
                 dropped: Optional[list] = None) -> list[dict]:
    """Every listing that clears your minimums, with its numbers, best first.
    `pics`: photo-check results by link; ads the photo check rejects go to `dropped`."""
    prep = prep or source.prepare
    speed = speed if speed is not None else {}
    pics = pics if pics is not None else {}
    fb = feedback.load()
    hidden, rules = feedback.hidden_urls(fb), feedback.learned(fb)
    out, seen = [], set()
    for r in rows:
        if r.get("url") in hidden:  # you ✕'d it
            continue
        try:
            p = prep(r, cfg, st.get("include_repairs", True), now)
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
        you = feedback.adjust(rules, model=p["model"], title=p["title"], repair=repair, seller=r.get("seller"), photo=pic)
        if you["drop"]:
            if dropped is not None:
                dropped.append({"url": r.get("url"), "title": p["title"], "why": "from your ✕s: " + you["drop"]})
            continue
        m = economics.analyze(title=p["title"], ask=p["ask"], value=p["value"] * fx["value_x"] * you["value_x"],
                              n=p["n"], sold_basis=p["sold_basis"], ship_in=p["ship_in"], local=p["local"],
                              repair=repair, days_base=speed.get(p["model"]), age_days=p["age_days"],
                              flags=p["flags"], settings=st, conf_adj=fx["conf"] + you["conf"])
        if not m:
            continue
        m["score"] = int(round(m["score"] * you["score_x"]))
        if not economics.passes(m, st):
            continue
        out.append({"url": r.get("url"), "title": p["title"], "source": (r.get("source_name") or "").split(" — ")[0],
                    "source_full": r.get("source_name"), "image": r.get("image_url"), "location": r.get("location"),
                    "posted": r.get("posted_at") or r.get("first_seen"), "first_seen": r.get("first_seen"),
                    "age_days": round(p["age_days"], 1) if p["age_days"] is not None else None,
                    "offers_ok": r.get("offers_ok"), "model": p["model"], "seller": r.get("seller"),
                    "flags": (p.get("flag_text") or []) + fx["tags"],
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
        opps = analyze_rows(rows, cfg, st, source.sell_speed(), pics=photos.results(), dropped=dropped)
        opps = source.drop_stolen_photos(opps[:MAX_KEEP * 2])[:MAX_KEEP]
        res = {"at": datetime.now(timezone.utc).isoformat(), "seconds": round(time.time() - t0, 1),
               "rows": len(rows), "found": len(opps), "settings": st, "opps": opps,
               "photo_dropped": dropped[:60], "photo_ai": bool(source.gemini_key(cfg))}
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


def plan(res: Optional[dict] = None, st: Optional[dict] = None, book: Optional[dict] = None) -> dict:
    """The best set of buys for the cash you have free right now."""
    res = res if res is not None else latest()
    st = st or S.load()
    book = book or ledger.bankroll(st["budget"])
    owned = ledger.open_urls() | feedback.hidden_urls()
    opps = [o for o in (res.get("opps") or []) if o.get("url") not in owned and economics.passes(o["m"], st)]
    # With picture analysis on, the plan only holds flips whose photo checked out
    if res.get("photo_ai"):
        opps = [o for o in opps if o.get("photo")]
    cash = max(0.0, book["cash"])
    chosen = portfolio.optimize(opps, cash, int(st.get("max_per_model", 1)))
    return {"cash": cash, "items": chosen, "t": portfolio.totals(chosen, cash),
            "rank": {o["url"]: i + 1 for i, o in enumerate(chosen)}}
