"""Tests for scrapers/unnamed_mic.py (pure function, no I/O)."""
import sys

sys.path.insert(0, ".")
from scrapers import unnamed_mic as u  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    if not cond:
        fails += 1
        print("FAIL", name)


r = u.identify("Vintage condenser microphone", "Made in Germany, works great")
check("germany", r and "Neumann" in r["guesses"][0] and "Germany" in r["reason"])

r = u.identify("Ribbon mic")
check("ribbon", r and any("ribbon" in g.lower() for g in r["guesses"]))

r = u.identify("Vintage tube mic", "U47 style, 9 pin connector")
check("u47 style in description", r and "U47" in r["guesses"][0])
check("max guesses", r and len(r["guesses"]) <= u.MAX_GUESSES)

check("studio mic no clues", u.identify("Studio mic") is None)
check("year not a model", u.identify("Vintage mic 1950s ribbon") is not None)

# Not unnamed or not a mic
check("brand", u.identify("Neumann vintage condenser microphone", "made in Germany") is None)
check("model", u.identify("AT4050 condenser microphone", "made in Japan") is None)
check("shure", u.identify("Shure ribbon mic") is None)
check("preamp", u.identify("Vintage tube mic preamp", "made in Germany") is None)
check("stand", u.identify("Studio mic stand", "made in Germany") is None)
check("not a mic", u.identify("Vintage tube amplifier", "made in Germany") is None)
check("empty", u.identify("") is None and u.identify(None) is None)
check("no desc", u.identify("Condenser microphone", None) is None)

print("FAILED" if fails else "ok", fails)
sys.exit(1 if fails else 0)
