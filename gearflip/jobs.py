"""One background pass: scour -> scan -> alert (and the morning email).

Gear Scout's scheduler calls tick() every 5 minutes; tick() itself decides
whether a pass is due (Settings → "Scan every N minutes"), so changing that
needs no restart. Never raises."""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from gearflip import alerts, engine, ledger, scour, settings as S, source

logger = logging.getLogger(__name__)

DIGEST_HOUR = 8


def _status_path():
    return S.data_dir() / "jobs.json"


def status() -> dict:
    return S.read_json(_status_path(), {}) or {}


def _local_now(cfg: dict) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo((cfg.get("schedule") or {}).get("timezone") or "America/Los_Angeles"))
    except Exception:
        return datetime.now()


def due(st: dict, now: Optional[float] = None) -> bool:
    last = status().get("ts") or 0
    return (now or time.time()) - last >= st["scan_minutes"] * 60 - 30


def tick(cfg: Optional[dict] = None, force: bool = False) -> dict:
    cfg = cfg if cfg is not None else source.load_config()
    st = S.load()
    if not force and not due(st):
        return {"skipped": "not due"}
    out = {"at": datetime.now(timezone.utc).isoformat(), "ts": time.time()}
    S.write_json(_status_path(), {**status(), "ts": out["ts"], "running": True})
    try:
        if st["scour"] and not source.scrape_busy():
            out["scour"] = scour.run(cfg, st)
        res = engine.scan(cfg, st)
        out["scan"] = {k: res.get(k) for k in ("rows", "found", "seconds")}
        book = ledger.bankroll(st["budget"])
        plan = engine.plan(res, st, book)
        out["plan"] = {k: plan["t"].get(k) for k in ("n", "cash_in", "expected", "roi", "days")}
        out["alerts"] = alerts.send(cfg, res, plan, st)
        local = _local_now(cfg)
        today = local.date().isoformat()
        prev = status().get("digest_day")
        out["digest_day"] = prev
        if st["email_digest"] and local.hour >= DIGEST_HOUR and prev != today:
            out["digest_sent"] = alerts.send_digest(cfg, res, plan, book)
            out["digest_day"] = today  # once a day, sent or not (no retry every pass)
    except Exception as e:
        logger.exception("Gear Flip pass failed")
        out["error"] = str(e)[:300]
    out["running"] = False
    S.write_json(_status_path(), out)
    return out
