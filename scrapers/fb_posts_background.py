"""
Facebook post search in the background, all day, through the WHOLE search
term list (keywords + learned terms), with the priority list mixed in so it
comes around several times a day.

Pacing (config `fb_post_search: background:`):
  per_hour      searches per hour (default 60)
  start_hour    first hour of the day to search (default 6)
  end_hour      stops at this hour (default 22)
  roundup_hour  hour of the daily "Facebook posts roundup" email (default 15)

Every 15 minutes a batch runs per_hour / 4 searches in one browser. A cursor
in the database remembers where it got to, so it walks the full list and
wraps around. If Facebook shows a "temporarily blocked" / checkpoint page,
it stops for 6 hours and sends a push notification instead of pushing on.

Hits are stored like any listing (source "FB Posts — …") and collected into
one roundup email each afternoon; they're left out of the regular digests.
"""
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from scrapers.base import Listing
from scrapers.store import _conn, mark_seen

logger = logging.getLogger(__name__)

BATCH_EVERY_MINUTES = 15
PRIORITY_PER_BATCH = 2
BLOCK_PAUSE_HOURS = 6
_BLOCKED = re.compile(
    r"you'?re temporarily blocked|you can'?t use this feature|we limit how often|"
    r"try again later|suspicious activity|confirm your identity", re.I)


def settings(cfg: dict) -> dict:
    bg = ((cfg.get("fb_post_search") or {}).get("background")) or {}
    return {
        "enabled": bg.get("enabled", True),
        "per_hour": max(8, min(200, int(bg.get("per_hour", 60) or 60))),
        "start_hour": int(bg.get("start_hour", 6)),
        "end_hour": int(bg.get("end_hour", 22)),
        "roundup_hour": int(bg.get("roundup_hour", 15)),
    }


def _state_get(conn, key: str, default=None):
    conn.execute("CREATE TABLE IF NOT EXISTS fb_posts_bg (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM fb_posts_bg WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _state_set(conn, key: str, value) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS fb_posts_bg (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO fb_posts_bg VALUES (?, ?)", (key, json.dumps(value)))
    conn.commit()


def state() -> dict:
    with _conn() as conn:
        return {k: _state_get(conn, k) for k in (
            "cursor", "priority_cursor", "pass_started", "passes_done", "today", "today_count",
            "today_hits", "last_term", "last_batch", "paused_until", "pause_reason", "total_terms")}


def all_terms(cfg: dict) -> list[str]:
    from scrapers.learning import keywords_with_learned
    seen, out = set(), []
    for t in keywords_with_learned(cfg):
        t = str(t).strip()
        if len(t) >= 3 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _local_now(cfg: dict) -> datetime:
    from zoneinfo import ZoneInfo
    tz = (cfg.get("schedule") or {}).get("timezone", "UTC")
    return datetime.now(ZoneInfo(tz))


def _term_stats(conn) -> dict[str, dict]:
    conn.execute("""CREATE TABLE IF NOT EXISTS fb_post_terms (
        term TEXT PRIMARY KEY, searches INTEGER DEFAULT 0, hits INTEGER DEFAULT 0,
        last_searched TEXT, last_hit TEXT)""")
    return {t.lower(): {"searches": s or 0, "hits": h or 0, "last_searched": ls, "last_hit": lh}
            for t, s, h, ls, lh in conn.execute(
                "SELECT term, searches, hits, last_searched, last_hit FROM fb_post_terms")}


