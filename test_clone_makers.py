"""Clone makers: feed, sibling terms, Facebook maker terms, misspellings, posting-time boost."""
from datetime import datetime, timedelta, timezone

from scrapers import clone_makers as C, posting_times as P

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def row(title, url, hours_ago, **kw):
    return {"title": title, "url": url, "first_seen": (NOW - timedelta(hours=hours_ago)).isoformat(), **kw}


def test_maker_of():
    assert C.maker_of("Vanguard V13 Large Diaphragm") == "vanguard"
    assert C.maker_of("sE Electronics VR2 ribbon") == "se"
    assert C.maker_of("United UT 87 mic") == "united"
    assert C.maker_of("Audio Scape LA-2A") == "audioscape"
    assert C.maker_of("Shure SM7B") is None
    assert C.maker_of("Pioneer united states flag") is None


def test_pick_newest_first_and_limits():
    rows = [row("Peluso P12", "a", 30), row("Vanguard V4", "b", 2), row("Vanguard V4", "b", 2),
            row("Shure SM57", "c", 1), row("Serrano 87", "d", 100), row("AudioScape LA-2A", "e", 5)]
    out = C.pick(rows, NOW)
    assert [r["url"] for r in out] == ["b", "e", "a"]      # newest first, deduped, SM57 and 100h-old dropped
    assert out[0]["maker"] == "Vanguard"
    assert C.pick([], NOW) == []
    assert C.pick([{"title": "Vanguard V13", "url": "x", "first_seen": "bad"}], NOW) == []


def test_sibling_and_fb_terms():
    s = C.sibling_terms()
    assert "vanguard v4" in s and "advanced audio cm12" in s and len(s) == len(set(s))
    assert "vanguard microphone" in C.fb_terms()


def test_derived_terms_include_clone_siblings():
    from scrapers import wanted
    d = wanted.derived_terms({"telex_list": ["Vanguard V13"]})
    assert "vanguard v4" in d
    assert "vanguard v13" not in d
    assert wanted.derived_terms({"wanted": {"derived_terms": False}}) == []


def test_misspellings_fixed():
    from scrapers.base import fix_brand_spelling
    assert fix_brand_spelling("vangaurd v13 mic") == "vanguard v13 mic"
    assert fix_brand_spelling("pelluso p12") == "peluso p12"


def test_posting_hot_buckets():
    sat10 = datetime(2026, 10, 3, 17, tzinfo=timezone.utc)   # Sat 10am PT
    other = datetime(2026, 10, 7, 20, tzinfo=timezone.utc)   # Wed 1pm PT
    times = [sat10 + timedelta(days=7 * i % 28, minutes=i % 50) for i in range(60)] * 1
    times = [datetime(2026, 10, 3, 17, i % 50, tzinfo=timezone.utc) for i in range(60)]
    times += [datetime(2026, 10, 5 + (i % 5), 8 + (i % 12), tzinfo=timezone.utc) for i in range(120)]
    hot = P.hot_buckets(times)
    assert "we-10" in hot
    assert P.hot_buckets(times[:20]) == []                  # too little data: never boosts


def test_hot_now_reads_file_only(tmp="/tmp"):
    import json, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        P.PROFILE = d + "/p.json"
        assert P.hot_now(refresh=False) is False            # no file: not hot, no crash
        b = P.bucket(datetime.now(timezone.utc))
        json.dump({"built": "2026-11-03T00:00:00+00:00", "hot": [b], "ads": 999}, open(P.PROFILE, "w"))
        assert P.hot_now(refresh=False) is True
        assert P.summary()["hot_now"] is True


def test_fb_pick_extra_slot_and_rotation_keeps_moving():
    from scrapers.telex import fb_pick
    assert fb_pick([], ["a", "b", "c"], 2) == ["a", "b"]                      # no maker due: normal rotation
    assert fb_pick(["m1"], ["a", "b", "c"], 2) == ["m1", "a", "b"]            # one maker: the extra slot
    assert fb_pick(["m1", "m2"], ["a", "b", "c"], 2) == ["m1", "m2", "a"]     # two: extra + one regular
    assert len(fb_pick(["m1", "m2"], ["a", "b", "c"], 2)) == 3                # never more than slots + 1
    assert fb_pick([], [], 2) == []
    assert fb_pick(["m1"], ["a"], 0) == ["m1"]


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
