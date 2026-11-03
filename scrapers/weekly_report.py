"""Sunday buyer's report: a plain-English email summing up the week for the mics
you hunt. New mics seen, the best deals found (50%+ under used value), price
drops, what sold, and the market pulse headline.

build_report(cfg) -> (subject, html, text). send_weekly(cfg) emails it (same
sender and config as the 8 AM best-deals email). Preview without sending:

    python -m scrapers.weekly_report
"""
import logging
import sys
from datetime import datetime, timedelta, timezone
from html import escape

logger = logging.getLogger(__name__)

DAYS = 7
MIN_DEAL_PCT = 50   # "best deals": at least this far under used value
TOP_DEALS = 8
TOP_LIST = 8


def _ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _week_rows(cfg: dict, now: datetime):
    """The week's mic listings from the seen table, split into new / dropped / sold.
    Each item: {title, url, name, price (float), old (float|None), source}."""
    from scrapers import mic_focus
    from scrapers.enrich import parse_price
    from scrapers.market_pulse import MIN_ASK
    from scrapers.store import _conn
    targets = [t for t in mic_focus.settings(cfg)["targets"] if t["on"] and not t.get("outboard")]
    floors = {t["key"]: max(t.get("min_price") or 0, MIN_ASK) for t in targets}
    cut = (now - timedelta(days=DAYS)).isoformat()
    new, dropped, sold = [], [], []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT title, price, url, source_name, first_seen, previous_price, price_dropped_at, sold, sold_at"
            " FROM seen WHERE duplicate = 0 AND price IS NOT NULL AND price NOT LIKE 'C$%'"
            " AND (country IS NULL OR country = 'US')"
            " AND (first_seen >= ? OR price_dropped_at >= ? OR sold_at >= ?)", (cut, cut, cut)).fetchall()
    for title, price, url, src, first, prev, dropped_at, is_sold, sold_at in rows:
        t = mic_focus.classify(title or "", targets)
        v = parse_price(price)
        if not t or not v or v < floors[t["key"]]:
            continue
        item = {"title": title, "url": url, "name": t["name"], "price": v, "old": parse_price(prev) if prev else None,
                "source": src or ""}
        if first and first >= cut:
            new.append(item)
        if dropped_at and dropped_at >= cut and item["old"] and item["old"] > v:
            dropped.append(item)
        if is_sold and sold_at and sold_at >= cut:
            sold.append(item)
    return new, dropped, sold


def _week_deals(now: datetime) -> list:
    """Deals first seen this week at MIN_DEAL_PCT+ under used value, best first."""
    from scrapers import mic_focus
    cut = (now - timedelta(days=DAYS)).isoformat()
    data = mic_focus.load()
    pool = [r for r in (data.get("deals") or []) if not r.get("warn")] + \
           [r for r in (data.get("rows") or []) if (r.get("mic") or {}).get("hit") and not r.get("warn")]
    seen, out = set(), []
    for r in pool:
        info = r.get("deal") or r.get("mic") or {}
        pct = info.get("pct") or 0
        if pct < MIN_DEAL_PCT or (r.get("first_seen") or "") < cut or r.get("url") in seen:
            continue
        seen.add(r.get("url"))
        out.append({"title": r.get("title") or "", "url": r.get("url") or "", "pct": pct,
                    "price": info.get("delivered"), "used": info.get("used"), "source": r.get("source_name") or "",
                    "name": (r.get("mic") or {}).get("name") or ""})
    out.sort(key=lambda d: -d["pct"])
    return out


def _pulse_line(cfg: dict) -> str:
    """One sentence on the market, from the market pulse trends."""
    try:
        from scrapers import market_pulse
        rows = market_pulse.pulse(cfg)
    except Exception:
        logger.exception("Weekly report: pulse unavailable")
        return ""
    ups = [r for r in rows if (r.get("trend") or {}).get("dir") == "up"]
    downs = [r for r in rows if (r.get("trend") or {}).get("dir") == "down"]
    parts = []
    if downs:
        d = min(downs, key=lambda r: r["trend"]["pct"])
        parts.append(f"{d['name']} asking prices are down {abs(d['trend']['pct'])}% over the last 4 weeks "
                     f"(median ${d['trend']['now']:,} vs ${d['trend']['before']:,})")
    if ups:
        u = max(ups, key=lambda r: r["trend"]["pct"])
        parts.append(f"{u['name']} is up {u['trend']['pct']}% (median ${u['trend']['now']:,} vs ${u['trend']['before']:,})")
    if not parts:
        busy = rows[0] if rows else None
        if busy:
            return f"The market looks steady. {busy['name']} is the busiest mic with {busy['total']} listings in 8 weeks."
        return ""
    return "Market pulse: " + "; ".join(parts) + "."


