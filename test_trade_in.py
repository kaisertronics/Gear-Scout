from scrapers import trade_in

r = trade_in.compare(1000)
assert r["trade"] == 600 and r["self"] == 920 and r["best"] == "reverb" and r["diff"] == 320
assert "Reverb nets" in r["line"]
r = trade_in.compare("$100")
assert r["self"] == 65 and r["trade"] == 60 and r["best"] == "reverb"
r = trade_in.compare(40)
assert r["self"] == 8 and r["trade"] == 24
r = trade_in.compare(30)
assert r["best"] == "trade-in" and "Trade-in nets" in r["line"]
for bad in (None, "", "abc", 0, -5, float("nan"), float("inf"), [], {}):
    assert trade_in.compare(bad) is None
print("ok")
