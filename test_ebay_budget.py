"""eBay daily budget: counted per job, paced across the day, fast lane protected."""
import tempfile
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from scrapers import ebay_api as E


def _at(h, m=0):
    return datetime(2026, 11, 3, h, m, tzinfo=ZoneInfo("America/Los_Angeles"))


def setup(tmp):
    E._CALLS_FILE = tmp + "/calls.json"
    E._PAUSE_FILE = tmp + "/pause.txt"


def test_counts_and_status():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(E, "_pt_now", lambda: _at(10)):
        setup(d)
        with mock.patch("requests.get", return_value="r"):
            assert E.ebay_get("u", job="fast_lane") == "r"
            E.ebay_get("u", job="priority_sweep")
        st = E.budget_status()
        assert st["used"] == 2 and st["left"] == E.DAILY_LIMIT - 2
        assert dict(st["jobs"])["Fast lane (newest listings)"] == 1


def test_job_paced_and_refused():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(E, "_pt_now", lambda: _at(0, 1)):
        setup(d)
        cap_now = int(E.JOB_SHARE["mic_sweep"] * E.DAILY_LIMIT * 0.10) + 1
        E._count_call("mic_sweep", cap_now)
        assert not E.budget_allows("mic_sweep")       # up-front 10% used at 00:01
        assert E.budget_allows("priority_sweep")      # other jobs unaffected
        with mock.patch.object(E, "_pt_now", lambda: _at(18)):
            assert E.budget_allows("mic_sweep")       # later in the day it may spend more
        try:
            E.ebay_get("u", job="mic_sweep")
            assert False
        except E.EbayBudgetError:
            pass


def test_fast_lane_reserved():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(E, "_pt_now", lambda: _at(12)):
        setup(d)
        E._count_call("priority_sweep", E.DAILY_LIMIT - 100)   # 12 h of fast lane left needs 360
        assert not E.budget_allows("priority_sweep")
        assert E.budget_allows("live_search") is False         # reserve holds for all but the fast lane
        assert E.budget_allows("fast_lane")


def test_new_day_resets_and_pause_counts_as_used():
    with tempfile.TemporaryDirectory() as d:
        setup(d)
        with mock.patch.object(E, "_pt_now", lambda: _at(9)):
            E._count_call("lookups", 5)
        nxt = lambda: datetime(2026, 11, 4, 0, 10, tzinfo=ZoneInfo("America/Los_Angeles"))
        with mock.patch.object(E, "_pt_now", nxt):
            assert E.budget_status()["used"] == 0
        open(E._PAUSE_FILE, "w").write("2999-01-01T00:00:00+00:00")
        with mock.patch.object(E, "_pt_now", lambda: _at(9)):
            st = E.budget_status()
            assert st["left"] == 0 and st["paused_until"]


def test_scrape_returns_failure_not_exception_when_refused():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(E, "_pt_now", lambda: _at(0, 1)):
        setup(d)
        E.TERM_JOBS["hosa dtp-802"] = "sibling_sweep"
        E._count_call("sibling_sweep", 100)
        with mock.patch.object(E, "_get_token", return_value="t"):
            r = E.scrape_ebay_api({"name": "eBay"}, ["hosa dtp-802"], {"client_id": "a", "client_secret": "b"})
        assert not r.success and r.blocked and "budget" in r.error


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
