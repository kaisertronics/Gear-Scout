"""Lot hunter: multi-item ads priced well under the sum of the parts."""
from scrapers import lot_hunter as H

V = {"sm57": 100, "sm58": 100, "414": 600, "vc528": 400}


def val(name):
    n = name.lower().replace(" ", "")
    return next((v for k, v in V.items() if k in n), None)


def row(title, price, url="u", **kw):
    return {"title": title, "price": f"${price}", "url": url, **kw}


def test_mixed_lot_under_parts():
    a = H.assess("Lot: SM57, SM58, AKG 414 $700", None, 700, val)
    assert a and a["total"] == 800 and a["units"] == 3 and a["pct"] == 12


def test_quantity_lot_of_one_model():
    a = H.assess("CAPI VC528 x8 modules", None, 2000, val)
    assert a and a["units"] == 8 and a["total"] == 3200 and a["pct"] == 38 and a["saved"] == 1200


def test_pick_thresholds_and_order():
    rows = [row("CAPI VC528 x8", 2000, "a"), row("Lot of SM57, SM58, 414", 780, "b"),   # 2.5% under: not enough
            row("Lot of SM57, SM58, 414", 500, "c"), row("Shure SM57", 30, "d"), row("2x SM57", 150, "e")]
    out = H.pick(rows, val)
    assert [r["url"] for r in out] == ["a", "c"]                 # biggest dollar saving first
    assert "Lot parts" in out[0]["finder_note"] and "8x" in out[0]["finder_note"]


def test_too_good_auction_foreign_and_unpriced_dropped():
    rows = [row("CAPI VC528 x8", 300, "a"),                       # 90% under: only some of it is real
            row("CAPI VC528 x8", 2000, "b", description="Auction ends soon"),
            {"title": "CAPI VC528 x8", "price": "C$2000", "url": "c"}, {"title": "CAPI VC528 x8", "price": None, "url": "d"}]
    assert H.pick(rows, val) == []


def test_single_item_is_not_a_lot():
    assert H.assess("Neumann U87", None, 500, val) is None
    assert H.assess("", None, 500, val) is None


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
