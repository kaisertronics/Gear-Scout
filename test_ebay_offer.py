from unittest import mock

from scrapers import ebay_offer as eo
from scrapers import ebay_api

SECRET_TOK = "v^1.1#i^1#SECRETTOKENVALUE123"

# id parsing
assert eo.listing_id_from_url("https://www.ebay.com/itm/123456789012") == "123456789012"
assert eo.listing_id_from_url("https://www.ebay.com/itm/Neumann-U87/123456789012?hash=x") == "123456789012"
assert eo.listing_id_from_url("https://www.ebay.com/sch/i.html?_nkw=1") is None
assert eo.listing_id_from_url(None) is None

# xml
x = eo.build_xml("123", 99.5)
assert "<ItemID>123</ItemID>" in x and '<Amount currencyID="USD">99.50</Amount>' in x
assert "<Action>BestOffer</Action>" in x and "<Quantity>1</Quantity>" in x

# response parsing
assert eo.parse_response("<R><Ack>Success</Ack></R>") == (True, "")
assert eo.parse_response("<R><Ack>Warning</Ack></R>")[0] is True
ok, why = eo.parse_response("<R><Ack>Failure</Ack><Errors><ShortMessage>x</ShortMessage>"
                            "<LongMessage>Offer too low. token %s</LongMessage></Errors></R>" % SECRET_TOK, SECRET_TOK)
assert not ok and "Offer too low" in why and "SECRETTOKEN" not in why, why


class Boom(Exception):
    pass


def no_post(*a, **k):
    raise Boom("network must not be touched")


connected = {"client_id": "cid123456", "client_secret": "sec123456", "user_refresh_token": "rt1234567", "runame": "R-U"}

# dry run sends nothing
with mock.patch.object(eo, "_cfg", return_value=connected), mock.patch.object(eo.requests, "post", no_post):
    r = eo.send_offer("https://www.ebay.com/itm/555", 80, dry_run=True, asking=100)
    assert r["ok"] and r["dry_run"] and "<ItemID>555</ItemID>" in r["payload_preview"], r
    assert r["copy_msg"]
    # amount above asking rejected
    r = eo.send_offer("https://www.ebay.com/itm/555", 120, dry_run=False, asking=100)
    assert not r["ok"] and "higher" in r["message"]
    r = eo.send_offer("https://www.ebay.com/itm/555", 0)
    assert not r["ok"]
    r = eo.send_offer("https://reverb.com/item/1", 50)
    assert not r["ok"] and "eBay item" in r["message"]

# no token / not connected
with mock.patch.object(eo, "_cfg", return_value={"client_id": "a", "client_secret": "b"}), \
        mock.patch.object(eo.requests, "post", no_post):
    eo.clear_cache()
    r = eo.send_offer("https://www.ebay.com/itm/555", 80, asking=100)
    assert not r["ok"] and "not connected" in r["message"], r
    assert not eo.is_connected()
    r = eo.send_offer("https://www.ebay.com/itm/555", 80, dry_run=True, asking=100)
    assert not r["ok"]


class Resp:
    def __init__(self, text, code=200, js=None):
        self.text, self.status_code, self._j = text, code, js or {}

    def json(self):
        return self._j


def fake_post(url, **k):
    if url == eo.TOKEN_URL:
        return Resp("", 200, {"access_token": SECRET_TOK, "expires_in": 7200})
    assert k["headers"]["X-EBAY-API-CALL-NAME"] == "PlaceOffer"
    assert k["headers"]["X-EBAY-API-IAF-TOKEN"] == SECRET_TOK
    return fake_post.reply


logged = []
fake_offer_log = mock.Mock()
fake_offer_log.add = lambda *a: logged.append(a)

with mock.patch.object(eo, "_cfg", return_value=connected), mock.patch.object(eo.requests, "post", fake_post), \
        mock.patch.dict("sys.modules", {}):
    import scrapers
    with mock.patch.object(scrapers, "offer_log", fake_offer_log, create=True):
        eo.clear_cache()
        fake_post.reply = Resp("<R><Ack>Success</Ack></R>")
        r = eo.send_offer("https://www.ebay.com/itm/555", 80, title="T", seller="s", asking=100)
        assert r["ok"] and r["message"] == "Offer sent", r
        assert logged and logged[0][5] == 80.0, logged
        fake_post.reply = Resp("<R><Ack>Failure</Ack><Errors><LongMessage>Bad offer %s</LongMessage></Errors></R>" % SECRET_TOK)
        r = eo.send_offer("https://www.ebay.com/itm/555", 80, asking=100)
        assert not r["ok"] and r["message"].startswith("eBay said: Bad offer") and "SECRETTOKEN" not in str(r), r
        assert r["copy_msg"]

# best offer flag
assert ebay_api.best_offer_of({"buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]}) is True
assert ebay_api.best_offer_of({"buyingOptions": ["FIXED_PRICE"]}) is False
assert ebay_api.best_offer_of({}) is None

# consent url
with mock.patch.object(eo, "_cfg", return_value=connected):
    u = eo.consent_url("st")
    assert u.startswith(eo.AUTH_URL) and "redirect_uri=R-U" in u and "state=st" in u
with mock.patch.object(eo, "_cfg", return_value={"client_id": "a"}):
    assert eo.consent_url("st") is None

print("ebay offer tests passed")
