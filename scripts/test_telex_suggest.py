import re
import sys
sys.path.insert(0, "/tmp/gs")
from scrapers.telex_suggest import suggest_terms

fake = lambda t: " ".join(t.split()[:3])
titles = ["SSL G Series Channel", "ssl g series channel", "Drawmer 1960 Compressor", "Random guitar"] + ["SSL X%d Thing" % i for i in range(10)]
r = suggest_terms(["ssl", "stam", "drawmer", "Drawmer 1960 Compressor", "neve 1073"], titles, term_for=fake)
assert len(r) <= 6, r
assert r[0] == {"broad": "ssl", "term": "SSL G Series", "title": "SSL G Series Channel"}, r
assert all(x["broad"] != "drawmer" for x in r), r  # already in list
assert len({x["term"].lower() for x in r}) == len(r)
assert suggest_terms([], titles, term_for=fake) == []
assert suggest_terms(["ssl"], [], term_for=fake) == []
print("unit ok", len(r))

import dashboard
app = dashboard.app
c = app.test_client()
with c.session_transaction() as s:
    s["authenticated"] = True
bad = 0
for rule in app.url_map.iter_rules():
    if "GET" not in rule.methods or rule.arguments:
        continue
    p = rule.rule
    if p.startswith("/static") or any(k in p for k in ("logout", "login", "export", "download", "stream", "events")):
        continue
    resp = c.get(p, headers={"X-GearScout-Test": "1"})
    ok = resp.status_code in (200, 302) if p != "/telex" else resp.status_code == 200
    print(p, resp.status_code, "" if ok else "<-- BAD")
    bad += 0 if ok else 1
assert c.get("/telex", headers={"X-GearScout-Test": "1"}).status_code == 200
print("pages done, bad =", bad)
