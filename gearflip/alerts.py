"""Phone pushes and the morning email, each carrying the full numbers.

A push goes out for a NEW strong flip (score and profit over your alert
lines, posted in the last FRESH_DAYS days, affordable with your bankroll),
once per ad, again only if its price drops 10%+. More than a few at once
become one summary push."""
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from gearflip import settings as S, source

logger = logging.getLogger(__name__)

FRESH_DAYS = 3
KEEP_DAYS = 30
REPUSH_DROP = 0.10


def _path():
    return S.data_dir() / "alerted.json"


def push_text(o: dict, rank: Optional[int] = None, plan_n: int = 0, cash: Optional[float] = None) -> tuple:
    """(title, message) for one opportunity."""
    m = o["m"]
    title = f"💰 Flip +${m['expected']:,} ({m['roi']:.0f}% ROI) · {o['title'][:60]}"
    lines = [f"Buy ${m['ask']:,} on {o['source'] or 'site'}" + (f" ({o['location']})" if o.get("location") else "")
             + (f" → ${m['buy_total']:,} all-in" if m["buy_total"] != m["ask"] else "")]
    if m["repair"]:
        lines.append(f"Needs: {m['repair']} ~${m['repair_cost']:,} ({m['repair_odds']}% odds it works; "
                     f"if not: ${m['downside']:,})")
    lines.append(f"Sell ~${m['resale']:,} on {m['channel_label']}")
    lines.append(f"Fees ${m['fees']:,} · Ship ${m['ship_out']:,}" + (f" · Tax ${m['tax']:,}" if m["tax"] else ""))
    lines.append(f"Net profit ~${m['expected']:,} · ROI {m['roi']:.0f}% · ~{m['days']} days to sell")
    lines.append(f"Confidence {m['confidence']}/100 ({m['comps']} comps{', real sales' if m['sold_basis'] else ''})"
                 f" · Score {m['score']}")
    lines.append(f"Walk-away price: ${m['walk_away']:,}")
    p = o.get("photo")
    if p:
        bits = [f"Photo: {p['condition']}" + (" · real unit" if p["is_item"] == "yes" and not p["stock"] else "")]
        if p.get("seen_model"):
            bits.append(f"reads {p['seen_model']}")
        if p.get("damage"):
            bits.append("damage: " + ", ".join(p["damage"][:2]))
        if p.get("missing"):
            bits.append("missing: " + ", ".join(p["missing"][:2]))
        lines.append(" · ".join(bits))
    if rank:
        lines.append(f"In your best-buys plan: #{rank} of {plan_n}" + (f" (${cash:,.0f} free)" if cash is not None else ""))
    return title, "\n".join(lines)


def _due(opps: list[dict], sent: dict, st: dict, cash_cap: float, now: datetime, photo_ai: bool = False) -> list[dict]:
    out = []
    for o in opps:
        m = o["m"]
        if m["score"] < st["alert_min_score"] or m["expected"] < st["alert_min_profit"] or m["cash_need"] > cash_cap:
            continue
        if o.get("age_days") is None or o["age_days"] > FRESH_DAYS:
            continue
        if photo_ai and not o.get("photo"):
            continue  # wait for the picture check before pushing
        prev = sent.get(o["url"])
        if prev and m["ask"] > prev.get("ask", 0) * (1 - REPUSH_DROP):
            continue
        out.append(o)
    return out


