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
         + _stat(72, str(sum(i.get("qty") or 1 for i in d["items"] if i["saved"] is not None)), "PIECES OF GEAR")
         + _stat(390, f'{d["pct"]}%', "UNDER NEW")
         + _stat(708, _money(d["total_paid"]), "PAID", f'vs {_money(d["total_new"])} new', size=48))
    return _frame(b, n, total)


def item_slide(it, n, total):
    """One piece: NEW (if it's still made), USED MARKET AVERAGE and YOU
    PAID as bars, then what you saved and how you did against the market."""
    lines = _wrap(it["item"], 22)[:2]
    size = 80 if len(lines) == 1 else 70
    title = "".join(_serif(70, 290 + i * 76, size, E(l)) for i, l in enumerate(lines))
    y0 = 290 + (len(lines) - 1) * 76 + 30
    bars = []
    if it.get("new_only"):
        bars.append(("NEW", it["new_only"], PANEL, CREAM))
    if it.get("used"):
        bars.append(("USED MARKET AVERAGE", it["used"], "#4A443A", CREAM))
    bars.append(("YOU PAID", it["paid"], ORANGE, ORANGE))
    top = max(v for _, v, _, _ in bars) or 1
    bw, b = 936, _sans(72, 200, 28, f"STEAL #{n - 1}", ORANGE, spacing=6) + title
    for k, (label, v, fill, ink) in enumerate(bars):
        y = y0 + k * 112
        w = max(48, bw * v / top)
        b += (_sans(72, y + 34, 22, label, spacing=4)
              + f'<rect x="72" y="{y + 48}" width="{w:.0f}" height="52" rx="12" fill="{fill}"/>')
        if w > bw - 230:
            b += _serif(72 + w - 18, y + 86, 38, _money(v), CHARCOAL if fill == ORANGE else CREAM, "end")
        else:
            b += _serif(72 + w + 16, y + 86, 38, _money(v), ink)
    y = y0 + len(bars) * 112 + 36
    against_new = it.get("against", "new") == "new"
    vm = it.get("vs_market")
    label = {"new": "YOU SAVED VS NEW", "mixed": "YOU SAVED VS NEW (USED WHERE NO NEW PRICE)"}.get(
        it.get("against", "new"), "YOU SAVED VS THE USED MARKET AVERAGE")
    b += _sans(72, y + 10, 24, label, CREAM, spacing=4)
    b += _serif(66, y + 110, 108, _money(it["saved"]), ORANGE)
    b += _serif(1014, y + 110, 64, f'{it["pct"]}% off', CREAM, "end")
    if against_new and vm is not None:
        word = "under" if vm >= 0 else "over"
        b += _sans(72, min(y + 158, 950), 26, f"and {_money(abs(vm))} {word} the used market average", MUTED, weight=500)
    elif it.get("against") == "used":
        b += _sans(72, min(y + 158, 950), 26, "no new price found, so this is against used prices", MUTED, weight=500)
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


def market(d, n, total):
    """The market check: vs new, vs the used market, what it's worth."""
    vm = d.get("vs_market") or 0
    b = (_sans(72, 236, 28, "THE MARKET CHECK", ORANGE, spacing=6)
         + _serif(70, 330, 84, "Not just cheaper")
         + _serif(70, 416, 84, "than new.", ORANGE))
    rows = [("SAVED VS BUYING NEW", _money(d["total_saved"]), f'{d["pct"]}% under new'),
            ("VS THE USED MARKET AVERAGE", ("−" if vm < 0 else "") + _money(abs(vm)),
             f'{abs(d.get("vs_market_pct") or 0)}% {"under" if vm >= 0 else "over"} going used prices'),
            ("WORTH TODAY, USED", _money(d.get("market_value")), f'paid {_money(d.get("market_paid"))}')]
    for k, (label, big, sub) in enumerate(rows):
        y = 480 + k * 150
        b += (f'<rect x="72" y="{y}" width="936" height="128" rx="22" fill="{PANEL}"/>'
              + _sans(104, y + 50, 24, label, spacing=4)
              + _sans(104, y + 96, 26, sub, CREAM, weight=500)
              + _serif(980, y + 90, 64, big, ORANGE if k != 1 or vm >= 0 else CREAM, "end"))
    return _frame(b, n, total)


def running(d, n, total):
    pts = [i for i in d["items"] if i["saved"] is not None]
    # Scaled from the lowest point (0, or below if an early buy cost more
    # than new) to the highest, so a dip below zero stays on the chart.
    lo = min([0] + [p["running"] for p in pts])
    span = (max([p["running"] for p in pts] or [0]) - lo) or 1
    x0, x1, y0, y1 = 100, 1000, 880, 420
    step = (x1 - x0) / max(len(pts) - 1, 1)
    coords = [(x0 + k * step, y0 - (y0 - y1) * (p["running"] - lo) / span) for k, p in enumerate(pts)]
    yz = y0 - (y0 - y1) * (0 - lo) / span  # the $0 line
    line = " ".join(f"{x:.0f},{y:.0f}" for x, y in coords)
    area = f"{x0},{yz:.0f} " + line + f" {coords[-1][0]:.0f},{yz:.0f}" if coords else ""
    b = (_sans(72, 236, 28, "RUNNING TOTAL SAVED", ORANGE, spacing=6)
         + _serif(64, 372, 140, _money(d["total_saved"]), ORANGE)
         + f'<line x1="{x0}" y1="{yz:.0f}" x2="{x1}" y2="{yz:.0f}" stroke="#3d3a33" stroke-width="3"/>')
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
    total = len(items) + 4
    out = [cover(d, 1, total)]
    out += [item_slide(it, k + 2, total) for k, it in enumerate(items)]
    out += [leaderboard(d, total - 2, total), market(d, total - 1, total), running(d, total, total)]
    return out
