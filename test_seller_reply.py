import sys
sys.path.insert(0, "/tmp/gs")
from scrapers.seller_reply import reply_stats, chip, reply_chip

A = "2026-11-01T10:00:00+00:00"
LOG = [
    {"id": "1", "at": A, "source": "Reverb", "seller": "bob", "reply": "accepted", "replied_at": "2026-11-01T14:00:00+00:00"},
    {"id": "2", "at": A, "source": "Reverb", "seller": "bob", "reply": "no_reply"},
    {"id": "3", "at": A, "source": "Reverb", "seller": "ann", "reply": "no_reply"},
    {"id": "4", "at": A, "source": "Reverb", "seller": "cat", "reply": "no_reply"},
    {"id": "5", "at": A, "source": "Reverb", "seller": "cat", "reply": "no_reply"},
    {"id": "6", "at": A, "source": "eBay", "seller": "dan", "reply": "declined"},
    {"id": "7", "at": A, "source": "eBay", "seller": "eve", "reply": "countered", "replied_at": "garbage"},
]
bad = 0


def check(name, got, want):
    global bad
    ok = got == want
    if not ok:
        bad += 1
    print("ok  " if ok else "FAIL", name, "" if ok else (got, want))


s = reply_stats(LOG, "Reverb — Used", "bob")
check("bob stats", (s["n"], s["replied"], s["hours"]), (2, 1, 4.0))
check("bob chip", chip(s)["text"], "Replied to 1 of 2 offers · usually ~4h")
check("ann single no-reply hidden", chip(reply_stats(LOG, "Reverb", "ann")), None)
check("cat never replies", chip(reply_stats(LOG, "Reverb", "cat"))["text"], "No reply to 2 offers")
check("dan no timestamp", chip(reply_stats(LOG, "eBay", "dan"))["text"], "Replied to 1 of 1 offer")
check("eve bad timestamp", chip(reply_stats(LOG, "eBay", "eve"))["text"], "Replied to 1 of 1 offer")
check("wrong source", reply_stats(LOG, "eBay", "bob"), None)
check("unknown seller", reply_stats(LOG, "Reverb", "zed"), None)
check("no seller", reply_stats(LOG, "Reverb", None), None)
check("empty log", reply_stats([], "Reverb", "bob"), None)
check("None log", reply_stats(None, "Reverb", "bob"), None)
check("junk items", reply_stats(["x", None, 3], "Reverb", "bob"), None)
check("chip None", chip(None), None)
check("chip junk", chip({"bad": 1}), None)
check("naive vs aware", reply_stats([{"at": A, "source": "R", "seller": "q", "reply": "accepted",
                                      "replied_at": "2026-11-01T12:00:00"}], "R", "q")["hours"], None)
check("reply_chip no file", reply_chip("Reverb", "nobody-xyz"), None)
print("failures:", bad)
