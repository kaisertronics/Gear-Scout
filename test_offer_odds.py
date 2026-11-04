import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers import offer_odds as oo


def o(src, pct, reply):
    return {"source": src, "asking": 100.0, "offered": 100.0 - pct, "reply": reply}


def main():
    assert oo.odds_stats([], "Reverb") is None
    assert oo.odds_stats(None, None) is None
    assert oo.chip(None) is None
    two = [o("Reverb", 15, "accepted"), o("Reverb", 14, "declined")]
    assert oo.odds_stats(two, "Reverb") is None
    items = [o("Reverb", 15, "accepted"), o("Reverb", 12, "accepted"), o("Reverb", 18, "declined"),
             o("Reverb", 40, "accepted"), o("eBay", 15, "no_reply"), {"junk": 1}, "x"]
    s = oo.odds_stats(items, "Reverb — Mics")
    assert s == {"n": 3, "accepted": 2, "scope": "Reverb"}, s
    c = oo.chip(s)
    assert c["text"] == "Offers ~15% under ask: accepted 2 of 3 on Reverb", c
    s2 = oo.odds_stats(items, "eBay")  # only 1 on eBay -> falls back to all sites
    assert s2["scope"] == "" and s2["n"] == 4, s2
    assert "across all sites" in oo.chip(s2)["text"]
    assert oo.chip(oo.odds_stats([o("A", 15, "declined")] * 3, "A"))["good"] is False
    assert oo.odds_chip("Reverb") is None or isinstance(oo.odds_chip("Reverb"), dict)
    print("ok")


main()
