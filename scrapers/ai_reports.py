"""
AI emails (Gemini, your own free key):
  - Daily briefing, 8 AM: the 5 best finds of the last 24 hours and why.
  - Weekly market notes, Sundays 9 AM: what's selling fast, price moves on
    the gear you track, and suggestions.
Both also send a short phone push when push notifications are set up.
"""
import json
import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from html import escape

logger = logging.getLogger(__name__)


def _key(cfg: dict) -> str:
    return ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()


def _send(cfg: dict, subject: str, html: str, push_title: str, push_text: str):
    from scrapers.emailer import send_html_email
    from scrapers.notify import push_enabled, send_push
    email_cfg = cfg.get("email") or {}
    if email_cfg.get("from") and email_cfg.get("password"):
        send_html_email(html, subject, email_cfg)
    if push_enabled(cfg):
        send_push(cfg, push_title, push_text[:900], url=email_cfg.get("dashboard_url"), tags=["robot"])


def _generate_patiently(key: str, prompt: str, json_mode: bool = False) -> str:
    """Scheduled emails can wait: when Gemini is overloaded, try again a few
    times over ~6 minutes instead of skipping the day."""
    import time
    from scrapers import assistant
    for attempt in range(4):
        try:
            return assistant._generate(key, prompt, json_mode=json_mode)
        except RuntimeError:
            if attempt == 3:
                raise
            time.sleep(120)
            assistant._busy.clear()


def _page(title: str, body: str, dashboard_url: str) -> str:
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="max-width:640px;margin:0 auto;padding:20px;">
        <div style="background:#1e3a5f;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">{escape(title)}</div>
        <div style="background:#fff;padding:18px 22px;border-radius:0 0 12px 12px;line-height:1.55;color:#1e293b;">{body}
          <p style="margin-top:18px;"><a href="{escape(dashboard_url)}" style="color:#1e3a5f;font-weight:600;">Open Gear Scout →</a></p>
        </div></div></body></html>"""


BRIEFING_PROMPT = """You are Gear Scout's assistant for a used pro-audio / studio gear buyer who loves
vintage and boutique mics, preamps and outboard. From the listings below (all found in the last 24 hours,
all priced under their comp), pick the 5 best finds — best value, most desirable gear, most likely to be
gone soon. Reply with JSON only:
{{"intro": "one friendly sentence", "picks": [{{"n": listing number, "why": "1-2 sentences"}}, ...]}}

Listings:
{rows}"""


def daily_briefing(cfg: dict) -> bool:
    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import is_accessory_only, is_not_audio, item_form
    from scrapers.learning import taste
    from scrapers.market import load_market
    from scrapers.store import _conn
    key = _key(cfg)
    if not key:
        return False
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM seen WHERE first_seen >= ? AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
            " AND COALESCE(live_only, 0) = 0 AND price IS NOT NULL", (since,))]
    market, index, similar, t = load_market(), price_index(), similar_index(), taste()
    cands = []
    for r in rows:
        title = r["title"] or ""
        if is_not_audio(title) or is_accessory_only(title) or item_form(title) == "pedal" \
                or (r.get("description") or "").startswith("Auction"):
            continue
        c = evaluate(title, r["price"], r.get("description"), r["url"], market, index, similar)
        if c and c["worth"]:
            r["comp"] = c
            cands.append((c["pct"] * (1 + max(0.0, min(t.score(title), 6.0)) / 3), r))
    cands = [r for _, r in sorted(cands, key=lambda x: -x[0])[:30]]
    if not cands:
        logger.info("Daily briefing: nothing qualifying in the last 24 hours")
        return False
    lines = [f"#{i} | {r['title']} | {r['price']} | comp {r['comp']['label']} ~${r['comp']['ref']:,.0f} "
             f"({r['comp']['pct']}% under) | {(r['source_name'] or '').split(' — ')[0]} | "
             f"{(r.get('location') or '').split(' @')[0]}" for i, r in enumerate(cands, 1)]
    try:
        out = json.loads(_generate_patiently(key, BRIEFING_PROMPT.format(rows="\n".join(lines)), json_mode=True))
    except Exception:
        logger.exception("Daily briefing: Gemini failed")
        return False
    picks = [(cands[p["n"] - 1], p.get("why", "")) for p in out.get("picks", [])
             if isinstance(p.get("n"), int) and 1 <= p["n"] <= len(cands)][:5]
    if not picks:
        return False
    dash = (cfg.get("email") or {}).get("dashboard_url", "http://localhost:8420")
    body = f"<p>{escape(out.get('intro', ''))}</p>"
    for r, why in picks:
        img = (f'<img src="{escape(r["image_url"])}" width="88" height="88" style="width:88px;height:88px;'
               f'object-fit:cover;border-radius:8px;float:left;margin:0 12px 6px 0;">') if r.get("image_url") else ""
        body += (f'<div style="overflow:hidden;border-top:1px solid #e2e8f0;padding:12px 0;">{img}'
                 f'<a href="{escape(r["url"])}" style="color:#1e3a5f;font-weight:700;text-decoration:none;">{escape(r["title"])}</a><br>'
                 f'<span style="color:#166534;font-weight:700;">{escape(r["price"])}</span>'
                 f'<span style="color:#64748b;font-size:13px;"> · {escape(r["comp"]["label"])} ~${r["comp"]["ref"]:,.0f} · '
                 f'{r["comp"]["pct"]}% under · {escape((r["source_name"] or "").split(" — ")[0])}</span>'
                 f'<p style="margin:6px 0 0;font-size:14px;">{escape(why)}</p></div>')
    _send(cfg, f"Gear Scout — today's {len(picks)} best finds", _page("🤖 Today's best finds", body, dash),
          "Today's best finds", "\n".join(f"• {r['title'][:60]} — {r['price']}" for r, _ in picks))
    logger.info("Daily briefing sent (%d picks)", len(picks))
    return True


WEEKLY_PROMPT = """You are Gear Scout's market analyst for a used pro-audio / studio gear buyer.
Write short, practical weekly market notes from the data below: what's selling fast (act quickly on
those), price moves on the gear they track (up/down vs. last week), anything notable, and 2-4 models
worth adding to their watch list given their taste. Plain language, short paragraphs or bullets
(use "- " for bullets). No more than ~250 words. Don't invent numbers that aren't in the data.

