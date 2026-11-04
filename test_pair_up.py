"""Pair-up finder: two singles of one model that beat a used pair."""
from scrapers import pair_up as P

VAL = {"1176": 1000, "sm57": 90}
key = lambda t: next((k for k in VAL if k in t.lower().replace(" ", "")), None)
val = lambda t: VAL.get(key(t))


def r(title, price, url, src="Reverb — All Used", ship=0, seller=None):
    return {"title": title, "price": f"${price}", "url": url, "source_name": src, "shipping": ship, "seller": seller}


def test_pair_found_and_messages():
    rows = [r("Universal Audio 1176 LN", 900, "a"), r("UA 1176 Blue", 850, "b"), r("UA 1176 reissue", 1500, "c")]
    out = P.pick(rows, val, key)
    assert len(out) == 1
    p = out[0]
    assert {p["a"]["url"], p["b"]["url"]} == {"a", "b"}          # the two cheapest
    assert p["total"] == 1750 and p["used_pair"] == 2000 and p["pct"] == 12
    assert "pair" in p["a"]["finder_msg"].lower() and "$" in p["a"]["finder_msg"]


def test_not_a_deal_when_combined_is_near_pair_value():
    assert P.pick([r("1176 A", 1000, "a"), r("1176 B", 980, "b")], val, key) == []


def test_already_pairs_and_unwanted_skipped():
    rows = [r("1176 pair", 1500, "a"), r("1176 matched", 1000, "b"), r("Shure SM57", 40, "c"), r("Shure SM57", 40, "d")]
    assert P.pick(rows, val, key) == []


def test_local_ads_need_same_region():
    a = r("1176 comp", 700, "a", src="Craigslist — Seattle")
    b = r("1176 comp", 700, "b", src="Craigslist — Portland")
    assert P.pick([a, b], val, key) == []
    b2 = r("1176 comp", 700, "b", src="Craigslist — Seattle")
    assert len(P.pick([a, b2], val, key)) == 1
    assert P.pick([a, r("1176 comp", 700, "c")], val, key) == []   # shipped can't pair with local pickup


def test_mismatched_asks_and_accessories_dropped():
    assert P.pick([r("1176 A", 150, "a"), r("1176 B", 950, "b")], val, key) == []    # one is a part / different variant
    assert P.pick([r("1176 A", 300, "a"), r("1176 B", 320, "b")], val, key) == []    # 68% under: too good


def test_too_good_and_cheap_gear_dropped():
    assert P.pick([r("1176 A", 100, "a"), r("1176 B", 100, "b")], val, key) == []   # 90% under: scam/mix-up
    assert P.pick([r("Shure SM57", 40, "c"), r("Shure SM57", 40, "d")], val, key) == []


def test_same_seller_flag_and_shipping_counts():
    rows = [r("1176 A", 800, "a", ship=100, seller="bob"), r("1176 B", 800, "b", ship=0, seller="bob")]
    out = P.pick(rows, val, key)
    assert out and out[0]["same_seller"] and out[0]["total"] == 1700


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
