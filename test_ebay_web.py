"""eBay web-search backup: parsing, 3 s spacing, block pause, term priority, hook in scrape_ebay_api."""
import tempfile
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from scrapers import ebay_api as E, ebay_web as W

HTML = """<html><title>mic</title><ul>
<li class="s-card"><div class="s-card__title">Neumann U87 Ai Microphone</div><span class="s-card__price">$2,100.00</span>
<a href="https://www.ebay.com/itm/123456789012?hash=abc&amp;x=1">l</a><img src="http://i/x.jpg"></li>
<li class="s-card"><div class="s-card__title">Shop on eBay</div><a href="https://www.ebay.com/itm/123456">l</a></li>
</ul></html>"""


class R:
    def __init__(self, text="", code=200):
        self.text, self.status_code = text, code


def fresh(tmp):
    W._BLOCK_FILE = tmp + "/blocked.txt"
    E._PAUSE_FILE = tmp + "/pause.txt"
    E._CALLS_FILE = tmp + "/calls.json"
    W._last_request = 0.0
    W._fast_i = 0
    for q in W._recent.values():
        q.clear()
    W.SPACING_S = 0.0


def test_parse_and_url():
    ls = W.parse(HTML, "eBay", None)
    assert len(ls) == 1 and ls[0].url == "https://www.ebay.com/itm/123456789012" and ls[0].listing_id == "123456789012"
    assert "LH_BIN=1" in W.search_url("hosa dtp-802") and "_sop=10" in W.search_url("x") and "LH_PrefLoc=1" in W.search_url("x")


def test_block_pauses_30_minutes():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        with mock.patch("requests.get", return_value=R("<title>Pardon Our Interruption</title>")) as g:
            assert W.search("u87", "eBay", None) is None
            assert W.blocked_until() is not None
            assert W.search("u87", "eBay", None) is None and g.call_count == 1  # no second request while blocked


def test_http_403_blocks():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        with mock.patch("requests.get", return_value=R("", 403)):
            assert W.search("u87", "eBay", None) is None and W.blocked_until()


def test_spacing_three_seconds():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        W.SPACING_S = 3.0
        sleeps = []
        with mock.patch("requests.get", return_value=R(HTML)), mock.patch("time.sleep", sleeps.append):
            W._fetch("a")
            W._fetch("b")
        assert len(sleeps) == 1 and 2.0 < sleeps[0] <= 3.0


def test_fast_lane_fallback_rotates_terms():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        with mock.patch("requests.get", return_value=R(HTML)) as g:
            r = W.fallback("eBay — Pro Audio", "m", newest=100)
        assert r.success and len(r.listings) == 1 and g.call_count == W.FAST_PER_TICK
        assert "nkw=microphone" in g.call_args_list[0][0][0]


def test_priority_and_caps():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        with mock.patch("requests.get", return_value=R(HTML)) as g:
            assert W.fallback("eBay", "m", term="u87", job="mic_sweep") is None      # mic model terms: no backup
            assert W.fallback("eBay", "m", term="hosa dtp-802", job="sibling_sweep") is None
            assert g.call_count == 0
            ok = [W.fallback("eBay", "m", term="hosa dtp-802", job="priority_sweep") for _ in range(8)]
            assert sum(1 for x in ok if x) == W.CAPS["shop"]
            assert W.fallback("eBay", "m", term="vanguard v4", job="sibling_sweep") is not None  # starred maker
            assert W.fallback("eBay", "m", term="vanguard v4", job="sibling_sweep") is not None
            assert W.fallback("eBay", "m", term="vanguard v4", job="sibling_sweep") is None


def test_scrape_ebay_api_uses_backup_when_paused_and_when_budget_spent():
    with tempfile.TemporaryDirectory() as d:
        fresh(d)
        E.TERM_JOBS["hosa dtp-802"] = "priority_sweep"
        src = {"name": "eBay — Pro Audio"}
        cfg = {"client_id": "a", "client_secret": "b"}
        with mock.patch.object(E, "ebay_paused_until", lambda: datetime(2030, 1, 1, tzinfo=ZoneInfo("UTC"))), \
                mock.patch("requests.get", return_value=R(HTML.replace("Neumann U87 Ai Microphone", "Hosa DTP-802 DB25 cable"))):
            r = E.scrape_ebay_api(src, ["hosa dtp-802"], cfg)
            assert r.success and [l.title for l in r.listings] == ["Hosa DTP-802 DB25 cable"]
            r = E.scrape_ebay_api(src, [], cfg, newest=100)
            assert r.success and r.listings
        # budget spent (not paused): the API call is refused, the backup answers
        with mock.patch.object(E, "ebay_paused_until", lambda: None), mock.patch.object(E, "_get_token", lambda *a: "t"), \
                mock.patch.object(E, "budget_allows", lambda job, limit=0: False), \
                mock.patch("requests.get", return_value=R(HTML)):
            r = E.scrape_ebay_api(src, [], cfg, newest=100)
            assert r.success and r.listings
        # blocked backup: the original "paused" failure is returned
        with mock.patch.object(E, "ebay_paused_until", lambda: datetime(2030, 1, 1, tzinfo=ZoneInfo("UTC"))), \
                mock.patch("requests.get", return_value=R("", 403)):
            W._recent["shop"].clear()
            r = E.scrape_ebay_api(src, ["hosa dtp-802"], cfg)
            assert not r.success and r.blocked


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