{data}"""


def weekly_notes(cfg: dict) -> bool:
    from scrapers.enrich import is_accessory_only, is_not_audio, item_form, parse_price
    from scrapers.learning import fast_sellers, taste
    from scrapers.store import _conn
    from scrapers.telex import term_matcher, terms
    from scrapers.base import fix_brand_spelling
    key = _key(cfg)
    if not key:
        return False
    now = datetime.now(timezone.utc)
    wk, prev = (now - timedelta(days=7)).isoformat(), (now - timedelta(days=14)).isoformat()
    with _conn() as conn:
        rows = conn.execute("SELECT title, price, first_seen FROM seen WHERE first_seen >= ? AND duplicate = 0"
                            " AND price IS NOT NULL", (prev,)).fetchall()
    texts = [(fix_brand_spelling((t or "").lower()), parse_price(p), f) for t, p, f in rows
             if not (is_not_audio(t or "") or is_accessory_only(t or "") or item_form(t or "") in ("pedal", "plugin"))]
    moves = []
    for term in terms(cfg)[:40]:
        m = term_matcher(term)
        this = [v for t, v, f in texts if v and v >= 20 and f >= wk and m(t)]
        last = [v for t, v, f in texts if v and v >= 20 and f < wk and m(t)]
        if len(this) >= 3 and len(last) >= 3:
            a, b = statistics.median(this), statistics.median(last)
            moves.append(f"{term}: median listed ${a:,.0f} this week vs ${b:,.0f} last week ({len(this)} vs {len(last)} listings)")
        elif this:
            moves.append(f"{term}: {len(this)} listed this week, median ${statistics.median(this):,.0f}")
    fast = [f"{k.replace(':', ' ').replace('-', ' ')}: sells in ~{v['days']:.1f} days at ~${v['price']:,.0f} ({v['n']} seen)"
            for k, v in fast_sellers(15)]
    likes = ", ".join(w for w, _ in taste().top(15))
    data = ("Tracked gear (Telex List) this week:\n" + ("\n".join(moves) or "(not enough data yet)") +
            "\n\nFast sellers Gear Scout watched sell:\n" + ("\n".join(fast) or "(none yet)") +
            f"\n\nWhat the buyer favorites (taste words): {likes or '(not enough favorites yet)'}"
            f"\n\nNew listings collected this week: {sum(1 for _, _, f in texts if f >= wk)}")
    try:
        notes = _generate_patiently(key, WEEKLY_PROMPT.format(data=data))
    except Exception:
        logger.exception("Weekly notes: Gemini failed")
        return False
    html_notes = ""
    for line in escape(notes).split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if line.lstrip().startswith("#"):
            html_notes += f"<h3 style='margin:14px 0 4px;color:#1e3a5f;'>{line.lstrip('# ')}</h3>"
            continue
        if line.strip().startswith("- "):
            html_notes += f"<p style='margin:4px 0 4px 12px;'>• {line.strip()[2:]}</p>"
        elif line.strip():
            html_notes += f"<p>{line}</p>"
    dash = (cfg.get("email") or {}).get("dashboard_url", "http://localhost:8420")
    _send(cfg, "Gear Scout — weekly market notes", _page("📈 Weekly market notes", html_notes, dash),
          "Weekly market notes", re.sub(r"\*\*|^#+ *", "", notes, flags=re.M)[:400])
    logger.info("Weekly market notes sent")
    return True
