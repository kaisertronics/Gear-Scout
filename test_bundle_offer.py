"""Auto-bundle lowball: one seller with 3+ shopping-list items -> one lot offer."""
from scrapers import shopping_list as S


def m(title, unit, seller="Joe", source="eBay", ship=0.0, pieces=1, url=None):
    return {"title": title, "source": source, "url": url or title, "unit": unit, "ship": ship, "seller": seller,
            "pieces": pieces, "auction": False, "delivered": unit + ship}


def it(key, qty, used, matches):
    return {"key": key, "name": key.upper(), "qty": qty, "used_unit": used, "by_seller": matches}


def items():
    return [it("a", 2, 50, [m("Hosa A", 40, pieces=2)]), it("b", 1, 80, [m("Hosa B", 70)]), it("c", 3, 30, [m("Hosa C", 25)]),
            it("d", 1, 100, [m("Other D", 90, seller="Sue")])]


def test_three_items_one_seller_builds_an_offer():
    out = S.find_bundles(items(), {})
    assert len(out) == 1 and out[0]["seller"] == "Joe" and len(out[0]["lines"]) == 3
    b = out[0]
    assert b["ask"] == 80 + 70 + 25 and b["value"] == 100 + 80 + 30
    assert b["offer"] == 160 and b["pct_under_value"] == 24 and b["offer"] >= 0.7 * b["ask"] and b["offer"] <= b["ask"]
    assert "$160" in b["message"] and "Seattle" in b["message"] and "Hosa B" in b["message"]


def test_floor_and_never_above_ask():
    its = [it("a", 1, 500, [m("A", 40)]), it("b", 1, 500, [m("B", 40)]), it("c", 1, 500, [m("C", 40)])]
    b = S.find_bundles(its, {})[0]
    assert b["offer"] <= b["ask"] * 0.9 + 5 and b["offer"] >= 0.7 * b["ask"] - 5


def test_bought_items_dont_count_and_sources_dont_mix():
    assert S.find_bundles(items(), {"a": 2}) == []                      # only 2 still needed from Joe
    mixed = items()
    mixed[2]["by_seller"] = [m("Hosa C", 25, source="Reverb")]
    assert S.find_bundles(mixed, {}) == []                              # same name, different site = different seller


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
