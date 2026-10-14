"""1080×1080 social slides (SVG) for the Saved page, in the Gear Scout brand
(the original logo's mark, Fraunces italic + IBM Plex Sans, charcoal /
cream / orange). Fonts are embedded so the browser can turn them into PNGs
exactly as drawn."""
from html import escape as E

from scrapers.brand import CHARCOAL, CREAM, MUTED, ORANGE, PANEL, SANS, SERIF, font_css, mark

W = H = 1080


def _money(v, cents=False):
    return ("${:,.2f}" if cents else "${:,.0f}").format(v or 0)


def _serif(x, y, size, text, fill=CREAM, anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="{SERIF}" font-style="italic" font-weight="800" '
            f'font-size="{size}" letter-spacing="-0.02em" fill="{fill}" text-anchor="{anchor}">{text}</text>')


def _sans(x, y, size, text, fill=MUTED, weight=700, spacing=0, anchor="start"):
    return (f'<text x="{x}" y="{y}" font-family="{SANS}" font-weight="{weight}" font-size="{size}" '
            f'letter-spacing="{spacing}" fill="{fill}" text-anchor="{anchor}">{text}</text>')


def _frame(body, n, total):
    bg = (f'<defs><style>{font_css()}</style>'
          f'<radialGradient id="glow" cx=".88" cy=".08" r=".75"><stop offset="0" stop-color="{ORANGE}" stop-opacity=".16"/>'
          f'<stop offset="1" stop-color="{ORANGE}" stop-opacity="0"/></radialGradient></defs>'
          f'<rect width="{W}" height="{H}" fill="{CHARCOAL}"/><rect width="{W}" height="{H}" fill="url(#glow)"/>')
    # Header: the mark + "Gear Scout" wordmark, like the brand's social posts.
    head = (mark(66, 60, 104)
            + f'<text x="186" y="106" font-family="{SERIF}" font-style="italic" font-weight="800" font-size="40" '
              f'letter-spacing="-0.01em" fill="{CREAM}">Gear <tspan fill="{ORANGE}">Scout</tspan></text>'
            + _sans(1014, 104, 24, f"{n} / {total}", anchor="end"))
    # Footer: a row of patch-bay jacks with the tagline.
    jacks = "".join(f'<circle cx="{72 + i * 39}" cy="1022" r="8" fill="{PANEL}" stroke="#3d3a33" stroke-width="2"/>'
                    for i in range(25))
    foot = _sans(72, 986, 22, "BOUGHT USED · FOUND WITH GEAR SCOUT", spacing=4)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
            f'{bg}{head}{body}{jacks}{foot}</svg>')


def _wrap(text, max_chars):
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines[:3]


def _stat(x, value, label, sub=None, size=64):
    s = (f'<rect x="{x}" y="728" width="300" height="160" rx="22" fill="{PANEL}"/>'
         + _serif(x + 150, 812 if not sub else 800, size, value, ORANGE if not sub else CREAM, "middle")
         + _sans(x + 150, 852 if not sub else 838, 22, label, spacing=3, anchor="middle"))
    if sub:
        s += _sans(x + 150, 868, 21, sub, weight=500, anchor="middle")
    return s


def cover(d, n, total):
    amt = _money(d["total_saved"])
    b = (_sans(72, 236, 28, "THE USED-GEAR SCOREBOARD", ORANGE, spacing=6)
         + _serif(70, 362, 116, "You saved")
         + _serif(62, 566, 230 if len(amt) <= 7 else 196 if len(amt) <= 8 else 168, amt, ORANGE)
         + _serif(70, 664, 84, "using Gear Scout.")
         + _stat(72, str(d["count"]), "PIECES OF GEAR")
         + _stat(390, f'{d["pct"]}%', "UNDER NEW")
         + _stat(708, _money(d["total_paid"]), "PAID", f'vs {_money(d["total_new"])} new', size=48))
    return _frame(b, n, total)


