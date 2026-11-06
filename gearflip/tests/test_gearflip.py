"""Gear Flip: profit math, the budget plan, the ledger, alerts, and one full
scan against a throwaway listings database."""
from datetime import datetime, timedelta, timezone

import pytest

from gearflip import economics as E, portfolio as P

ST = {"buy_tax_pct": 10, "local_pickup_cost": 15, "sell_on": "best", "min_roi": 20, "min_profit": 75}


@pytest.fixture
def gf_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEARFLIP_DIR", str(tmp_path / "gf"))
    return tmp_path / "gf"


# ---------------------------------------------------------------- economics
def test_working_item_numbers_add_up():
    m = E.analyze(title="Neumann TLM 103", ask=600, value=1000, n=20, sold_basis=True, local=True, settings=ST)
    assert m["buy_total"] == 615 and m["tax"] == 0 and m["pickup"] == 15  # local: a trip, no tax
    assert m["repair"] is None and m["repair_cost"] == 0
    side = E.sell_side(m["channel"], 980, "small")
    assert m["resale"] == round(side["price"]) and m["fees"] == round(side["fees"])
    assert m["profit_if_ok"] == round(side["net"] - 615)
    assert m["downside"] < m["expected"] < m["profit_if_ok"]
    assert m["roi"] == pytest.approx(m["expected"] / m["cash_in"] * 100, abs=0.5)


def test_online_purchase_pays_tax_and_shipping():
    m = E.analyze(title="AKG C414 XLII", ask=500, value=900, n=10, ship_in=20, local=False, settings=ST)
    assert m["tax"] == 52 and m["buy_total"] == 572 and m["pickup"] == 0


def test_repair_costs_and_odds_lower_the_expected_profit():
    ok = E.analyze(title="Teletronix LA-2A", ask=1500, value=4000, n=12, settings=ST)
    fix = E.analyze(title="Teletronix LA-2A", ask=1500, value=4000, n=12, repair="general repair", settings=ST)
    assert fix["repair_cost"] == 300 and fix["repair_odds"] == 60
    assert fix["expected"] < ok["expected"] and fix["downside"] < 0
    assert fix["cash_need"] == fix["buy_total"] + 300
    assert fix["confidence"] < ok["confidence"]
    assert fix["days"] > ok["days"]


def test_heavy_gear_sells_local_and_mics_ship():
    console = E.analyze(title="Tascam 32 channel console", ask=800, value=2000, n=6, settings=ST)
    mic = E.analyze(title="Neumann U87 Ai", ask=1400, value=2600, n=30, settings=ST)
    assert console["size"] == "heavy" and console["channel"] == "local" and console["ship_out"] == 0
    assert mic["size"] == "small" and mic["channel"] in ("reverb", "ebay") and mic["ship_out"] > 0


def test_walk_away_price_hits_the_minimum_roi():
    m = E.analyze(title="Neumann U87 Ai", ask=1500, value=2600, n=25, sold_basis=True, settings=ST)
    at = E.analyze(title="Neumann U87 Ai", ask=m["walk_away"], value=2600, n=25, sold_basis=True,
                   settings={**ST, "sell_on": m["channel"]})
    assert at["roi"] == pytest.approx(20, abs=1.5) or at["expected"] == pytest.approx(75, abs=3)
    assert at["roi"] >= 18.5 and at["expected"] >= 72


def test_passes_respects_minimums():
    m = E.analyze(title="Shure SM7B", ask=380, value=400, n=30, settings=ST)
    assert not E.passes(m, {"min_profit": 75, "min_roi": 20, "min_confidence": 0, "max_days": 90})


# ---------------------------------------------------------------- portfolio
def _o(url, need, exp, conf=100, model=None, score=50):
    return {"url": url, "model": model or url, "m": {"cash_need": need, "cash_in": need, "expected": exp,
            "confidence": conf, "score": score, "buy_total": need, "repair_cost": 0, "fees": 0, "ship_out": 0,
            "resale": need + exp, "downside": -10, "days": 20}}


