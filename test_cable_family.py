from scrapers.cable_family import alt_query, in_description, title_matches
from scrapers.base import keyword_match

T = "HOSA 6 ft DB25 to 8 TRS cables (2)"


def test_title_names_brand_and_connectors():
    assert title_matches("Hosa DTP-802", T)
    assert keyword_match(T, ["dtp-802"])
    assert not title_matches("Hosa DTP-802", "Hosa DTP-805 DB25 to TRS")  # a different model number
    assert not title_matches("Hosa DTP-802", "Behringer DB25 to 8 TRS snake")  # not Hosa
    assert not title_matches("Hosa DTP-802", "Hosa DB25 to 8 XLR female snake")  # other connectors
    assert not title_matches("Shure SM57", T)


def test_alt_query_and_description():
    assert alt_query("Hosa DTP-802") == "hosa db25 trs"
    assert alt_query("Shure SM57") is None
    assert in_description("Hosa DTP-802", "<p>Hosa DTP-802 snake, like new</p>")
    assert not in_description("ssl", "ssl everywhere")
