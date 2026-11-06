import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers import scarcity

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


check("classify 1", scarcity.classify(1) == "very rare")
check("classify 2", scarcity.classify(2) == "very rare")
check("classify 3", scarcity.classify(3) == "rare")
check("classify 4", scarcity.classify(4) == "rare")
check("classify 5", scarcity.classify(5) == "uncommon")
check("classify 14", scarcity.classify(14) == "uncommon")
check("classify 15", scarcity.classify(15) == "common")
check("none title", scarcity.scarcity(None) is None)
check("junk title", scarcity.scarcity("") is None)

idx = scarcity.index()
check("index is dict", isinstance(idx, dict) and len(idx) > 0)
t = time.time()
scarcity.index()
check("cached fast", time.time() - t < 0.05)

rare = [k for k, n in idx.items() if n <= 4]
common = [k for k, n in idx.items() if n >= 15]
print("keys", len(idx), "rare", len(rare), "common", len(common))
check("some common keys", len(common) > 0)

# Render pages
from dashboard import app
pages = ["/", "/mics", "/deals", "/shopping", "/search", "/saved"]
chips = 0
with app.test_client() as c:
    with c.session_transaction() as s:
        s["authenticated"] = True
    for p in pages:
        r = c.get(p, headers={"X-GearScout-Test": "1"})
        n = len(re.findall(r"(?:Very rare|Rare): only \d+ ads? seen", r.get_data(as_text=True)))
        print(p, r.status_code, "chips", n)
        check("200 " + p, r.status_code == 200)
        chips += n
check("chip on at least one card", chips > 0)
print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