def test_plan_beats_greedy_and_stays_in_budget():
    # Greedy-by-profit takes the $900 one (+300) and can't fit more; two $500 ones make +400.
    opps = [_o("a", 900, 300), _o("b", 500, 200), _o("c", 500, 200)]
    chosen = P.optimize(opps, 1000)
    assert sorted(o["url"] for o in chosen) == ["b", "c"]
    t = P.totals(chosen, 1000)
    assert t["cash_in"] <= 1000 and t["expected"] == 400 and t["cash_left"] == 0


def test_plan_limits_one_per_model_and_discounts_shaky_estimates():
    opps = [_o("a", 400, 200, model="u87"), _o("b", 400, 190, model="u87"), _o("c", 400, 250, conf=30)]
    urls = {o["url"] for o in P.optimize(opps, 1200)}
    assert urls == {"a", "c"}
    assert [o["url"] for o in P.optimize(opps, 400)] == ["a"]  # 200 sure beats 250 x 30%


def test_plan_with_no_cash_is_empty():
    assert P.optimize([_o("a", 100, 50)], 0) == []


# ---------------------------------------------------------------- settings + ledger
def test_settings_clean_clamps_and_defaults(gf_dir):
    from gearflip import settings as S
    s = S.save({"budget": "$5,000", "min_roi": "-4", "sell_on": "myspace", "alerts": "off", "bogus": 1})
    assert s["budget"] == 5000 and s["min_roi"] == 0 and s["sell_on"] == "best" and s["alerts"] is False
    assert "bogus" not in s and S.load() == s


def test_ledger_bankroll(gf_dir):
    from gearflip import ledger as L
    m = E.analyze(title="Shure SM7B", ask=250, value=400, n=30, settings=ST)
    a = L.buy({"url": "u1", "title": "SM7B", "source": "eBay", "m": m}, 260)
    assert L.buy({"url": "u1", "m": m})["id"] == a["id"]  # no double-tap duplicate
    b = L.buy({"url": "u2", "title": "LA-2A", "m": {"buy_total": 1000, "repair_cost": 300}})
    book = L.bankroll(2000)
    assert book["tied"] == 260 + 1300 and book["cash"] == 2000 - 1560
    L.sold(a["id"], 400, 20, "reverb")
    L.repaired(b["id"], 150)
    book = L.bankroll(2000)
    fees = round(400 * 0.0819 + 0.49, 2)
    assert book["realized"] == round(400 - fees - 20 - 260, 2)
    assert book["tied"] == 1150 and book["sold"] == 1 and book["open"] == 1
    assert L.open_urls() == {"u2"}
    assert L.remove(b["id"]) and not L.remove("nope")


# ---------------------------------------------------------------- alerts
def test_push_text_has_the_analytics():
    from gearflip import alerts
    m = E.analyze(title="Teletronix LA-2A", ask=1500, value=4000, n=12, repair="tubes", local=True, settings=ST)
    title, msg = alerts.push_text({"title": "Teletronix LA-2A needs tubes", "source": "Craigslist", "location": "Seattle",
                                   "m": m}, rank=2, plan_n=4, cash=3000)
    assert f"+${m['expected']:,}" in title and "ROI" in title
    for part in ("Buy $1,500", "Needs: tubes", "Fees", "Ship", "Net profit", "days to sell", "Confidence",
                 "Walk-away", "#2 of 4"):
        assert part in msg


def test_alerts_only_fresh_affordable_once(gf_dir, monkeypatch):
    from gearflip import alerts, source
    sent = []
    monkeypatch.setattr(source, "push", lambda cfg, topic, title, msg, **kw: sent.append(title) or True)
    st = {"alerts": True, "alert_min_profit": 100, "alert_min_score": 10, "max_pushes_per_scan": 3, "budget": 5000,
          "ntfy_topic": "t"}
    good = E.analyze(title="Neumann U87 Ai", ask=1400, value=2600, n=25, sold_basis=True, settings=ST)
    res = {"opps": [{"url": "fresh", "title": "U87", "source": "FB", "age_days": 1, "m": good},
                    {"url": "old", "title": "U87", "source": "FB", "age_days": 9, "m": good}]}
    plan = {"cash": 5000, "items": [], "rank": {}}
    assert alerts.send({}, res, plan, st)["pushed"] == 1 and len(sent) == 1
    assert alerts.send({}, res, plan, st)["pushed"] == 0  # once per ad
    cheaper = dict(good, ask=1200)
    res["opps"][0]["m"] = cheaper
    assert alerts.send({}, res, plan, st)["pushed"] == 1  # again after a 10%+ price drop
    st["alerts"] = False
    assert alerts.send({}, res, plan, st)["pushed"] == 0


