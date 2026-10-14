"""1080×1080 social slides (SVG) for the Saved page. System fonts only, so
the browser can turn them into PNGs exactly as drawn."""
from html import escape as E

W = H = 1080
DISPLAY = "'Arial Black', 'Helvetica Neue', Impact, Arial, sans-serif"
BODY = "'Helvetica Neue', Helvetica, Arial, sans-serif"
GOLD, GREEN, RED, INK, DIM = "#ffd166", "#3ddc97", "#ff6b6b", "#f4f1ea", "#9aa3ab"


def _money(v, cents=False):
    return ("${:,.2f}" if cents else "${:,.0f}").format(v or 0)


def _frame(body, n, total):
    bg = (f'<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
          f'<stop offset="0" stop-color="#16181d"/><stop offset="1" stop-color="#0b0c0f"/></linearGradient>'
          f'<radialGradient id="glow" cx=".85" cy=".1" r=".7"><stop offset="0" stop-color="{GOLD}" stop-opacity=".22"/>'
          f'<stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient></defs>'
          f'<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>')
    # Patchbay-style jack row as a signature border.
    jacks = "".join(f'<circle cx="{60 + i * 40}" cy="1012" r="9" fill="#2b3138" stroke="#3a424a" stroke-width="2"/>' for i in range(25))
    foot = (f'<text x="60" y="980" font-family="{BODY}" font-size="26" font-weight="700" fill="{DIM}" letter-spacing="3">TELEX RECORDING · BOUGHT USED</text>'
            f'<text x="1020" y="980" font-family="{BODY}" font-size="26" font-weight="700" fill="{DIM}" text-anchor="end">{n} / {total}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">{bg}{body}{jacks}{foot}</svg>'


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


def cover(d, n, total):
    b = (f'<text x="60" y="150" font-family="{BODY}" font-size="34" font-weight="700" fill="{GOLD}" letter-spacing="6">THE USED-GEAR SCOREBOARD</text>'
         f'<text x="60" y="300" font-family="{DISPLAY}" font-size="96" fill="{INK}">I SAVED</text>'
         f'<text x="52" y="500" font-family="{DISPLAY}" font-size="{190 if d["total_saved"] < 100000 else 150}" fill="{GREEN}">{_money(d["total_saved"])}</text>'
         f'<text x="60" y="590" font-family="{DISPLAY}" font-size="60" fill="{INK}">BUYING USED</text>'
         f'<rect x="60" y="660" width="290" height="150" rx="18" fill="#1f232a" stroke="#2f353d"/>'
         f'<text x="205" y="740" font-family="{DISPLAY}" font-size="64" fill="{GOLD}" text-anchor="middle">{d["count"]}</text>'
         f'<text x="205" y="785" font-family="{BODY}" font-size="24" font-weight="700" fill="{DIM}" text-anchor="middle">PIECES OF GEAR</text>'
         f'<rect x="395" y="660" width="290" height="150" rx="18" fill="#1f232a" stroke="#2f353d"/>'
         f'<text x="540" y="740" font-family="{DISPLAY}" font-size="64" fill="{GOLD}" text-anchor="middle">{d["pct"]}%</text>'
         f'<text x="540" y="785" font-family="{BODY}" font-size="24" font-weight="700" fill="{DIM}" text-anchor="middle">UNDER NEW</text>'
         f'<rect x="730" y="660" width="290" height="150" rx="18" fill="#1f232a" stroke="#2f353d"/>'
         f'<text x="875" y="732" font-family="{DISPLAY}" font-size="40" fill="{INK}" text-anchor="middle">{_money(d["total_paid"])}</text>'
         f'<text x="875" y="762" font-family="{BODY}" font-size="22" font-weight="700" fill="{DIM}" text-anchor="middle">PAID</text>'
         f'<text x="875" y="795" font-family="{BODY}" font-size="22" fill="{DIM}" text-anchor="middle">vs {_money(d["total_new"])} new</text>'
         f'<text x="60" y="890" font-family="{BODY}" font-size="30" fill="{DIM}">Swipe for every piece →</text>')
    return _frame(b, n, total)


def item_slide(it, n, total):
    lines = _wrap(it["item"].upper(), 18)
    title = "".join(f'<text x="60" y="{200 + i * 84}" font-family="{DISPLAY}" font-size="76" fill="{INK}">{E(l)}</text>' for i, l in enumerate(lines))
    y0 = 200 + len(lines) * 84 + 30
    new, paid = it["new"] or 1, it["paid"]
    bw = 900
    pw = max(40, bw * min(paid / new, 1))
    b = (f'<text x="60" y="100" font-family="{BODY}" font-size="30" font-weight="700" fill="{GOLD}" letter-spacing="5">STEAL #{n - 1}</text>' + title +
         f'<text x="60" y="{y0 + 40}" font-family="{BODY}" font-size="28" font-weight="700" fill="{DIM}" letter-spacing="3">NEW</text>'
         f'<rect x="60" y="{y0 + 60}" width="{bw}" height="70" rx="12" fill="#2b3138"/>'
         f'<text x="{60 + bw - 20}" y="{y0 + 108}" font-family="{DISPLAY}" font-size="40" fill="{INK}" text-anchor="end">{_money(it["new"])}</text>'
         f'<text x="60" y="{y0 + 185}" font-family="{BODY}" font-size="28" font-weight="700" fill="{DIM}" letter-spacing="3">I PAID</text>'
         f'<rect x="60" y="{y0 + 205}" width="{pw:.0f}" height="70" rx="12" fill="{GOLD}"/>'
         f'<text x="{60 + pw + 18:.0f}" y="{y0 + 253}" font-family="{DISPLAY}" font-size="40" fill="{GOLD}">{_money(paid)}</text>'
         f'<text x="60" y="{min(y0 + 430, 900)}" font-family="{DISPLAY}" font-size="120" fill="{GREEN}">−{_money(it["saved"])}</text>'
         f'<text x="1020" y="{min(y0 + 430, 900)}" font-family="{DISPLAY}" font-size="70" fill="{INK}" text-anchor="end">{it["pct"]}% OFF</text>')
    if it.get("where"):
        b += f'<text x="60" y="935" font-family="{BODY}" font-size="26" fill="{DIM}">found on {E(it["where"])} · running total {_money(it["running"])}</text>'
    else:
        b += f'<text x="60" y="935" font-family="{BODY}" font-size="26" fill="{DIM}">running total saved: {_money(it["running"])}</text>'
    return _frame(b, n, total)


def leaderboard(d, n, total):
    top = sorted([i for i in d["items"] if i["saved"] is not None], key=lambda i: -i["saved"])[:7]
    mx = max([i["saved"] for i in top] or [1]) or 1
    b = (f'<text x="60" y="130" font-family="{BODY}" font-size="30" font-weight="700" fill="{GOLD}" letter-spacing="5">BIGGEST SAVES</text>'
         f'<text x="60" y="220" font-family="{DISPLAY}" font-size="80" fill="{INK}">THE LEADERBOARD</text>')
    for k, it in enumerate(top):
        y = 290 + k * 92
        w = 640 * max(it["saved"], 0) / mx
        name = it["item"] if len(it["item"]) <= 26 else it["item"][:25] + "…"
        b += (f'<text x="60" y="{y + 26}" font-family="{BODY}" font-size="30" font-weight="700" fill="{INK}">{E(name)}</text>'
              f'<rect x="60" y="{y + 40}" width="{max(w, 8):.0f}" height="30" rx="8" fill="{GREEN if k else GOLD}"/>'
              f'<text x="{60 + max(w, 8) + 16:.0f}" y="{y + 66}" font-family="{DISPLAY}" font-size="30" fill="{INK}">{_money(it["saved"])}</text>')
    return _frame(b, n, total)


def running(d, n, total):
    pts = [i for i in d["items"] if i["saved"] is not None]
    mx = max([p["running"] for p in pts] or [1]) or 1
    x0, x1, y0, y1 = 100, 1000, 860, 330
    step = (x1 - x0) / max(len(pts) - 1, 1)
    coords = [(x0 + k * step, y0 - (y0 - y1) * p["running"] / mx) for k, p in enumerate(pts)]
    line = " ".join(f"{x:.0f},{y:.0f}" for x, y in coords)
    area = f"{x0},{y0} " + line + f" {coords[-1][0]:.0f},{y0}" if coords else ""
    b = (f'<text x="60" y="130" font-family="{BODY}" font-size="30" font-weight="700" fill="{GOLD}" letter-spacing="5">RUNNING TOTAL</text>'
         f'<text x="60" y="230" font-family="{DISPLAY}" font-size="100" fill="{GREEN}">{_money(d["total_saved"])}</text>'
         f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#2f353d" stroke-width="3"/>')
    if coords:
        b += (f'<polygon points="{area}" fill="{GREEN}" fill-opacity=".15"/>'
              f'<polyline points="{line}" fill="none" stroke="{GREEN}" stroke-width="8" stroke-linejoin="round" stroke-linecap="round"/>')
        for x, y in coords:
            b += f'<circle cx="{x:.0f}" cy="{y:.0f}" r="9" fill="{GOLD}"/>'
        b += f'<text x="{x0}" y="{y0 + 50}" font-family="{BODY}" font-size="26" fill="{DIM}">first buy</text>'
        b += f'<text x="{x1}" y="{y0 + 50}" font-family="{BODY}" font-size="26" fill="{DIM}" text-anchor="end">today</text>'
    return _frame(b, n, total)


def slides(d) -> list[str]:
    items = [i for i in d["items"] if i["saved"] is not None]
    total = len(items) + 3
    out = [cover(d, 1, total)]
    out += [item_slide(it, k + 2, total) for k, it in enumerate(items)]
    out += [leaderboard(d, total - 1, total), running(d, total, total)]
    return out