def record_term(term: str, new_hits: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        _term_stats(conn)
        conn.execute(
            "INSERT INTO fb_post_terms (term, searches, hits, last_searched, last_hit) VALUES (?, 1, ?, ?, ?)"
            " ON CONFLICT(term) DO UPDATE SET searches = searches + 1, hits = hits + excluded.hits,"
            " last_searched = excluded.last_searched,"
            " last_hit = COALESCE(excluded.last_hit, fb_post_terms.last_hit)",
            (term.lower(), new_hits, now, now if new_hits else None))
        conn.commit()


def term_weight(st: Optional[dict]) -> float:
    """How often a term should come around, learned from its results:
    never searched first; terms that found for-sale posts in the last two
    weeks 3x as often; terms that found nothing in 4+ searches 4x less."""
    if not st or not st["searches"]:
        return 100.0
    if st["last_hit"]:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(st["last_hit"]) < timedelta(days=14):
                return 3.0
        except ValueError:
            pass
    if st["searches"] >= 4 and not st["hits"]:
        return 0.25
    return 1.0


def _next_jobs(cfg: dict, n: int) -> list[tuple[str, str]]:
    """[(category, term)] — a couple of priority terms, then the terms most
    "due": hours since last searched × learned weight (see term_weight)."""
    from scrapers.fb_posts import priority_terms
    prio = priority_terms(cfg)
    terms = all_terms(cfg)
    jobs = []
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        pc = _state_get(conn, "priority_cursor", 0) or 0
        for _ in range(min(PRIORITY_PER_BATCH, len(prio))):
            jobs.append(prio[pc % len(prio)])
            pc += 1
        _state_set(conn, "priority_cursor", pc % max(1, len(prio)))
        stats = _term_stats(conn)
        prio_lower = {t.lower() for _, t in jobs}

        def due(t: str) -> float:
            st = stats.get(t.lower())
            hours = 1000.0
            if st and st["last_searched"]:
                try:
                    hours = (now - datetime.fromisoformat(st["last_searched"])).total_seconds() / 3600
                except ValueError:
                    pass
            return hours * term_weight(st)

        for t in sorted((t for t in terms if t.lower() not in prio_lower), key=due, reverse=True):
            if len(jobs) >= n:
                break
            jobs.append(("All terms", t))
        covered = sum(1 for t in terms if (stats.get(t.lower()) or {}).get("searches"))
        _state_set(conn, "cursor", covered)
        _state_set(conn, "total_terms", len(terms))
    return jobs


def run_batch(cfg: dict) -> Optional[dict]:
    """One paced batch. Returns a summary, or None if it's not time to run."""
    from scrapers.facebook_scraper import SESSION_FILE, _browser, _new_context, _save_session
    from scrapers.fb_posts import search_term

    st = settings(cfg)
    if not st["enabled"] or not SESSION_FILE.exists():
        return None
    now_local = _local_now(cfg)
    if not (st["start_hour"] <= now_local.hour < st["end_hour"]):
        return None
    with _conn() as conn:
        paused = _state_get(conn, "paused_until")
        if paused and datetime.fromisoformat(paused) > datetime.now(timezone.utc):
            return None
        today = now_local.date().isoformat()
        if _state_get(conn, "today") != today:
            _state_set(conn, "today", today)
            _state_set(conn, "today_count", 0)
            _state_set(conn, "today_hits", 0)

    jobs = _next_jobs(cfg, max(1, st["per_hour"] * BATCH_EVERY_MINUTES // 60))
    done = hits = 0
    blocked = None
    started = time.time()
    try:
        with _browser() as browser:
            context = _new_context(browser)
            page = context.new_page()
            for category, term in jobs:
                posts = search_term(page, term, scrolls=3)
                body = ""
                try:
                    body = page.inner_text("body")[:3000]
                except Exception:
                    pass
                if _BLOCKED.search(body) or "checkpoint" in page.url or "/login" in page.url:
                    blocked = "Facebook asked to slow down or confirm the account"
                    break
                term_new = 0
                for p in posts:
                    status = mark_seen(Listing(
                        source_name=f"FB Posts — {category}", title=p["title"], url=p["url"],
                        price=p["price"], description=p["description"], image_url=p["image"],
                        listing_id=p["id"], posted_at=p["posted_at"]))
                    if status == "new":
                        hits += 1
                        term_new += 1
                record_term(term, term_new)
                done += 1
                with _conn() as conn:
                    _state_set(conn, "last_term", term)
                # Human-ish pacing between searches.
                time.sleep(random.uniform(3, 7))
            _save_session(context)
            context.close()
    except Exception:
        logger.exception("Background FB post search batch failed")

    with _conn() as conn:
        _state_set(conn, "today_count", (_state_get(conn, "today_count", 0) or 0) + done)
        _state_set(conn, "today_hits", (_state_get(conn, "today_hits", 0) or 0) + hits)
        _state_set(conn, "last_batch", datetime.now(timezone.utc).isoformat())
        if blocked:
            until = datetime.now(timezone.utc) + timedelta(hours=BLOCK_PAUSE_HOURS)
            _state_set(conn, "paused_until", until.isoformat())
            _state_set(conn, "pause_reason", blocked)
            # Don't skip the term it stopped on.
            _state_set(conn, "cursor", max(0, (_state_get(conn, "cursor", 0) or 0) - (len(jobs) - done)))
    if blocked:
        logger.warning("FB post search paused %dh: %s", BLOCK_PAUSE_HOURS, blocked)
        try:
            from scrapers.notify import push_enabled, send_push
            if push_enabled(cfg):
                send_push(cfg, "Gear Scout paused Facebook post search",
                          f"{blocked}. It will try again in {BLOCK_PAUSE_HOURS} hours on its own.",
                          tags=["warning"])
        except Exception:
            logger.exception("Couldn't send pause notification")
    logger.info("FB post search batch: %d searches, %d new posts in %.0fs", done, hits, time.time() - started)
    return {"searched": done, "new": hits, "blocked": blocked}


# ---------------------------------------------------------------------------
# Afternoon roundup email
# ---------------------------------------------------------------------------

def roundup_rows(hours: int = 24) -> list[dict]:
    import sqlite3
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM seen WHERE source_name LIKE 'FB Posts%' AND first_seen >= ?
               AND COALESCE(hidden, 0) = 0 AND COALESCE(duplicate, 0) = 0
               ORDER BY posted_at DESC""", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def build_roundup_html(rows: list[dict], dashboard_url: str, no_comp: int = 0) -> str:
    from html import escape
    cards = []
    for r in rows:
        img = (f'<img src="{escape(r["image_url"])}" alt="" width="72" height="72" '
               f'style="width:72px;height:72px;object-fit:cover;border-radius:6px;flex-shrink:0;">'
               if r.get("image_url") else "")
        price = (f'<span style="display:inline-block;margin-top:4px;padding:2px 10px;background:#dcfce7;'
                 f'color:#166534;border-radius:12px;font-size:13px;font-weight:600;">{escape(r["price"])}</span>'
                 if r.get("price") else "")
        from scrapers.timefmt import local_time
        when = local_time(r.get("posted_at"))
        comp = r.get("comp")
        if comp:
            price += (f'<span style="font-size:11px;color:#166534;font-weight:600;">&nbsp;{escape(comp["label"])} '
                      f'~${comp["ref"]:,.0f}{" (est.)" if comp.get("est") else ""} · {comp["pct"]}% under</span>')
        cards.append(f"""
        <div style="display:flex;gap:12px;padding:12px;margin-bottom:8px;background:#fff;
             border:1px solid #e2e8f0;border-radius:10px;align-items:flex-start;">
          {img}
          <div style="flex:1;min-width:0;">
            <a href="{escape(r['url'])}" style="font-size:15px;font-weight:600;color:#1e3a5f;text-decoration:none;">
              {escape(r['title'] or 'Facebook post')}</a><br>
            {price}
            <span style="font-size:11px;color:#94a3b8;">&nbsp;posted {escape(when)}</span>
            <p style="margin:6px 0 0;font-size:13px;color:#64748b;line-height:1.4;">{escape(r.get('description') or '')}</p>
          </div>
        </div>""")
    return f"""<html><body style="margin:0;padding:16px;background:#f1f5f9;font-family:-apple-system,Segoe UI,Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;">
        <h2 style="color:#1e3a5f;margin:0 0 4px;">Facebook posts roundup</h2>
        <p style="color:#64748b;margin:0 0 14px;font-size:14px;">
          {len(rows)} for-sale post{'s' if len(rows) != 1 else ''} found in the last 24 hours while searching
          your whole term list in the background — only ones at least 10% under their comp. Every post here
          is from the last 14 days.{f" {no_comp} more had no price or nothing to compare against (see FB Posts)." if no_comp else ""}</p>
        <p style="margin:0 0 16px;"><a href="{escape(dashboard_url)}/fb-posts"
           style="display:inline-block;padding:10px 16px;background:#1e3a5f;color:#fff;border-radius:8px;
           text-decoration:none;font-weight:600;">Open FB Posts in Gear Scout →</a></p>
        {''.join(cards) or '<p style="color:#64748b;">Nothing new today.</p>'}
      </div></body></html>"""


def send_roundup(cfg: dict) -> bool:
    from scrapers.emailer import send_html_email
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.market import load_market
    all_rows = roundup_rows()
    st = state()
    # Same rule as everywhere: only posts 10%+ under their comp.
    market, index, similar = load_market(), price_index(), similar_index()
    rows, no_comp = [], 0
    for r in all_rows:
        c = evaluate(r.get("title"), r.get("price"), r.get("description"), r.get("url"), market, index, similar)
        if not c:
            no_comp += 1
        elif c["worth"]:
            r["comp"] = c
            rows.append(r)
    if not rows:
        logger.info("FB posts roundup: %d posts, none 10%%+ under a comp — no email.", len(all_rows))
        return False
    email_cfg = cfg["email"]
    html = build_roundup_html(rows, email_cfg.get("dashboard_url", "http://localhost:8420"), no_comp)
    ok = send_html_email(html, f"Gear Scout — Facebook posts roundup: {len(rows)} for sale "
                               f"({st.get('today_count') or 0} searches today)", email_cfg)
    logger.info("FB posts roundup email %s (%d posts)", "sent" if ok else "FAILED", len(rows))
    return ok
