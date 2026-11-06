import sys
sys.path.insert(0, "/tmp/gsB")
from scrapers.ribbon_note import ribbon_note as rn

cases = [
    ("sE Electronics VR2 ribbon mic", "Great condition, comes with case", "no ribbon condition mentioned, ask"),
    ("Royer R-121 ribbon microphone", "Just re-ribboned by Royer", "says re-ribboned"),
    ("Beyerdynamic M160 mic", "ribbon is sagging a bit", "sagging (risk)"),
    ("AEA R84 ribbon mic", "Ribbon is blown, as is", "blown (risk)"),
    ("Cascade Fat Head ribbon mic", "no sag, ribbon is good", "described as OK"),
    ("Neumann U87 condenser mic", "mint", None),
    ("Satin hair ribbon 10 yards", "craft ribbon spool", None),
    ("Shure SM57", None, None),
    (None, None, None),
]
bad = 0
for t, d, want in cases:
    got = rn(t, d)
    ok = (got is None) if want is None else (got is not None and want in got)
    bad += not ok
    print("OK " if ok else "FAIL", t, "->", got)
print("failures:", bad)
sys.exit(1 if bad else 0)
