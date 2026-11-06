"""Fixer-upper finder: repair-wording ads on valuable gear, with profit-if-fixed math."""
from scrapers import fixer_upper as F

V = {"la-2a": 3000, "u87": 1500, "m55": 600, "sm57": 90}


def val(t):
    t = t.lower().replace(" ", "")
    return next((v for k, v in V.items() if k.replace(" ", "") in t), None)


def row(title, price, url="u", **kw):
    return {"title": title, "price": f"${price}", "url": url, **kw}


def test_repair_cost_wording():
    assert F.repair_cost("LA-2A clone needs tubes") == ("tubes", 150)
    assert F.repair_cost("Neumann U87 capsule damaged") == ("capsule / re-skin", 350)
    assert F.repair_cost("Beyer M55 not working") == ("general repair", 300)
    assert F.repair_cost("Beyer M55 as-is") == ("check-over", 120)
    assert F.repair_cost("Beyer M55 mint") is None
    assert F.repair_cost("Neve 1073", "has no power, for parts") == ("power supply", 200)


def test_strip_repair_leaves_the_model():
    assert "LA-2A" in F.strip_repair("LA-2A compressor needs tubes") and "tubes" not in F.strip_repair("LA-2A compressor needs tubes")
    assert F.strip_repair("Beyer M55 for parts or repair").lower().startswith("beyer m55")


def test_profit_math():
    a = F.assess("Teletronix LA-2A needs tubes", None, 1200, val)
    assert a == {"used": 3000, "cost": 1200, "repair": 150, "repair_label": "tubes", "profit": 1650, "pct": 55}


def test_thresholds():
    assert F.assess("Shure SM57 not working", None, 20, val) is None           # low value: not worth a repair slot
    assert F.assess("Neumann U87 not working", None, 1400, val) is None        # no real margin
    assert F.assess("Neumann U87 not working", None, 1000, val) is None        # ask > 60% of working value
    assert F.assess("Neumann U87 not working", None, 700, val)["profit"] == 500
    assert F.assess("Neumann U87", None, 700, val) is None                     # no repair wording
    assert F.assess("Some random amp not working", None, 10, val) is None


def test_pick_order_and_skips():
    rows = [row("Neumann U87 not working", 700, "a"), row("LA-2A needs tubes", 1200, "b"),
            row("LA-2A needs tubes", 1200, "b"), row("LA-2A needs tubes", 1200, "c", description="Auction ends"),
            {"title": "LA-2A needs tubes", "price": "C$900", "url": "d"}]
    out = F.pick(rows, val)
    assert [r["url"] for r in out] == ["b", "a"] and "profit if fixed" in out[0]["finder_note"]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
