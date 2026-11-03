"""Shopping-list items are searched as their own terms and match on title or description."""
import sqlite3
from datetime import datetime, timezone

from scrapers import cable_family, shopping_list, wanted
from scrapers.cable_family import in_description, siblings, title_matches

STX = 'Hosa STX-805 8-Channel Audio Snake Cable 1/4" TRS to Female XLR 16ft'
DTP = "HOSA 6 ft DB25 to 8 TRS cables (2)"


def test_stx_family_matches_unsuffixed_title_and_siblings():
    assert title_matches("Hosa STX-805F", STX)
    assert not title_matches("Hosa STX-807F", STX)  # a different length
    assert in_description("Hosa STX-805F", "<p>Hosa STX-805 snake</p>")
    assert title_matches("Hosa STX-807F", "Hosa 8 channel snake XLR to 1/4 TRS cable 23ft")
    assert wanted.match(STX, "$29.99", [("Hosa STX-805F", __import__("scrapers.telex", fromlist=["x"]).term_matcher("Hosa STX-805F"), None, True)])


def test_siblings_skip_listed_terms():
    sib = siblings(["Hosa DTP-805", "Hosa STX-807F", "dtp-802", "ssl"])
    assert "Hosa STX-805F" in sib and "Hosa DTP-803" in sib
    assert "Hosa DTP-802" not in sib and "Hosa DTP-805" not in sib and "Hosa STX-807F" not in sib


def test_derived_terms_include_every_shopping_item_and_can_be_disabled():
    d = wanted.derived_terms({"telex_list": ["Hosa STX-807F"]})
    assert "Hosa STX-805F" in d and "Hosa DTP-802" in d
    assert {it["key"] for it in shopping_list.ITEMS} == set(shopping_list.SEARCH)
    assert wanted.derived_terms({"wanted": {"derived_terms": False}}) == []


def test_shopping_matches_title_or_description(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE seen (title, price, url, source_name, location, shipping, last_seen,
                 sold, pending, hidden, duplicate, description, country)""")
    now = datetime.now(timezone.utc).isoformat()
    rows = [(STX, "$29.99", "u1", "eBay", "LA", 12, now, 0, 0, 0, 0, "Used", "US"),
            ("Hosa 8 channel snake", "$40", "u2", "eBay", "NY", 0, now, 0, 0, 0, 0, "Used · Hosa DTP-802 DB25 to TRS", "US"),
            ("Hosa DTP-805 DB25 snake", "$40", "u3", "eBay", "NY", 0, now, 0, 0, 0, 0, "Hosa DTP-802 mention", "US")]
    c.executemany("INSERT INTO seen VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    monkeypatch.setattr(shopping_list, "DB", str(db))
    monkeypatch.setattr(shopping_list, "STATE_PATH", str(tmp_path / "st.json"))
    shopping_list._cache.update(at=0.0, data=None)
    items = {i["key"]: i for i in shopping_list.summary()["items"]}
    assert items["stx"]["matches"][0]["unit"] == 29.99 and items["stx"]["matches"][0]["ship"] == 12
    assert items["stx"]["matches"][0]["delivered"] == round(41.99 * 1.107, 2)
    assert items["dtp"]["used"]
    urls = [m["url"] for m in items["dtp"]["matches"]]
    assert "u2" in urls  # model only in the description
    shopping_list._cache.update(at=0.0, data=None)


def test_cheap_listing_not_hidden_by_median_floor(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE seen (title, price, url, source_name, location, shipping, last_seen,
                 sold, pending, hidden, duplicate, description, country)""")
    now = datetime.now(timezone.utc).isoformat()
    rows = [(f"Mogami Gold DB25 TRS snake {i}", "$240", f"m{i}", "eBay", "NY", 0, now, 0, 0, 0, 0, "", "US") for i in range(5)]
    rows.append((DTP, "$45.00", "cheap", "eBay", "NY", 0, now, 0, 0, 0, 0, "", "US"))  # 2 cables: $22.50 each
    c.executemany("INSERT INTO seen VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    monkeypatch.setattr(shopping_list, "DB", str(db))
    shopping_list._cache.update(at=0.0, data=None)
    dtp = next(i for i in shopping_list.build()["items"] if i["key"] == "dtp")
    assert dtp["matches"][0]["url"] == "cheap" and dtp["matches"][0]["unit"] == 22.5
    shopping_list._cache.update(at=0.0, data=None)
