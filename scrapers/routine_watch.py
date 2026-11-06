"""
Watchdog for the two Claude routines that maintain Gear Scout (the 10:10 AM
nightly update and the 11:40 AM code review).

A routine that asks for a permission nobody answers freezes silently — and a
frozen run blocks the next day's. That cost 23 hours on Oct 28 and 4 days in
Oct 20–23, found only when the owner noticed nothing was changing. Each
routine writes its log file when it finishes (docs/quality_log.md, docs/
code_review_log.md, visible here through the /config mount of the project
folder), so a log older than 30 hours means the routine hasn't finished.
This pushes and emails once a day per routine until it has.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT = Path("/config")
STATE = Path("/data/routine_watch.json")
STALE_HOURS = 30
REPEAT_HOURS = 24
ROUTINES = {
    "nightly update": "docs/quality_log.md",
    "daily code review": "docs/code_review_log.md",
}


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def stale(now=None, stale_hours: float = STALE_HOURS) -> list[tuple[str, float, datetime]]:
    """[(routine, hours since its log was last written, when)] for each one overdue."""
    now = now or datetime.now(timezone.utc)
    out = []
    for name, rel in ROUTINES.items():
        p = PROJECT / rel
        try:
            written = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
        except OSError:
            continue  # no log yet: nothing to compare with
        hours = (now - written).total_seconds() / 3600
        if hours > stale_hours:
            out.append((name, hours, written))
    return out


def check(cfg: dict, now=None) -> int:
    from scrapers.notify import push_enabled, send_push
    now = now or datetime.now(timezone.utc)
    state = _state()
    sent = 0
    for name, hours, written in stale(now):
        try:
            last = datetime.fromisoformat(state.get(name, ""))
            if now - last < timedelta(hours=REPEAT_HOURS):
                continue
        except ValueError:
            pass
        try:
            from zoneinfo import ZoneInfo
            when = written.astimezone(ZoneInfo(cfg.get("timezone") or "America/Los_Angeles")).strftime("%a %-I:%M %p")
        except Exception:
            when = written.strftime("%a %H:%M UTC")
        title = f"⚠️ Gear Scout's {name} hasn't finished in {hours:.0f} h"
        text = (f"Its log was last updated {when}. It's most likely frozen on a permission prompt nobody can see, "
                f"which also blocks the next day's run. Open the Claude app and find the session named "
                f"“{'Gear Scout nightly improvements' if name == 'nightly update' else 'Gear Scout daily code review'}” "
                f"in your session list (it shows as running), then approve the waiting step or switch it to Auto mode.")
        if push_enabled(cfg):
            send_push(cfg, title, text, tags=["warning"])
            sent += 1
        email_cfg = cfg.get("email") or {}
        if email_cfg.get("from") and email_cfg.get("password"):
            try:
                from html import escape
                from scrapers.emailer import send_html_email
                send_html_email(f"<html><body style='font-family:sans-serif'><h3>{escape(title)}</h3><p>{escape(text)}</p></body></html>",
                                title, email_cfg)
            except Exception:
                logger.exception("Routine watchdog email failed")
        state[name] = now.isoformat()
        logger.warning("Routine watchdog: %s has not finished in %.0f h", name, hours)
    try:
        STATE.write_text(json.dumps(state))
    except OSError:
        pass
    return sent
