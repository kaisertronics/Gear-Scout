"""Pre-sold price drops: a model's asking prices falling across several sellers."""
from datetime import datetime, timedelta, timezone
from scrapers import price_windows as W

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def row(price, days_ago, seller, n=[0], title="Neumann U87 mic"):
    n[0] += 1
    return {"title": f"{title} #{n[0]}", "price": f"${price}", "url": f"u{n[0]}", "seller": seller, "source_name": "Reverb",
            "first_seen": (NOW - timedelta(days=days_ago)).isoformat()}


def key(t):
    return "neumann:u87" if "u87" in t.lower() else None


def base_rows():
    return [row(1800 + i * 10, 30 + i * 4, f"b{i}") for i in range(9)]


def test_window_found():
    rows = base_rows() + [row(1500, 2, "s1"), row(1550, 4, "s2"), row(1600, 6, "s3"), row(1580, 8, "s4"), row(1560, 9, "s5")]
    out = W.pick(rows, NOW, key)
    assert len(out) == 1 and out[0]["window"]["drop"] >= 10 and out[0]["window"]["sellers"] == 5
    assert out[0]["price"] == "$1500" and "Price window" in out[0]["finder_note"] and out[0]["window"]["label"] == "Neumann U87"


def test_needs_several_sellers_and_enough_ads():
    assert W.pick(base_rows() + [row(1500, 2, "s1"), row(1550, 4, "s1"), row(1600, 6, "s1"), row(1580, 8, "s1"), row(1560, 9, "s1")], NOW, key) == []  # one seller
    assert W.pick(base_rows() + [row(1500, 2, "s1"), row(1550, 4, "s2"), row(1600, 6, "s3"), row(1580, 8, "s4")], NOW, key) == []   # 4 ads
    assert W.pick(base_rows()[:4] + [row(1500, 2, "s1"), row(1550, 4, "s2"), row(1600, 6, "s3"), row(1580, 8, "s4"), row(1560, 9, "s5")], NOW, key) == []  # thin baseline


def test_small_or_huge_moves_ignored():
    assert W.pick(base_rows() + [row(1780, 2, "s1"), row(1790, 4, "s2"), row(1800, 6, "s3"), row(1770, 8, "s4"), row(1760, 9, "s5")], NOW, key) == []
    assert W.pick(base_rows() + [row(700, 2, "s1"), row(800, 4, "s2"), row(900, 6, "s3"), row(750, 8, "s4"), row(760, 9, "s5")], NOW, key) == []


def test_skips_cad_and_auctions():
    rows = base_rows() + [dict(row(1500, 2, "s1"), price="C$1500"), row(1550, 4, "s2"), row(1600, 6, "s3"), dict(row(1580, 8, "s4"), description="Auction"), row(1560, 9, "s5")]
    assert W.pick(rows, NOW, key) == []


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
