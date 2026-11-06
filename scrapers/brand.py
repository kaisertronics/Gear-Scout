"""Gear Scout brand: the knob / VU / patch-cable mark and palette from the
original logo (Fraunces italic + IBM Plex Sans, charcoal, cream, orange)."""
import base64
from functools import lru_cache
from pathlib import Path

CHARCOAL, PANEL, CREAM, ORANGE, MUTED = "#161512", "#2A2722", "#EFE6D2", "#E0892B", "#A39A8A"
SERIF = "'GS Fraunces', Fraunces, Georgia, serif"
SANS = "'GS Plex', 'IBM Plex Sans', 'Helvetica Neue', Helvetica, Arial, sans-serif"
FONTS = Path(__file__).resolve().parent.parent / "static" / "fonts"

_MARK = """<rect x="20" y="100" width="360" height="160" rx="12" fill="#2A2722" stroke="#EFE6D2" stroke-width="8"/>
<line x1="46" y1="104" x2="46" y2="256" stroke="#EFE6D2" stroke-width="2.5" stroke-opacity="0.5"/>
<line x1="354" y1="104" x2="354" y2="256" stroke="#EFE6D2" stroke-width="2.5" stroke-opacity="0.5"/>
<rect x="29" y="118" width="8" height="18" rx="4" fill="#161512"/>
<rect x="29" y="224" width="8" height="18" rx="4" fill="#161512"/>
<rect x="363" y="118" width="8" height="18" rx="4" fill="#161512"/>
<rect x="363" y="224" width="8" height="18" rx="4" fill="#161512"/>
<rect x="58" y="116" width="64" height="50" rx="5" fill="#EFE6D2"/>
<path d="M65.8 146 A28 28 0 0 1 114.2 146" fill="none" stroke="#161512" stroke-width="2.5" stroke-linecap="round"/>
<g stroke-width="2.5" stroke-linecap="round">
<line x1="68.6" y1="142" x2="64" y2="138.1" stroke="#161512"/>
<line x1="78.2" y1="134.6" x2="75.6" y2="129.2" stroke="#161512"/>
<line x1="90" y1="132" x2="90" y2="126" stroke="#161512"/>
<line x1="101.8" y1="134.6" x2="104.4" y2="129.2" stroke="#161512"/>
<line x1="111.4" y1="142" x2="116" y2="138.1" stroke="{accent}"/>
</g>
<line x1="90" y1="160" x2="106" y2="132.3" stroke="{accent}" stroke-width="3" stroke-linecap="round"/>
<circle cx="90" cy="160" r="3.5" fill="#161512"/>
<circle cx="57.5" cy="221.5" r="1.4" fill="#EFE6D2"/><circle cx="57.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="74" cy="193" r="1.4" fill="#EFE6D2"/><circle cx="90.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="90.5" cy="221.5" r="1.4" fill="#EFE6D2"/>
<circle cx="74" cy="212" r="14" fill="#EFE6D2"/>
<circle cx="74" cy="212" r="13" fill="none" stroke="#2A2722" stroke-width="2" stroke-dasharray="1.5 2.1"/>
<circle cx="74" cy="212" r="8.5" fill="#2A2722"/>
<line x1="74" y1="212" x2="69.8" y2="207.8" stroke="#EFE6D2" stroke-width="3" stroke-linecap="round"/>
<circle cx="93.5" cy="221.5" r="1.4" fill="#EFE6D2"/><circle cx="93.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="110" cy="193" r="1.4" fill="#EFE6D2"/><circle cx="126.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="126.5" cy="221.5" r="1.4" fill="#EFE6D2"/>
<circle cx="110" cy="212" r="14" fill="#EFE6D2"/>
<circle cx="110" cy="212" r="13" fill="none" stroke="#2A2722" stroke-width="2" stroke-dasharray="1.5 2.1"/>
<circle cx="110" cy="212" r="8.5" fill="#2A2722"/>
<line x1="110" y1="212" x2="114.2" y2="207.8" stroke="#EFE6D2" stroke-width="3" stroke-linecap="round"/>
<circle cx="273.5" cy="221.5" r="1.4" fill="#EFE6D2"/><circle cx="273.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="290" cy="193" r="1.4" fill="#EFE6D2"/><circle cx="306.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="306.5" cy="221.5" r="1.4" fill="#EFE6D2"/>
<circle cx="290" cy="212" r="14" fill="#EFE6D2"/>
<circle cx="290" cy="212" r="13" fill="none" stroke="#2A2722" stroke-width="2" stroke-dasharray="1.5 2.1"/>
<circle cx="290" cy="212" r="8.5" fill="#2A2722"/>
<line x1="290" y1="212" x2="290" y2="206" stroke="#EFE6D2" stroke-width="3" stroke-linecap="round"/>
<circle cx="309.5" cy="221.5" r="1.4" fill="#EFE6D2"/><circle cx="309.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="326" cy="193" r="1.4" fill="#EFE6D2"/><circle cx="342.5" cy="202.5" r="1.4" fill="#EFE6D2"/><circle cx="342.5" cy="221.5" r="1.4" fill="#EFE6D2"/>
<circle cx="326" cy="212" r="14" fill="#EFE6D2"/>
<circle cx="326" cy="212" r="13" fill="none" stroke="#2A2722" stroke-width="2" stroke-dasharray="1.5 2.1"/>
<circle cx="326" cy="212" r="8.5" fill="#2A2722"/>
<line x1="326" y1="212" x2="331.2" y2="209" stroke="#EFE6D2" stroke-width="3" stroke-linecap="round"/>
<circle cx="290" cy="138" r="7" fill="#2A2722" stroke="#EFE6D2" stroke-width="2.5"/>
<line x1="290" y1="138" x2="284" y2="123" stroke="#EFE6D2" stroke-width="4" stroke-linecap="round"/>
<circle cx="326" cy="138" r="8" fill="none" stroke="#EFE6D2" stroke-width="3"/>
<circle cx="326" cy="138" r="4.5" fill="{accent}"/>
<g transform="rotate(45 200 180)">
<rect x="266" y="169" width="20" height="22" rx="3" fill="#EFE6D2"/>
<rect x="284" y="165" width="90" height="30" rx="15" fill="{accent}"/>
<line x1="304" y1="168" x2="304" y2="192" stroke="#161512" stroke-width="3" stroke-opacity="0.35"/>
<line x1="314" y1="168" x2="314" y2="192" stroke="#161512" stroke-width="3" stroke-opacity="0.35"/>
<line x1="324" y1="168" x2="324" y2="192" stroke="#161512" stroke-width="3" stroke-opacity="0.35"/>
</g>
<circle cx="200" cy="180" r="62" fill="#161512" stroke="#EFE6D2" stroke-width="16"/>
<circle cx="200" cy="180" r="36" fill="#EFE6D2"/>
<circle cx="200" cy="180" r="34" fill="none" stroke="#161512" stroke-width="4" stroke-dasharray="3 4.12"/>
<circle cx="200" cy="180" r="25" fill="{accent}"/>
<line x1="200" y1="180" x2="209" y2="164.4" stroke="#161512" stroke-width="6" stroke-linecap="round"/>
<path d="M154.7 172 A46 46 0 0 1 184.3 136.8" fill="none" stroke="#EFE6D2" stroke-opacity="0.4" stroke-width="5" stroke-linecap="round"/>"""


def mark(x, y, w, accent=ORANGE) -> str:
    """The mark as a nested <svg>, w px wide (aspect 380:236)."""
    h = w * 236 / 380
    return (f'<svg x="{x}" y="{y}" width="{w:.0f}" height="{h:.0f}" viewBox="10 88 380 236">'
            + _MARK.format(accent=accent) + "</svg>")


@lru_cache(maxsize=1)
def font_css() -> str:
    """@font-face rules with the fonts inlined: an SVG drawn as an image
    (which is how the page makes PNGs) can't fetch anything."""
    css = ""
    for fam, fn, style in (("GS Fraunces", "fraunces-800i.woff2", "italic"), ("GS Plex", "plex-sans.woff2", "normal")):
        try:
            b64 = base64.b64encode((FONTS / fn).read_bytes()).decode()
        except OSError:
            continue
        css += (f"@font-face{{font-family:'{fam}';font-style:{style};font-weight:100 900;"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return css
