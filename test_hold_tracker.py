import sys
sys.path.insert(0, "/tmp/gs")
sys.path.insert(0, ".")
from scrapers.hold_tracker import hold_models


from datetime import datetime, timedelta, timezone


def pts(model, prices):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"model": model, "label": model.upper(), "price": p,
             "at": (base + timedelta(days=40 * i)).isoformat()} for i, p in enumerate(prices)]


rising = pts("u87", [2000, 2050, 2200, 2300])
flat = pts("sm7b", [300, 300, 305, 300])
falling = pts("c414", [1000, 900, 800, 700])
thin = pts("km84", [500, 600])
same_day = [{"model": "x", "label": "X", "price": p, "at": "2026-03-01T00:00:00+00:00"} for p in (1, 2, 3, 4)]
junk = [{"model": "j", "price": "abc", "at": "bad"}, {"model": "", "price": 5, "at": "2026-01-01"}]

CASES = [
    ("rising listed", [r["model"] for r in hold_models(rising)], ["u87"]),
    ("flat listed", [r["model"] for r in hold_models(flat)], ["sm7b"]),
    ("falling dropped", hold_models(falling), []),
    ("too few dropped", hold_models(thin), []),
    ("no span dropped", hold_models(same_day), []),
    ("junk ok", hold_models(junk), []),
    ("empty ok", hold_models([]), []),
    ("sorted best first", [r["model"] for r in hold_models(flat + rising)], ["u87", "sm7b"]),
    ("value is newer median", hold_models(rising)[0]["value"], 2250),
]
bad = 0
for name, got, want in CASES:
    flag = "ok  " if got == want else "FAIL"
    if got != want:
        bad += 1
    print(flag, name, got)
print("failures:", bad)
