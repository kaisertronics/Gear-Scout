"""Deal calibration report: matches purchases to earlier-seen listings."""
from scrapers import deal_calibration as D


def purchases():
    return [{"item": "Neumann U87", "paid": 1000, "date": "2026-10-10", "used": 1400},
            {"item": "Warm WA-47", "paid": 400, "date": "2026-10-12", "used": 500},
            {"item": "Sony C-800G", "paid": 3000, "date": "2026-10-20", "used": 4000},
            {"item": "Undated thing", "paid": 50, "date": "", "used": 60}]


def listings():
    return [{"title": "Neumann U87 Ai mic", "price": 1050, "first_seen": "2026-10-09T00:00:00+00:00"},
            {"title": "Neumann U87 Ai mic", "price": 1050, "first_seen": "2026-10-10T12:00:00+00:00"},  # after purchase
            {"title": "Warm Audio WA-47 tube", "price": 900, "first_seen": "2026-10-01T00:00:00+00:00"},  # price too far
            {"title": "Sony C-800G", "price": 2900, "first_seen": "2026-10-18T00:00:00+00:00"}]


def test_lead_and_discount():
    r = D.calibrate(purchases(), listings())
    assert r["m"] == 3 and r["n"] == 2
    by = {x["item"]: x for x in r["rows"]}
    assert by["Neumann U87"]["lead_h"] == 24 and by["Neumann U87"]["discount_pct"] == 29
    assert by["Warm WA-47"]["lead_h"] is None
    assert by["Sony C-800G"]["lead_h"] == 48
    assert r["median_lead"] == 36 and "2 of 3" in r["text"]


def test_too_few_purchases_returns_none():
    assert D.calibrate(purchases()[:2], listings()) is None
    assert D.calibrate([], []) is None


def test_report_never_raises_and_caches(monkeypatch=None):
    calls = []
    orig = D._listings
    D._listings = lambda: calls.append(1) or listings()
    try:
        D._cache.update(key=None, at=0.0, out=None)
        items = [dict(p) for p in purchases()]
        for i in items:
            i["date"] = i["date"] or None
        r1 = D.report(items)
        r2 = D.report(items)
        assert r1 == r2 and len(calls) == 1
        D._listings = lambda: 1 / 0
        D._cache.update(key=None, at=0.0, out=None)
        assert D.report(items) is None
    finally:
        D._listings = orig


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
