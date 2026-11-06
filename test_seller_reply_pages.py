import sys
sys.path.insert(0, "/tmp/gs")
import dashboard
c = dashboard.app.test_client()
with c.session_transaction() as s:
    s["authenticated"] = True
bad = 0
for p in ["/", "/mics", "/offers"]:
    r = c.get(p, headers={"X-GearScout-Test": "1"})
    print(p, r.status_code)
    if r.status_code != 200:
        bad += 1
print("BAD", bad)
