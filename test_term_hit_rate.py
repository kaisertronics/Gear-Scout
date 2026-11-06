from scrapers import term_hit_rate as t

rows = []
for i in range(30):
    rows.append({"url": f"u{i}", "title": "noisy thing", "favorite": 0})
for i in range(10):
    rows.append({"url": f"g{i}", "title": "good thing", "favorite": 1 if i < 5 else 0})
for i in range(5):
    rows.append({"url": f"s{i}", "title": "small thing", "favorite": 0})


def match(r):
    return [w for w in ("noisy", "good", "small") if w in r["title"]]


rep = t.hit_rates(rows, match, {"g7", "g8"})
by = {e["term"]: e for e in rep}
assert by["noisy"]["matched"] == 30 and by["noisy"]["good"] == 0 and by["noisy"]["noisy"], by["noisy"]
assert by["good"]["matched"] == 10 and by["good"]["good"] == 7 and not by["good"]["noisy"], by["good"]
assert by["small"]["noisy"] is False, "too few matches to judge"
assert rep[0]["term"] == "noisy", "noisiest first"
assert t.noisy_lines(rep) == ["noisy (0 of 30)"], t.noisy_lines(rep)
# an alert on one noisy listing at 1/30 (3%) is above the 2% line
assert t.noisy_terms(t.hit_rates(rows, match, {"u1"})) == [], "3% is not noisy"
# broken matcher never raises
assert t.hit_rates(rows, lambda r: 1 / 0, set()) == []
assert t.hit_rates([], match, set()) == []
print("ok")
