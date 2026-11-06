from scrapers import bundle_shipping as b


def m(source, seller, unit, ship, title="t", pieces=1):
    return {"source": source, "seller": seller, "unit": unit, "ship": ship, "delivered": unit + ship,
            "title": title, "url": "http://x/" + title, "pieces": pieces, "auction": False}


def item(key, ms, qty=1):
    return {"key": key, "name": key, "qty": qty, "by_seller": ms}


def check(c, msg):
    assert c, msg
    print("ok", msg)


assert b.estimate_saved([10, 6]) == 6.0
check(b.estimate_saved([10, 0]) == b.FLAT_EXTRA, "unknown shipping uses flat estimate")
check(b.estimate_saved([10]) == 0.0, "single listing saves nothing")
check(b.estimate_saved([None, "x"]) == b.FLAT_EXTRA, "junk input safe")

items = [item("a", [m("eBay", "Bob", 20, 8, "a1"), m("eBay", "Zed", 25, 5, "a2")]),
         item("b", [m("eBay", "bob ", 30, 6, "b1")]),
         item("c", [m("Reverb", "Bob", 30, 6, "c1")]),
         item("d", [m("eBay", "Bob", 9, 3, "d1")])]
g = b.find_groups(items, {})
check(len(g) == 1, "one same-seller group (Reverb Bob separate)")
check(len(g[0]["lines"]) == 3 and g[0]["source"] == "eBay", "three eBay Bob items")
check(g[0]["saved"] == 9.0, "saved = 6 + 3")
check(g[0]["total"] == 20 + 8 + 30 + 6 + 9 + 3, "total")
check(len(b.find_groups(items, {"a": 1, "d": 1})) == 0, "bought items excluded, group disappears")
check(b.find_groups([], {}) == [] and b.find_groups(None) == [], "empty safe")
print("ALL OK")