def _money(v) -> str:
    return f"${v:,.0f}" if v is not None else "?"


def build_report(cfg: dict, now: datetime = None):
    """-> (subject, html, text)."""
    now = now or datetime.now(timezone.utc)
    new, dropped, sold = _week_rows(cfg, now)
    deals = _week_deals(now)
    pulse = _pulse_line(cfg)

    by_mic: dict = {}
    for i in new:
        by_mic[i["name"]] = by_mic.get(i["name"], 0) + 1
    top_mics = sorted(by_mic.items(), key=lambda kv: -kv[1])
    dropped.sort(key=lambda i: -((i["old"] - i["price"]) / i["old"]))
    sold.sort(key=lambda i: -i["price"])

    subject = f"Gear Scout weekly: {len(new)} new mic{'s' if len(new) != 1 else ''}, {len(deals)} great deal{'s' if len(deals) != 1 else ''}"
    # sections: (heading, [(label, url, detail)], plain sentence when empty)
    sections = []
    summary = f"This week I saw {len(new)} new mic listing{'s' if len(new) != 1 else ''}"
    if top_mics:
        summary += " (" + ", ".join(f"{n} {name}" for name, n in top_mics[:5]) + ")"
    summary += f". {len(dropped)} dropped their price and {len(sold)} sold."
    sections.append(("Best deals (%d%%+ under used value)" % MIN_DEAL_PCT,
                     [(d["title"][:90], d["url"], f"{_money(d['price'])} delivered, {d['pct']}% under used ({_money(d['used'])}) - {d['source']}")
                      for d in deals[:TOP_DEALS]], "No 50%+ deals turned up this week."))
    sections.append(("Price drops",
                     [(i["title"][:90], i["url"], f"{_money(i['old'])} to {_money(i['price'])} - {i['source']}") for i in dropped[:TOP_LIST]],
                     "No price drops this week."))
    sections.append(("Sold this week",
                     [(i["title"][:90], i["url"], f"{_money(i['price'])} - {i['source']}") for i in sold[:TOP_LIST]],
                     "Nothing was marked sold this week."))

    text = [summary, ""]
    body = f'<p style="font-size:15px;color:#161512">{escape(summary)}</p>'
    if pulse:
        text += [pulse, ""]
        body += f'<p style="font-size:14px;color:#334155">{escape(pulse)}</p>'
    for head, items, empty in sections:
        text.append(head.upper())
        body += f'<h3 style="margin:18px 0 6px;font-size:15px;color:#1e3a5f">{escape(head)}</h3>'
        if not items:
            text.append("  " + empty)
            body += f'<p style="color:#64748b;margin:0">{escape(empty)}</p>'
        for label, url, detail in items:
            text.append(f"  - {label}: {detail}\n    {url}")
            body += (f'<div style="padding:5px 0"><a href="{escape(url)}" style="color:#1e3a5f;font-weight:700;text-decoration:none">'
                     f'{escape(label)}</a><div style="font-size:12px;color:#64748b">{escape(detail)}</div></div>')
        text.append("")

    dash = ((cfg.get("email") or {}).get("dashboard_url") or "").rstrip("/")
    link = f'<p><a href="{escape(dash)}/mics">Open Gear Scout</a></p>' if dash else ""
    html = ('<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,Segoe UI,sans-serif">'
            '<div style="max-width:620px;margin:0 auto;padding:20px"><div style="background:#161512;color:#efe6d2;padding:16px 20px;'
            f'border-radius:12px 12px 0 0;font-size:18px;font-weight:700">{escape(subject)}</div>'
            f'<div style="background:#fff;padding:8px 20px 16px;border-radius:0 0 12px 12px">{body}{link}</div></div></body></html>')
    return subject, html, "\n".join(text)


def send_weekly(cfg: dict) -> bool:
    """Sunday morning email; same sender/config as the 8 AM best-deals email."""
    email_cfg = cfg.get("email") or {}
    if not (email_cfg.get("from") and email_cfg.get("password")):
        return False
    from scrapers.emailer import send_html_email
    subject, html, _ = build_report(cfg)
    try:
        return send_html_email(html, subject, email_cfg)
    except Exception:
        logger.exception("Weekly report email failed")
        return False


def _load_cfg() -> dict:
    import yaml
    from pathlib import Path
    for p in (Path("/config/config.yaml"), Path("/app/config.yaml"), Path("config.yaml")):
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    return {}


if __name__ == "__main__":
    # Preview only: prints the report, never sends.
    subj, _html, txt = build_report(_load_cfg())
    sys.stdout.write(subj + "\n\n" + txt + "\n")