def send(cfg: dict, res: dict, plan: dict, st: dict, now: Optional[datetime] = None) -> dict:
    """Pushes what's new and strong. Returns {"pushed", "due"}."""
    now = now or datetime.now(timezone.utc)
    sent = S.read_json(_path(), {}) or {}
    cut = (now - timedelta(days=KEEP_DAYS)).isoformat()
    sent = {u: v for u, v in sent.items() if (v.get("at") or "") >= cut}
    out = {"pushed": 0, "due": 0}
    if not st.get("alerts") or not source.push_ready(cfg, st.get("ntfy_topic", "")):
        return out
    cash_cap = max(plan.get("cash", 0), 0) or st["budget"]
    due = _due(res.get("opps") or [], sent, st, cash_cap, now, bool(res.get("photo_ai")))
    out["due"] = len(due)
    if not due:
        S.write_json(_path(), sent)
        return out
    page = source.dashboard_url(cfg) + "/gearflip"
    n_plan = len(plan.get("items") or [])
    limit = int(st.get("max_pushes_per_scan", 3))
    for o in due[:limit]:
        title, msg = push_text(o, (plan.get("rank") or {}).get(o["url"]), n_plan, plan.get("cash"))
        ok = source.push(cfg, st.get("ntfy_topic", ""), title, msg, url=o["url"], image=o.get("image"),
                         actions=[{"action": "view", "label": "Gear Flip", "url": page}])
        if ok:
            out["pushed"] += 1
    rest = due[limit:]
    if rest:
        best = rest[0]["m"]
        msg = "\n".join(f"+${o['m']['expected']:,} ({o['m']['roi']:.0f}%) · ${o['m']['ask']:,} · {o['title'][:50]}"
                        for o in rest[:8])
        if source.push(cfg, st.get("ntfy_topic", ""), f"💰 {len(rest)} more flips (best +${best['expected']:,})", msg,
                       url=page):
            out["pushed"] += 1
    for o in due:
        sent[o["url"]] = {"at": now.isoformat(), "ask": o["m"]["ask"]}
    S.write_json(_path(), sent)
    return out


# ---------------------------------------------------------------- email
def _money(v) -> str:
    return f"${v:,.0f}"


def email_html(res: dict, plan: dict, book: dict, page: str) -> str:
    t = plan["t"]
    e = html.escape
    rows = []
    for o in plan["items"]:
        m = o["m"]
        rows.append(
            f"<tr><td style='padding:6px;border-top:1px solid #ddd'><a href='{e(o['url'] or '')}'>{e(o['title'][:70])}</a>"
            f"<br><small>{e(o['source'] or '')}{' · needs ' + e(m['repair']) if m['repair'] else ''}</small></td>"
            f"<td style='padding:6px;border-top:1px solid #ddd'>{_money(m['buy_total'])}</td>"
            f"<td style='padding:6px;border-top:1px solid #ddd'>{_money(m['resale'])} ({e(m['channel_label'])})</td>"
            f"<td style='padding:6px;border-top:1px solid #ddd'><b>{_money(m['expected'])}</b> · {m['roi']:.0f}%</td>"
            f"<td style='padding:6px;border-top:1px solid #ddd'>{m['days']}d · {m['confidence']}/100</td></tr>")
    table = ("<table style='border-collapse:collapse;font-size:14px'><tr><th align=left>Buy this</th><th align=left>All-in</th>"
             "<th align=left>Sell for</th><th align=left>Profit · ROI</th><th align=left>Days · Confidence</th></tr>"
             + "".join(rows) + "</table>") if rows else "<p>Nothing clears your minimums right now.</p>"
    return (f"<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:760px'>"
            f"<h2>💰 Gear Flip: your best buys today</h2>"
            f"<p>Bankroll {_money(book['value'])} · cash free {_money(plan['cash'])} · "
            f"{book['open']} flips waiting to sell · {_money(book['realized'])} profit so far.</p>"
            f"<p><b>The plan:</b> {t['n']} buys for {_money(t['cash_in'])} → about {_money(t['expected'])} profit "
            f"({t['roi']:.0f}% ROI) in about {t['days']} days. Worst realistic case: {_money(t['downside'])}.</p>"
            f"{table}<p style='color:#666;font-size:12px'>{res.get('found', 0)} flips found in {res.get('rows', 0)} live ads. "
            f"Estimates only: check photos, ask the seller, confirm it works.</p>"
            f"<p><a href='{e(page)}'>Open Gear Flip →</a></p></div>")


def send_digest(cfg: dict, res: dict, plan: dict, book: dict) -> bool:
    page = source.dashboard_url(cfg) + "/gearflip"
    t = plan["t"]
    subject = f"Gear Flip: {t['n']} buys, ~${t['expected']:,.0f} profit ({t['roi']:.0f}% ROI)"
    return source.email(cfg, email_html(res, plan, book, page), subject)