def item_slide(it, n, total):
    lines = _wrap(it["item"], 20)
    title = "".join(_serif(70, 290 + i * 82, 80, E(l)) for i, l in enumerate(lines))
    y0 = 290 + (len(lines) - 1) * 82 + 36
    new, paid = it["new"] or 1, it["paid"]
    bw = 936
    pw = max(48, bw * min(paid / new, 1))
    b = (_sans(72, 200, 28, f"STEAL #{n - 1}", ORANGE, spacing=6) + title
         + _sans(72, y0 + 44, 24, "NEW", spacing=4)
         + f'<rect x="72" y="{y0 + 62}" width="{bw}" height="68" rx="14" fill="{PANEL}"/>'
         + _serif(72 + bw - 22, y0 + 110, 44, _money(it["new"]), CREAM, "end")
         + _sans(72, y0 + 178, 24, "YOU PAID", spacing=4)
         + f'<rect x="72" y="{y0 + 196}" width="{pw:.0f}" height="68" rx="14" fill="{ORANGE}"/>'
         + _serif(72 + pw + 18, y0 + 244, 44, _money(paid), ORANGE)
         + _sans(72, min(y0 + 340, 800), 26, "YOU SAVED", CREAM, spacing=5)
         + _serif(66, min(y0 + 460, 920), 128, _money(it["saved"]), ORANGE)
         + _serif(1014, min(y0 + 460, 920), 72, f'{it["pct"]}% off', CREAM, "end"))
    return _frame(b, n, total)


def leaderboard(d, n, total):
    top = sorted([i for i in d["items"] if i["saved"] is not None and i["saved"] > 0], key=lambda i: -i["saved"])[:6]
    mx = max([i["saved"] for i in top] or [1]) or 1
    b = (_sans(72, 236, 28, "BIGGEST SAVES", ORANGE, spacing=6)
         + _serif(70, 330, 92, "The leaderboard"))
    for k, it in enumerate(top):
        y = 380 + k * 92
        w = 660 * max(it["saved"], 0) / mx
        name = it["item"] if len(it["item"]) <= 34 else it["item"][:33] + "…"
        fade = ' fill-opacity=".85"' if k else ""
        b += (_sans(72, y + 28, 28, E(name), CREAM, weight=600)
              + f'<rect x="72" y="{y + 42}" width="{max(w, 10):.0f}" height="30" rx="15" fill="{ORANGE if not k else CREAM}"{fade}/>'
              + _serif(72 + max(w, 10) + 16, y + 68, 34, _money(it["saved"]), ORANGE if not k else CREAM))
    return _frame(b, n, total)


def running(d, n, total):
    pts = [i for i in d["items"] if i["saved"] is not None]
    mx = max([p["running"] for p in pts] or [1]) or 1
    x0, x1, y0, y1 = 100, 1000, 880, 420
    step = (x1 - x0) / max(len(pts) - 1, 1)
    coords = [(x0 + k * step, y0 - (y0 - y1) * p["running"] / mx) for k, p in enumerate(pts)]
    line = " ".join(f"{x:.0f},{y:.0f}" for x, y in coords)
    area = f"{x0},{y0} " + line + f" {coords[-1][0]:.0f},{y0}" if coords else ""
    b = (_sans(72, 236, 28, "RUNNING TOTAL SAVED", ORANGE, spacing=6)
         + _serif(64, 372, 140, _money(d["total_saved"]), ORANGE)
         + f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#3d3a33" stroke-width="3"/>')
    if coords:
        b += (f'<polygon points="{area}" fill="{ORANGE}" fill-opacity=".14"/>'
              f'<polyline points="{line}" fill="none" stroke="{ORANGE}" stroke-width="8" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in coords:
            b += f'<circle cx="{x:.0f}" cy="{y:.0f}" r="10" fill="{CHARCOAL}" stroke="{CREAM}" stroke-width="4"/>'
        b += _sans(x0, y0 + 46, 24, "first buy", weight=500)
        b += _sans(x1, y0 + 46, 24, "today", weight=500, anchor="end")
    return _frame(b, n, total)


def slides(d) -> list[str]:
    # A piece you paid over today's price stays in the table, not the posts.
    items = [i for i in d["items"] if i["saved"] is not None and i["saved"] > 0]
    total = len(items) + 3
    out = [cover(d, 1, total)]
    out += [item_slide(it, k + 2, total) for k, it in enumerate(items)]
    out += [leaderboard(d, total - 1, total), running(d, total, total)]
    return out
