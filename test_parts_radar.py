"""LA-2A and clone parts radar: part ads well under their own family's usual price."""
from datetime import datetime, timezone
from scrapers import parts_radar as P

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def row(title, price, url, seen="2026-11-01T00:00:00+00:00", **kw):
    return {"title": title, "price": f"${price}", "url": url, "first_seen": seen, **kw}


def test_family():
    assert P.family("Kenetek T4B opto cell for LA-2A")[0] == "t4b"
    assert P.family("UTC A-18 transformer pair")[0] == "utc:a18"
    assert P.family("Sowter 8065 output transformer")[0] == "sowter:8065"
    assert P.family("NOS Telefunken ECC83 12AX7 tube smooth plate")[0] == "tube:12ax7" or P.family("NOS Telefunken ECC83 tube")[0].startswith("tube:")
    assert P.family("Carnhill VTB9045 transformer")[0] == "xfmr:carnhill"
    assert P.family("Fender guitar amp with UTC transformer") is None
    assert P.family("WTB T4B cell") is None
    assert P.family("Neumann U87") is None


def old(n, price, title="T4B opto cell"):
    return row(title, price, f"o{n}{price}", seen="2026-08-01T00:00:00+00:00")


def test_cheap_part_flagged():
    rows = [old(1, 200), old(2, 220), old(3, 240), old(4, 260), row("Kenetek T4B optical cell", 120, "new1")]
    out = P.pick(rows, NOW)
    assert [r["url"] for r in out] == ["new1"] and out[0]["part"]["median"] == 220 and "T4B" in out[0]["finder_note"]
    assert out[0]["finder_msg"].startswith("Hi!")


def test_needs_enough_history_and_real_gap():
    assert P.pick([old(1, 200), old(2, 220), row("T4B cell", 100, "n")], NOW) == []            # only 3 ads behind the median
    base = [old(1, 200), old(2, 220), old(3, 240), old(4, 260)]
    assert P.pick(base + [row("T4B cell", 200, "n")], NOW) == []                               # not 25% under
    assert P.pick(base + [row("T4B cell", 40, "n")], NOW) == []                                # >70% under = broken/mislabelled
    assert P.pick(base + [row("T4B cell", 120, "n", seen="2026-09-01T00:00:00+00:00")], NOW) == []  # not fresh
    assert P.pick(base + [row("T4B cell", 120, "n", description="Auction ends")], NOW) == []


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
