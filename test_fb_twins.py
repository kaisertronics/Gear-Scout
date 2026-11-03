import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.fb_twins import hide_twins

FB = "https://www.facebook.com/marketplace/item/"


def ad(n, title="Shure SM7B dynamic microphone", price="$300", seller="bob", day=1, **kw):
    d = {"url": f"{FB}{n}/", "title": title, "price": price, "seller": seller,
         "location": "Austin, TX", "first_seen": f"2026-11-0{day}T10:00:00"}
    d.update(kw)
    return d


def urls(ls):
    return [l["url"] for l in ls]


def test_twins_collapse_to_newest():
    a, b = ad(1, day=1), ad(2, day=3)
    assert urls(hide_twins([b, a])) == [b["url"]]


def test_near_identical_title_and_price_within_few_dollars():
    a, b = ad(1, day=1), ad(2, title="Shure SM7B dynamic microphone!", price="$297", day=2)
    assert urls(hide_twins([b, a])) == [b["url"]]


def test_same_photo_different_seller_name_missing():
    a = ad(1, seller="", location="", image_url="https://x/img/abc.jpg?t=1", day=1)
    b = ad(2, seller="", location="", image_url="https://y/img/abc.jpg?t=2", day=2)
    assert urls(hide_twins([b, a])) == [b["url"]]


def test_different_price_not_twins():
    assert len(hide_twins([ad(1, price="$300"), ad(2, price="$350", day=2)])) == 2


def test_different_seller_not_twins():
    assert len(hide_twins([ad(1, seller="bob"), ad(2, seller="amy", day=2)])) == 2


def test_far_apart_not_twins():
    a, b = ad(1), ad(2)
    b["first_seen"] = "2026-11-20T10:00:00"
    assert len(hide_twins([b, a])) == 2


def test_different_title_not_twins():
    assert len(hide_twins([ad(1), ad(2, title="Neumann U87 large diaphragm condenser", day=2)])) == 2


def test_non_facebook_untouched():
    a = ad(1)
    b = ad(2, day=2)
    for l in (a, b):
        l["url"] = l["url"].replace("www.facebook.com/marketplace/item", "reverb.com/item")
    assert len(hide_twins([b, a])) == 2


def test_starred_never_hidden():
    a, b = ad(1, favorite=True), ad(2, day=2)
    assert len(hide_twins([b, a])) == 2


def test_input_not_mutated_and_order_kept():
    a, b, c = ad(1, day=1), ad(2, title="Other Thing Entirely Here", day=2), ad(3, day=3)
    src = [c, b, a]
    out = hide_twins(src)
    assert src == [c, b, a] and urls(out) == [c["url"], b["url"]]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
