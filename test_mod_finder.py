"""Mod and upgrade value finder: modded budget mics priced like stock ones."""
from scrapers import mod_finder as M

V = {"apex460": 350, "apex210": 250, "u87": 1500}


def val(t):
    t = t.lower().replace(" ", "")
    return next((v for k, v in V.items() if k in t), None)


def row(title, price, url="u", **kw):
    return {"title": title, "price": f"${price}", "url": url, **kw}


def test_parts_need_a_mod_word():
    assert M.parts_in("Apex 460 modded with Peluso C12 capsule and Lundahl transformer") == [("Peluso capsule", 300), ("Lundahl transformer", 200)]
    assert M.parts_in("Cascade Knuckle Head ribbon with Lundahl transformer") == []   # stock spec, not a mod
    assert M.parts_in("Apex 210 mic, transformer upgraded to Lundahl") == [("Lundahl transformer", 200)]
    assert M.parts_in("Apex 460 mic") == []


def test_body_title_drops_mod_wording():
    assert M.body_title("Apex 460 modded with Peluso C12 capsule and Lundahl transformer").lower().startswith("apex 460")


def test_profit_math():
    a = M.assess("Apex 460 modded with Peluso C12 capsule and Lundahl transformer", None, 429, val)
    assert a == {"body": 350, "parts": 500, "worth": 850, "cost": 429, "pct": 50, "labels": ["Peluso capsule", "Lundahl transformer"]}


def test_thresholds():
    assert M.assess("Apex 460 modded Lundahl transformer", None, 560, val) is None       # 15%? worth 550, no gap
    assert M.assess("Apex 460 modded Lundahl transformer", None, 100, val) is None       # under 60% of stock body
    assert M.assess("Lundahl transformer only, upgraded mic part", None, 100, val) is None
    assert M.assess("Random modded mic Lundahl", None, 100, val) is None                 # unknown body value
    assert M.assess("Neumann U87 modded Lundahl", None, 1500, val) is None               # no gap


def test_pick_order_and_skips():
    rows = [row("Apex 460 modded with Peluso C12 capsule and Lundahl transformer", 429, "a"),
            row("Apex 460 modded with Peluso C12 capsule and Lundahl transformer", 429, "a"),
            row("Apex 210 modded Lundahl transformer", 300, "b", description="Auction ends"),
            {"title": "Apex 460 modded Lundahl transformer", "price": "C$300", "url": "c"},
            row("Apex 210 mic upgraded Jensen transformer", 300, "d")]
    out = M.pick(rows, val)
    assert [r["url"] for r in out] == ["a", "d"] and "Modded" in out[0]["finder_note"]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
