import sys
sys.path.insert(0, "/tmp/gs")
sys.path.insert(0, ".")
from scrapers.price_per_channel import parse_channels, per_channel

cases = [
    ("Focusrite OctoPre 8 channel mic preamp", 8),
    ("Presonus Digimax 8-ch preamp", 8),
    ("Behringer ADA8200 8ch ADAT", 8),
    ("ADAT 16 channel converter", 16),
    ("Mackie 4ch mixer", 4),
    ("Allen & Heath 16-channel mixer", 16),
    ("Neve 8816 summing mixer", None),
    ("Single channel preamp", None),
    ("Channel 8 strip", None),
    ("Neumann U87 mic with 2 channel preamp", None),
    ("Behringer 8 channel mic pre", 8),
    ("XLR cable 8 channel snake", None),
    ("128 channel console", None),
    ("1 channel DI", None),
    ("Rack case for 8 ch preamps", None),
    ("Much 5 chocolate", None),
    ("Presonus 4 channel preamp and 8 channel mixer", None),
    ("Audient ASP880 8ch", 8),
]
bad = 0
for t, want in cases:
    got = parse_channels(t)
    if got != want:
        bad += 1
        print("FAIL", t, got, want)
r = per_channel("$800", "Focusrite 8 channel preamp")
assert r and r["each"] == 100 and r["text"] == "$100 per channel" and not r["low"], r
r = per_channel(300, "4ch mixer")
assert r["text"] == "$75 per channel" and r["low"], r
assert per_channel(None, "8ch") is None
assert per_channel(0, "8ch") is None
assert per_channel("$500", "Vintage compressor") is None
print("FAILS", bad)
sys.exit(1 if bad else 0)
