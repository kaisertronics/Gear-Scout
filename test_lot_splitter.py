import sys
sys.path.insert(0, "/tmp/gs")
from scrapers import lot_splitter as ls

PRICES = {"sm57": 100, "sm58": 90, "akg 414": 700, "u87": 2000}


def valuer(name):
    return PRICES.get(name.lower())


fails = 0


def check(label, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        fails += 1


check("is_lot yes", ls.is_lot("Lot: SM57, SM58, AKG 414, cables $900"))
check("is_lot no", not ls.is_lot("Shure SM57"))
pieces = ls.split_lot("Lot: SM57, SM58, AKG 414, cables $900")
check("split drops lead/price", pieces[:3] == ["SM57", "SM58", "AKG 414"] and "cables" in pieces)
check("qty prefix", ls._qty_and_name("2x SM57") == (2, "SM57"))
check("qty bare", ls._qty_and_name("3 Shure SM58") == (3, "Shure SM58"))
check("qty suffix", ls._qty_and_name("SM57 x3") == (3, "SM57"))
check("model number not qty", ls._qty_and_name("414 XLS") == (1, "414 XLS"))
parts = ls.value_parts(["2x SM57", "SM58", "AKG 414", "cables", "SM58"], valuer)
check("value_parts total", sum(p["value"] for p in parts) == 200 + 90 + 700 and len(parts) == 3)
c = ls.lot_chip("Lot: SM57, SM58, AKG 414, cables $900", None, 900, valuer)
check("ask above parts -> none", c is None)
c = ls.lot_chip("Lot: 2x SM57, SM58, AKG 414, cables", None, 700, valuer)
check("chip text", c is not None and c["total"] == 990 and c["pct_under"] == 29 and "Lot parts" in c["text"] and "$990" in c["text"])
check("not under enough", ls.lot_chip("Lot: SM57, SM58, AKG 414", None, 850, valuer) is None)
check("single item", ls.lot_chip("Lot: AKG 414, cables", None, 300, valuer) is None)
check("no lot word", ls.lot_chip("SM57, SM58, AKG 414", None, 300, valuer) is None)
check("no ask", ls.lot_chip("Lot: SM57, SM58", None, None, valuer) is None)
check("bad input", ls.lot_chip(None, None, "x", valuer) is None)
print("FAILS", fails)
sys.exit(1 if fails else 0)
