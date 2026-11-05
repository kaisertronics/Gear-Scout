import tempfile
from pathlib import Path
from scrapers import term_silence as ts

ts.STATE_PATH = Path(tempfile.mkdtemp()) / "term_silence.json"

for n in (8, 6, 7):
    ts.record("peluso p-12", n, "ebay+fast")
for _ in range(5):
    ts.record("peluso p-12", 0, "ebay+fast")
assert ts.silent_terms() == [], "5 zeros is not enough"
ts.record("peluso p-12", 0, "ebay+fast")
assert ts.silent_terms() == ["peluso p-12 (ebay+fast)"], ts.silent_terms()
ts.record("peluso p-12", 3, "ebay+fast")
assert ts.silent_terms() == [], "recovered"

for _ in range(10):
    ts.record("never matched", 0)
assert "never matched" not in ts.silent_terms()
for n in (2, 1, 0, 0):
    ts.record("small", n)
for _ in range(8):
    ts.record("small", 0)
assert "small" not in ts.silent_terms(), "peak under 5"

ts.STATE_PATH = Path("/proc/nope/x.json")
ts.record("x", 1)  # must not raise
print("OK")
