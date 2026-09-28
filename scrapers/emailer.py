"""
Builds and sends the HTML email digest.
"""
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from .base import Listing, ScrapeResult
from .enrich import needs_repair, price_context

logger = logging.getLogger(__name__)


def _source_status_color(result: ScrapeResult) -> str:
    if result.success and result.listings:
        return "#22c55e"   # green
    if result.success and not result.listings:
        return "#94a3b8"   # slate — ok but nothing matched
    if result.blocked:
        return "#1d4ed8"   # blue — expected, has a manual link, not a bug
    return "#ef4444"       # red — failed


def build_email_html(
    new_listings: list[Listing],
    results: list[ScrapeResult],
    run_time: datetime,
    run_number: int,
    max_listings_per_source: int = 8,
    max_total_listings: int = 40,
    dashboard_url: str = "http://localhost:8420",
    price_index: Optional[dict] = None,
    market_index: Optional[dict] = None,
    price_drops: Optional[list[dict]] = None,
) -> str:
    """
    Build a full HTML email.
    - Top section: new listings (only sent when there are some)
    - Bottom section: source status report (always included)
    """

    # --- Source status tiles ---
    # Blocked sources (Guitar Center/Sweetwater/eBay behind Akamai) aren't
    # bugs needing attention — they're expected and have a working manual
    # link — so they're excluded from the "failed" alert/tally below.
    failed_sources = [r for r in results if not r.success and not r.blocked]
    ok_sources = [r for r in results if r.success or r.blocked]

    # Back to one row per source (not the 3-per-row tile grid) — each row
    # stays a single compact line (icon, name, count, duration) with any
    # error/fix-hint detail tucked into a native <details> disclosure, closed
    # by default, so the list reads as a minimized summary you expand only
    # when you actually want more. <details>/<summary> is widely supported
    # in Gmail; clients that don't support it just show the detail expanded,
    # which degrades safely rather than hiding anything.
    #
    # Only blocked/failed rows link the source name to its raw page — that
    # link is the deliberate "check this yourself" fallback for sources the
    # app can't scrape or search. A successful source's name used to link
    # there too, but that's the same unfiltered browse URL, and sitting next
    # to "X new" it read like a shortcut to those matches, which it never
    # was — the real matches are the listing cards above.
    status_rows = ""
    for r in results:
        color = _source_status_color(r)
        icon = "✓" if r.success else ("🔗" if r.blocked else "✗")
        if r.success:
            count = f"{len(r.listings)} new"
        elif r.blocked:
            count = "manual check only"
        else:
            count = "FAILED"
        duration = f"{r.duration_seconds:.1f}s"

        if r.success:
            name_html = f'<span style="color:#1e293b;">{r.source_name}</span>'
        else:
            name_html = f'<a href="{r.source_url}" style="color:#1e40af;text-decoration:none;">{r.source_name}</a>'

        fix_html = ""
        if not r.success and r.fix_hint:
            manual_link = ""
            if r.source_url:
                manual_link = f"""
                <div style="margin-top:8px;">
                  <a href="{r.source_url}" target="_blank" rel="noopener"
                     style="display:inline-block;padding:5px 12px;background:#1e3a5f;
                         color:#ffffff;text-decoration:none;border-radius:6px;
                         font-size:11px;font-weight:600;">
                    Open in browser &rarr;
                  </a>
                </div>"""
            box_bg, box_border, box_text = (
                ("#eff6ff", "#bfdbfe", "#1d4ed8") if r.blocked
                else ("#fffbeb", "#fde68a", "#b45309")
            )
            label = "Heads up:" if r.blocked else "How to fix:"
            fix_html = f"""
            <div style="margin-top:8px;padding:8px 10px;font-size:11px;
                color:{box_text};background:{box_bg};border:1px solid {box_border};
                border-radius:6px;">
              <strong>{label}</strong> {r.fix_hint}
              {manual_link}
            </div>"""
        error_html = ""
        if not r.success and not r.blocked and r.error:
            error_html = f'<div style="margin-top:6px;font-size:11px;color:#ef4444;">{r.error}</div>'

        detail_inner = error_html + fix_html
        detail_html = ""
        if detail_inner:
            detail_html = f"""
            <details style="margin-top:4px;">
              <summary style="cursor:pointer;font-size:11px;color:#1e40af;">Details</summary>
              {detail_inner}
            </details>"""

        status_rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;">
            <table style="width:100%;border-collapse:collapse;">
              <tr>
                <td style="font-size:13px;font-weight:600;">
                  <span style="color:{color};">{icon}</span> {name_html}
                </td>
                <td style="text-align:right;font-size:12px;color:#64748b;white-space:nowrap;">
                  {count} &middot; {duration}
                </td>
              </tr>
            </table>
            {detail_html}
          </td>
        </tr>"""

    # --- Listing cards ---
    # Capped both per-source AND overall — with enough sources actually
    # finding real matches, an uncapped email can easily exceed Gmail's
    # ~102KB clipping threshold (confirmed: 148 listings across 25 sources
    # produced a 213KB email; even an 8-per-source cap alone still hit
    # 194KB with 23 active sources — the per-source cap isn't enough on its
    # own once enough sources are each contributing). Gmail truncates
    # mid-HTML past its limit, breaking the layout for everything after
    # the cut. The dashboard has no such limit and always shows everything,
    # so it's the actual place to see the rest.
    MAX_LISTINGS_PER_SOURCE = max_listings_per_source
    MAX_TOTAL_LISTINGS_SHOWN = max_total_listings

    listing_cards = ""
    by_source: dict[str, list[Listing]] = {}
    for l in new_listings:
        by_source.setdefault(l.source_name, []).append(l)

    shown_total = 0
    skipped_sources: list[str] = []
    for source_name, items in by_source.items():
        budget = MAX_TOTAL_LISTINGS_SHOWN - shown_total
        if budget <= 0:
            skipped_sources.append(source_name)
            continue
        shown = items[:min(MAX_LISTINGS_PER_SOURCE, budget)]
        shown_total += len(shown)
        remaining = len(items) - len(shown)
        listing_cards += f"""
        <h3 style="margin:28px 0 10px;font-size:14px;font-weight:600;
            color:#475569;text-transform:uppercase;letter-spacing:.05em;">
          {source_name} &mdash; {len(items)} listing{'s' if len(items) != 1 else ''}
        </h3>"""
        for item in shown:
            img_html = ""
            if item.image_url:
                img_html = f"""
                <img src="{item.image_url}" alt="" width="80" height="80"
                     style="width:80px;height:80px;object-fit:cover;
                            border-radius:6px;flex-shrink:0;" />"""
            price_html = ""
            if item.price:
                price_html = f"""
                <span style="display:inline-block;margin-top:6px;padding:2px 10px;
                    background:#dcfce7;color:#166534;border-radius:12px;
                    font-size:13px;font-weight:600;">{item.price}</span>"""
                ctx = price_context(item.title, item.price, price_index or {}, market_index,
                                    description=item.description)
                if ctx.get("note"):
                    price_html += f"""
                <span style="font-size:11px;color:#b45309;font-weight:600;">&nbsp;{ctx['note']}</span>"""
                if ctx.get("typical"):
                    each = " each" if ctx.get("qty", 1) > 1 else ""
                    price_html += f"""
                <span style="font-size:11px;color:#94a3b8;">&nbsp;{ctx['label']}{each}</span>"""
                    if ctx.get("deal"):
                        price_html += _flag_html("Deal", "#dcfce7", "#166534")
            if needs_repair(item.title, item.description):
                price_html += _flag_html("Needs repair", "#fffbeb", "#b45309")
            desc_html = ""
            if item.description:
                desc_html = f"""
                <p style="margin:6px 0 0;font-size:13px;color:#64748b;line-height:1.5;">
                  {item.description}
                </p>"""
            posted_html = ""
            if item.posted_at:
                posted_html = f"""
                <span style="font-size:11px;color:#94a3b8;">
                  &nbsp;· {item.posted_at.strftime('%b %d, %I:%M %p UTC')}
                </span>"""

            listing_cards += f"""
            <div style="display:flex;gap:14px;padding:14px;margin-bottom:10px;
                background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;
                align-items:flex-start;">
              {img_html}
              <div style="flex:1;min-width:0;">
                <a href="{item.url}" style="font-size:15px;font-weight:600;
                    color:#1e3a5f;text-decoration:none;line-height:1.3;">
                  {item.title}
                </a>
                {posted_html}
                {price_html}
                {desc_html}
              </div>
            </div>"""

        if remaining > 0:
            listing_cards += f"""
            <p style="margin:4px 0 0;font-size:13px;color:#64748b;">
              + {remaining} more from {source_name} — see the full list on the dashboard.
            </p>"""

    total_new = len(new_listings)
    if skipped_sources or shown_total < total_new:
        hidden_count = total_new - shown_total
        skipped_note = f" ({len(skipped_sources)} source{'s' if len(skipped_sources) != 1 else ''} not shown at all: {', '.join(skipped_sources)})" if skipped_sources else ""
        listing_cards += f"""
        <div style="margin-top:20px;padding:14px 18px;background:#eff6ff;
            border:1px solid #bfdbfe;border-radius:8px;color:#1d4ed8;font-size:13px;">
          This email shows {shown_total} of {total_new} new listings found this run —
          the rest ({hidden_count}) are in the dashboard{skipped_note}. Capped to keep the
          email from growing large enough that some email clients (Gmail included) truncate it.
        </div>"""

    # --- Failed sources alert block ---
    failed_alert = ""
    if failed_sources:
        names = ", ".join(r.source_name for r in failed_sources)
        failed_alert = f"""
        <div style="margin-bottom:24px;padding:14px 18px;background:#fef2f2;
            border:1px solid #fecaca;border-radius:8px;color:#991b1b;">
          <strong>⚠️ {len(failed_sources)} source{'s' if len(failed_sources) != 1 else ''} failed:</strong>
          {names}<br/>
          <span style="font-size:13px;">See the Source Status section below for details and fix instructions.</span>
        </div>"""

    run_label = f"Run #{run_number} &middot; {run_time.strftime('%A %B %d, %Y at %I:%M %p UTC')}"
    total_ok = len(ok_sources)
    total_sources = len(results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gear Scout Digest</title></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:680px;margin:0 auto;padding:20px;">

    <!-- Header -->
    <div style="background:#1e3a5f;color:#ffffff;padding:24px 28px;border-radius:12px 12px 0 0;">
      <div style="font-size:22px;font-weight:700;letter-spacing:-.02em;">🎛 Gear Scout</div>
      <div style="font-size:13px;color:#93c5fd;margin-top:4px;">{run_label}</div>
    </div>

    <!-- Body -->
    <div style="background:#f1f5f9;padding:24px 28px;border-radius:0 0 12px 12px;">

      <!-- This email is always a capped subset (see MAX_LISTINGS_PER_SOURCE/
      MAX_TOTAL_LISTINGS_SHOWN above) — this banner is unconditional, not
      just shown when this particular run happened to get truncated, so
      it's never a surprise that the dashboard has more. -->
      <div style="margin-bottom:20px;padding:14px 18px;background:#1e3a5f;
          border-radius:8px;display:flex;align-items:center;justify-content:space-between;
          flex-wrap:wrap;gap:10px;">
        <span style="color:#ffffff;font-size:13px;">
          This email never lists <strong>all</strong> your results — only a capped preview.
        </span>
        <a href="{dashboard_url}" target="_blank" rel="noopener"
           style="display:inline-block;padding:8px 18px;background:#ffffff;
               color:#1e3a5f;text-decoration:none;border-radius:6px;
               font-size:13px;font-weight:700;white-space:nowrap;">
          See everything on the dashboard &rarr;
        </a>
      </div>

      {failed_alert}

      {_price_drops_html(price_drops)}

      <!-- New listings -->
      <div style="margin-bottom:32px;">
        <h2 style="margin:0 0 16px;font-size:18px;font-weight:700;color:#1e293b;">
          🆕 {len(new_listings)} New Listing{'s' if len(new_listings) != 1 else ''} Found
        </h2>
        {listing_cards if listing_cards else '<p style="color:#64748b;">No new matching listings this run.</p>'}
      </div>

      <!-- Source status -->
      <div>
        <h2 style="margin:0 0 12px;font-size:16px;font-weight:700;color:#1e293b;">
          📡 Source Status &mdash; {total_ok}/{total_sources} OK
        </h2>
        <table style="width:100%;border-collapse:collapse;background:#ffffff;
            border-radius:8px;overflow:hidden;">
          <tbody>{status_rows}</tbody>
        </table>
      </div>

    </div>

    <!-- Footer -->
    <div style="text-align:center;padding:16px;color:#94a3b8;font-size:12px;">
      Gear Scout &middot; Running in Docker &middot; Edit sources in config.yaml
    </div>
  </div>
</body>
</html>"""
    return html


def _flag_html(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block;margin:6px 0 0 6px;padding:1px 8px;background:{bg};'
            f'color:{fg};border-radius:10px;font-size:11px;font-weight:700;">{text}</span>')


def _price_drops_html(drops: Optional[list[dict]]) -> str:
    if not drops:
        return ""
    rows = "".join(
        f"""<div style="padding:10px 0;border-bottom:1px solid #e2e8f0;">
              <a href="{d['url']}" style="color:#1e3a5f;font-weight:600;text-decoration:none;">{d['title']}</a><br>
              <span style="color:#166534;font-weight:700;">{d['price']}</span>
              <span style="color:#94a3b8;text-decoration:line-through;">&nbsp;{d.get('previous_price') or ''}</span>
              <span style="color:#94a3b8;font-size:12px;">&nbsp;· {d['source_name']}</span>
            </div>"""
        for d in drops
    )
    return f"""
      <div style="margin-bottom:28px;padding:14px 18px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;">
        <h2 style="margin:0 0 6px;font-size:16px;font-weight:700;color:#1d4ed8;">⬇️ Price drops on your favorites</h2>
        {rows}
      </div>"""


def build_watch_alert_html(new_by_watch: dict[str, list[dict]], dashboard_url: str) -> str:
    from urllib.parse import quote_plus
    sections = ""
    for q, hits in new_by_watch.items():
        items = "".join(
            f"""<div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #e2e8f0;">
                  {f'<img src="{h["image_url"]}" width="64" height="64" style="width:64px;height:64px;object-fit:cover;border-radius:6px;">' if h.get("image_url") else ''}
                  <div><a href="{h['url']}" style="color:#1e3a5f;font-weight:600;text-decoration:none;">{h['title']}</a><br>
                  <span style="color:#166534;font-weight:700;">{h['price'] or ''}</span>
                  <span style="color:#94a3b8;font-size:12px;">&nbsp;· {h['source_name']}</span>
                  {_flag_html('Needs repair', '#fffbeb', '#b45309') if h.get('needs_repair') else ''}</div>
                </div>"""
            for h in hits
        )
        sections += f"""
          <h2 style="margin:18px 0 6px;font-size:16px;color:#1e293b;">🔔 {len(hits)} new for “{q}”</h2>
          {items}
          <p style="margin:8px 0 0;font-size:13px;"><a href="{dashboard_url}/search?q={quote_plus(q)}">See all results for “{q}” →</a></p>"""
    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <div style="max-width:640px;margin:0 auto;padding:20px;">
        <div style="background:#1e3a5f;color:#fff;padding:18px 22px;border-radius:12px 12px 0 0;font-size:18px;font-weight:700;">🎛 Gear Scout — saved search alert</div>
        <div style="background:#fff;padding:6px 22px 20px;border-radius:0 0 12px 12px;">{sections}</div>
      </div></body></html>"""


def send_email(
    html: str,
    subject: str,
    cfg: dict,
    new_count: int,
    failed_count: int,
):
    """Send the digest via Gmail SMTP."""
    subject_line = f"{subject} — {new_count} new listing{'s' if new_count != 1 else ''}"
    if failed_count:
        subject_line += f" | ⚠️ {failed_count} source error{'s' if failed_count != 1 else ''}"
    return send_html_email(html, subject_line, cfg)


def send_html_email(html: str, subject_line: str, cfg: dict) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject_line
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["from"], cfg["password"])
            server.sendmail(cfg["from"], [cfg["to"]], msg.as_string())
        logger.info("Email sent: %s", msg["Subject"])
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Gmail authentication failed. "
            "Make sure you're using an App Password, not your regular password. "
            "Get one at: https://myaccount.google.com/apppasswords"
        )
        return False
    except smtplib.SMTPException as e:
        logger.error("SMTP error: %s", e)
        return False
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        return False
