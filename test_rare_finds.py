import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrapers.rare_finds import rare_find

POS = [
    ("Neumann U47 vintage tube microphone", None),
    ("Vintage Neumann U 47 FET", None),
    ("Neumann M49 condenser mic", None),
    ("AKG C12 original CK12 tube mic", None),
    ("AKG C24 stereo tube mic", None),
    ("Telefunken ELA M 251 tube microphone", None),
    ("Telefunken ELA M251E", None),
    ("Sony C-37A vintage condenser", None),
    ("Sony C-800G", None),
    ("RCA 44-BX ribbon microphone", None),
    ("RCA Type 77-DX vintage ribbon mic", None),
    ("RCA BK-5A ribbon", None),
    ("Altec 633A Salt Shaker", None),
    ("Pultec EQP-1A equalizer", None),
    ("Pultec MEQ-5", None),
    ("Fairchild 670 compressor", None),
    ("Teletronix LA-2A vintage original leveling amplifier", None),
    ("Vintage Neve 1073 module from console 1970s", None),
    ("Telefunken V72 preamp", None),
    ("Siemens W395 amplifier", None),
    ("Coles 4038 ribbon", None),
    ("U47 tube microphone", "Neumann"),
]
NEG = [
    ("Neumann U87 Ai", None),
    ("Neumann U87", "Neumann"),
    ("U47 style clone microphone", "Neumann"),
    ("Neumann U47 type tube mic", None),
    ("Telefunken ELA M 251 case", None),
    ("Telefunken ELA M 251 replica", None),
    ("Neumann U47 poster", None),
    ("Neumann U47 XLR cable", None),
    ("AKG C12 vinyl record LP", None),
    ("AKG C214 microphone", None),
    ("AKG C414 XLII", None),
    ("Neumann KM84", None),
    ("Shure SM57", None),
    ("Sony C-800G kit", None),
    ("44 inch speaker", None),
    ("Pultec EQP-1A clone", None),
    ("Neve 1073 DPA", None),
    ("Neve 1073LB lunchbox preamp", None),
    ("Warm Audio WA73 Neve 1073 style", None),
    ("API 550a 500 series", None),
    ("Universal Audio LA-2A reissue", None),
    ("Audioscape EQP-1A (Pultec)", None),
    ("NEW Fairchild 660 in the box", None),
    ("Union Sleeve Nut For Neumann Ea87 U87ai U67", None),
    ("Tube Microphone Pre Modeled after Telefunken V76", None),
    ("Coles 4038 2000s - Black", None),
    ("", None),
    (None, None),
]

bad = 0
for t, b in POS:
    got = rare_find(t, b)
    if not got:
        bad += 1
        print("FAIL (expected match):", t, b)
for t, b in NEG:
    got = rare_find(t, b)
    if got:
        bad += 1
        print("FAIL (expected none):", t, b, "->", got)
print("failures:", bad, "of", len(POS) + len(NEG))
sys.exit(1 if bad else 0)
