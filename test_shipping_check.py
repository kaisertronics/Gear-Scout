from scrapers.shipping_check import check, label

# small shipping: no tag
assert check(500, 20, 700) is None
assert check(500, 0, 700) is None
assert check(500, None, 700) is None
assert check("500", 50, 700) is None
assert check(0, 50, 700) is None
# >= $30
r = check(1000, 30, 1400)
assert r == {"ship": 30, "real": 1030, "pct_under": 26}, r
# >= 8% of price
r = check(200, 16, 300)
assert r["real"] == 216 and r["pct_under"] == 28, r
assert check(200, 15, 300) is None
# no used value
r = check(300, 45)
assert r["pct_under"] is None
assert label(r) == "Ships $45: real price $345"
# over used value
assert label(check(500, 100, 550)) == "Ships $100: real price $600, 9% over used value"
assert label(check(500, 45, 700)) == "Ships $45: real price $545, 22% under used value"
assert label(None) is None
print("ok")
