"""Offline tests for the one-tap instant offer (no network, nothing sent)."""
from unittest import mock
from scrapers import instant_offer as io


def test_suggest():
    assert io.suggest(1000) == 850 and io.suggest(50) == 40 and io.suggest(0) is None and io.suggest("x") is None
    for a in (7, 33, 99, 250, 1234, 4999):
        s = io.suggest(a)
        assert s <= a and s >= 0.7 * a - 0.01, (a, s)
        assert s % 5 == 0 or a < 20


def test_channel():
    assert io.channel("https://reverb.com/item/123-x", 1) == "reverb"
    assert io.channel("https://reverb.com/item/123-x", 0) == "copy"
    assert io.channel("https://www.ebay.com/itm/1", 1, True) == "ebay"
    assert io.channel("https://www.ebay.com/itm/1", 1, False) == "copy"
    assert io.channel("https://craigslist.org/x") == "copy"


def test_link_and_actions():
    cfg = {"email": {"dashboard_url": "http://h:8420/"}}
    assert io.link(cfg, "https://a.b/c?d=1") == "http://h:8420/offer?url=https%3A%2F%2Fa.b%2Fc%3Fd%3D1"
    assert io.link({}, "u") == "" and io.push_actions({}, "u") is None
    assert io.push_actions(cfg, "u", 850)[0]["label"] == "Offer $850"


def test_push_has_actions():
    from scrapers import notify
    with mock.patch.object(notify.requests, "post") as p:
        p.return_value.status_code = 200
        notify.send_push({"notifications": {"ntfy_topic": "t"}}, "t", "m", actions=[{"action": "view", "label": "x", "url": "u"}])
        assert p.call_args.kwargs["json"]["actions"][0]["label"] == "x"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
