"""Unit tests for scrapers/shop_fold.py. Run: python3 test_shop_fold.py"""
from scrapers.shop_fold import fold_shop_stock, is_shop_listing

VALUES = {"la-220": 400.0, "wa-87": 600.0}
key = lambda t: "la-220" if "LA-220" in t else ("wa-87" if "WA-87" in t else "other")
val = lambda t, d=None: VALUES.get(key(t))
price = lambda p: float(str(p).replace("$", "").replace(",", "")) if p else None


def L(title, p, cond, src="Reverb — All Used", **kw):
    return {"title": title, "price": p, "description": f"{cond} · text", "source_name": src, **kw}


def run(ls):
    return fold_shop_stock(ls, val, key, price)


def main():
    shop = L("Lauten Audio LA-220", "$350", "Brand New")
    shop2 = L("Lauten Audio LA-220 B", "$320", "B-Stock")
    cheap = L("Lauten Audio LA-220 cheap", "$290", "B-Stock")        # 27.5% under 400
    used = L("Lauten Audio LA-220 used", "$350", "Excellent")        # private used
    fav = L("Lauten Audio LA-220 fav", "$350", "Brand New", favorite=True)
    other = L("Neumann U87", "$900", "Brand New")                    # other brand
    ebay = L("Lauten Audio LA-220 ebay", "$350", "Brand New", src="eBay — Pro Audio")
    noval = L("Warm Audio WA-9 Mystery", "$100", "Brand New")        # no value, shop
    wa = L("Warm Audio WA-87 R2", "$500", "Brand New")
    kept, groups = run([shop, shop2, cheap, used, fav, other, ebay, noval, wa])
    assert kept == [cheap, used, fav, other, ebay], kept
    by = {g["key"]: g for g in groups}
    assert by["la-220"]["count"] == 2 and by["la-220"]["low"] == 320.0
    assert by["la-220"]["items"] == [shop2, shop]
    assert by["other"]["count"] == 1 and by["wa-87"]["low"] == 500.0
    assert groups[0]["key"] == "la-220"
    assert is_shop_listing(shop) and not is_shop_listing(used) and not is_shop_listing(ebay)
    assert run([]) == ([], [])
    print("OK")


main()
