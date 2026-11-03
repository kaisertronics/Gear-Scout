from datetime import datetime, timezone

from scrapers.best_month import best_month


def d(y, m, day=15):
    return datetime(y, m, day, tzinfo=timezone.utc)


def mk(spec):
    return [(d(y, m), p) for (y, m, p, n) in spec for _ in range(n)]


good = mk([(2025, 3, 900, 5), (2025, 7, 1000, 6), (2025, 11, 1000, 5), (2026, 2, 1100, 5)])
r = best_month(good)
assert r and r["name"] == "March" and r["pct"] == 10, r
assert r["line"] == "Cheapest month to buy: March (about 10% under average)", r
assert best_month([]) is None
# too few months with enough samples
assert best_month(mk([(2025, 3, 900, 5), (2025, 7, 1000, 5), (2026, 2, 1100, 4)])) is None
# short span
assert best_month(mk([(2026, 1, 900, 5), (2026, 2, 1000, 5), (2026, 3, 1100, 5)])) is None
# no meaningful gap
assert best_month(mk([(2025, 3, 995, 5), (2025, 7, 1000, 5), (2026, 2, 1000, 5)])) is None

from scrapers import best_month as bm
import dashboard
print("live models with a result:", bm.best_months(dashboard.load_config_raw()))
app = dashboard.app
app.config["TESTING"] = True
c = app.test_client()
with c.session_transaction() as s:
    s["authenticated"] = True
resp = c.get("/mics", headers={"X-GearScout-Test": "1"})
print("mics status", resp.status_code, "has line:", b"Cheapest month to buy" in resp.data)
assert resp.status_code == 200
print("OK")