# ---------------------------------------------------------------- full scan
@pytest.fixture
def db(tmp_path, monkeypatch, gf_dir):
    from scrapers import market_value as mv, store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "seen.db")
    monkeypatch.setattr(store, "_schema_ready", False)
    mv._cache.update(at=0.0, idx=None, sold=None, tiers=None)
    from gearflip import source
    source._speed.update(at=0.0, by_model={})
    yield store
    mv._cache.update(at=0.0, idx=None, sold=None, tiers=None)


def _listing(i, title, price, src="Reverb — All Used", ship=None, when=None):
    from scrapers.base import Listing
    return Listing(source_name=src, title=title, url=f"https://example.com/{i}", price=f"${price}",
                   description="Used", listing_id=str(i), shipping=ship, country="US",
                   posted_at=when or datetime.now(timezone.utc) - timedelta(hours=5))


def test_full_scan_finds_working_and_repair_flips(db):
    from gearflip import engine, settings as S
    for i in range(14):  # what this mic usually goes for
        db.mark_seen(_listing(i, "Neumann TLM 103 condenser microphone", 1000 + (i % 5) * 20, ship=0))
    db.mark_seen(_listing(100, "Neumann TLM 103 condenser microphone", 560, src="Craigslist — Seattle"))
    db.mark_seen(_listing(101, "Neumann TLM 103 microphone not working", 150, src="FB Marketplace — Seattle"))
    db.mark_seen(_listing(102, "Neumann TLM 103 condenser microphone", 990, ship=0))  # no margin
    st = S.save({"budget": 2000, "min_confidence": 0})
    res = engine.scan({}, st)
    urls = {o["url"]: o for o in res["opps"]}
    assert "https://example.com/100" in urls and "https://example.com/102" not in urls
    o = urls["https://example.com/100"]
    assert o["m"]["pickup"] == 15 and o["m"]["tax"] == 0 and o["m"]["expected"] > 150 and o["why"].startswith("Pay")
    rep = urls.get("https://example.com/101")
    assert rep and rep["m"]["repair"] == "general repair" and rep["m"]["repair_cost"] == 300
    plan = engine.plan(res, st)
    assert plan["items"] and plan["t"]["cash_in"] <= 2000
    assert engine.latest()["found"] == res["found"]


def test_tab_renders(db):
    flask = pytest.importorskip("flask")
    import jinja2
    from gearflip import engine, settings as S
    from gearflip.web import bp
    db.mark_seen(_listing(1, "Shure SM7B", 200, src="Craigslist — Seattle"))
    for i in range(2, 10):
        db.mark_seen(_listing(i, "Shure SM7B", 380))
    engine.scan({}, S.save({"min_confidence": 0}))
    app = flask.Flask(__name__)
    app.jinja_loader = jinja2.ChoiceLoader([jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                                           app.jinja_loader] if app.jinja_loader else
                                          [jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"})])
    app.register_blueprint(bp)
    c = app.test_client()
    r = c.get("/gearflip", headers={"X-GearScout-Test": "1"})
    assert r.status_code == 200 and b"Gear Flip" in r.data and b"Best buys" in r.data
    assert c.get("/gearflip/api/plan.json").json["bankroll"]["budget"] == 2000
    r = c.post("/gearflip/settings", data={"budget": "3000", "alerts": "on"})
    assert r.status_code == 302 and S.load()["budget"] == 3000 and S.load()["scour"] is False
