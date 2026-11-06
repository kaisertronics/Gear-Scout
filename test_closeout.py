import sys
sys.path.insert(0, "/tmp/gsA")
from scrapers import closeout, motivated

cases = [
    ({"title": "Neumann U87", "description": "Our studio is closing, everything must go"}, "Studio closing"),
    ({"title": "Estate sale microphones lot"}, "Estate"),
    ({"title": "Recording studio liquidation - API 512c"}, "Liquidating"),
    ({"title": "Whole studio for sale, Seattle"}, "Whole studio / lot"),
    ({"title": "Rack lot: preamps and compressors"}, "Whole studio / lot"),
    ({"title": "Shure SM7B", "description": "Great condition, moving coil design"}, None),
    ({"title": "Neumann U87", "description": "Mint"}, None),
]
bad = 0
for r, want in cases:
    got = closeout.label(r)
    ok = got == want
    bad += not ok
    print("OK " if ok else "BAD", r["title"], "->", got)
# pushed up as motivated too
m = motivated.reason({"title": "Whole studio for sale"})
print("motivated:", m)
assert m == "Closeout", m

from dashboard import app
c = app.test_client()
with c.session_transaction() as s:
    s["authenticated"] = True
for p in ("/", "/mics"):
    resp = c.get(p, headers={"X-GearScout-Test": "1"})
    print(p, resp.status_code)
    assert resp.status_code == 200
assert not bad
print("ALL PASS")
