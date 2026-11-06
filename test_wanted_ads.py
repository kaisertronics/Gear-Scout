"""Wanted-ad sniper: no-price / placeholder-price local ads for buy-list gear."""
from scrapers import wanted_ads as W

V = {"1176": 1000, "u87": 1500, "sm57": 90}


def val(t):
    t = t.lower().replace(" ", "")
    return next((v for k, v in V.items() if k in t), None)


def row(title, price, url, src="Craigslist — Seattle", **kw):
    return {"title": title, "price": price, "url": url, "source_name": src, **kw}


def test_placeholder_kinds():
    assert W.placeholder(None) == "none" and W.placeholder("") == "none"
    assert W.placeholder("$1") == "token" and W.placeholder("$0") == "token"
    assert W.placeholder("$450") == "" and W.placeholder("C$450") == ""


def test_cues():
    assert W.cues("1176 compressor, looking to sell") == ["looking to sell"]
    assert "must go" in W.cues("UA 1176 must go") and "open to offers" in W.cues("U87 make me an offer")
    assert W.cues("Shure SM57") == []


def test_picks_unpriced_buylist_and_ranks():
    rows = [row("Shure SM57 must go", None, "a"), row("UA 1176 compressor", "", "b"),
            row("Neumann U87 mic looking to sell", "$1", "c"), row("Neumann U87", "$900", "d"),
            row("UA 1176", None, "e", src="Reverb — All Used")]
    out = W.pick(rows, val)
    assert [r["url"] for r in out] == ["c", "b"]          # SM57 too cheap, $900 has a real price, Reverb isn't a local site
    assert "no price" in out[1]["finder_note"] or "placeholder" in out[0]["finder_note"]
    assert "What price" in out[0]["finder_msg"]


def test_unknown_model_only_if_buylist():
    out = W.pick([row("Fairchild 670 compressor", None, "a"), row("random lamp", None, "b")], val)
    assert [r["url"] for r in out] == ["a"] and "buy list" in out[0]["finder_note"]


def test_dedupes():
    out = W.pick([row("UA 1176", None, "a"), row("UA 1176", None, "a")], val)
    assert len(out) == 1


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
