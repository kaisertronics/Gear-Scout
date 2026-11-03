"""Unit tests for scrapers/spec_mismatch.py (run: python test_spec_mismatch.py)."""
import sys

from scrapers.spec_mismatch import check

# (title, description, should_flag)
CASES = [
    ("Neumann U87 Large Diaphragm Condenser", "Great mic. This is actually a TLM 103, sold as is.", True),
    ("Neumann U87", "It's a Neumann TLM103 in very good shape.", True),
    ("Neumann U87 Ai", "U87 style clone, works great and sounds close.", True),
    ("Neumann U87", "This is a replica of the U87, not a real Neumann.", True),
    ("Telefunken ELA M 251", "Not a real Telefunken, a Chinese copy of the 251.", True),
    ("Shure SM7B", "Model: Behringer B-2 Pro", True),
    ("Neumann U87", "this is a Behringer B-2 Pro large diaphragm mic", True),
    ("AKG C414 XLS", "Mic is a WA-87 actually, bought on a trade.", True),
    ("Neumann U87", "Sounds great, compare to a U87 or a TLM 103 at twice the price.", False),
    ("Neumann U87", "Fits a TLM 103 shockmount. Works with any XLR cable.", False),
    ("Neumann U87", "Includes original case, shockmount, and a copy of the manual.", False),
    ("Neumann U87", "Not a clone, 100% genuine Neumann. No replicas here.", False),
    ("Neumann U87 and U47 pair", "This is a U87 and this is a U47, matched pair.", False),
    ("Neumann U87 Ai", "This is a U87ai, serial 123456, mint.", False),
    ("Warm Audio WA-87 R2", "Modeled after the Neumann U87, but this is a Warm Audio WA-87.", False),
    ("Neumann U87 style large diaphragm condenser clone", "It is a clone and says so.", False),  # title is honest
    ("Shockmount for Neumann U87", "This is a TLM 103 compatible mount.", False),
    ("Neumann U87", "", False),
    (None, None, False),
    ("Vintage tube microphone", "This is a TLM 103 maybe.", False),  # no model in title
    ("Neumann TLM 103", "Pair with a Shure SM7B for the podcast, it's a Neumann TLM 103.", False),
]


def main() -> int:
    bad = 0
    for i, (title, desc, want) in enumerate(CASES, 1):
        got = check(title, desc)
        ok = bool(got) == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {i:2d} want={want!s:5} got={got!r}")
    print(f"{len(CASES) - bad}/{len(CASES)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
