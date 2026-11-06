#!/usr/bin/env python3
"""Live builder status page on http://localhost:8421 (auto-refreshes every 15 s). Run in a Terminal tab."""
import html, json, subprocess, os
from http.server import BaseHTTPRequestHandler, HTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
MARK = {"done": "✓", "running": "▶", "waiting": "·"}

def page():
    try:
        s = json.loads(subprocess.run(["python3", f"{HERE}/agent_status.py"], capture_output=True, text=True, timeout=30).stdout)
    except Exception as e:
        return f"<p>Status unavailable: {html.escape(str(e))}</p>"
    pct = int(100 * s["done"] / max(s["total"], 1))
    rows = "".join(f'<li class="{p["state"]}"><b>{MARK[p["state"]]}</b> {html.escape(p["label"])}</li>' for p in s["plan"])
    commits = "".join(f"<li>{html.escape(c[:110])}</li>" for c in s["commits"][:8])
    lim = ", ".join(f"{a} until {t}" for a, t in s["limited"].items())
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=15>
<title>Gear Scout builder</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--dim:#777;--bar:#2b7a4b;--track:#e6e6e6}}@media(prefers-color-scheme:dark){{:root{{--bg:#161616;--fg:#eee;--dim:#999;--track:#333;--bar:#4cc38a}}}}
body{{background:var(--bg);color:var(--fg);font:15px -apple-system,system-ui,sans-serif;margin:0;padding:16px;max-width:640px}}
.track{{background:var(--track);border-radius:8px;height:14px}}.fill{{background:var(--bar);height:14px;border-radius:8px;width:{pct}%}}
ul{{list-style:none;padding:0}}li{{padding:5px 0}}.waiting,.done{{color:var(--dim)}}.running{{font-weight:600}}h3{{margin:20px 0 4px}}small{{color:var(--dim)}}</style>
<h2>Gear Scout builder</h2><div class=track><div class=fill></div></div>
<p><b>{s['done']}/{s['total']}</b> batches done · App {'healthy' if s['healthy'] else '<b>DOWN</b>'} · {s['counts'].get('proposed',0)} ideas waiting · {s['counts'].get('built',0)} built{(' · limited: '+lim) if lim else ''}</p>
<ul>{rows}</ul><h3>Latest commits</h3><ul>{commits}</ul><small>Updated {s['at']}, refreshes itself</small>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = page().encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8421), H).serve_forever()
