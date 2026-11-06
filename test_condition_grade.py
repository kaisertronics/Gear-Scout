import sys
sys.path.insert(0, "/tmp/gs")
from scrapers.condition_grade import grade

cases = [
    ("Neumann U87 mint condition", "", "Mint"),
    ("SM57", "Good condition, works great", "Good"),
    ("SM58", "heavy wear, scratched up", "Worn"),
    ("Mic for parts or repair", "not working", "For parts / repair"),
    ("Mic", "A mic for sale", None),
    (None, None, None),
]
bad = 0
for t, d, exp in cases:
    got = grade(t, d)
    ok = got == exp
    bad += not ok
    print("OK " if ok else "BAD", t, d, got, exp)

import dashboard
app = dashboard.app
c = app.test_client()
with c.session_transaction() as s:
    s["authenticated"] = True
rules = sorted(r.rule for r in app.url_map.iter_rules() if "GET" in r.methods and not r.arguments)
for r in rules:
    if r.startswith("/static") or "logout" in r or "export" in r:
        continue
    code = c.get(r, headers={"X-GearScout-Test": "1"}).status_code
    print(code, r)
    bad += code != 200
print("FAILS", bad)
