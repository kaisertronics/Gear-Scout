"""Gear Flip: profit math, the budget plan, the ledger, alerts, and one full
scan against a throwaway listings database."""
from datetime import datetime, timedelta, timezone

import pytest

from gearflip import economics as E, portfolio as P

ST = {"buy_tax_pct": 10, "local_pickup_cost": 15, "sell_on": "best", "min_roi": 20, "min_profit": 75}


@pytest.fixture
def gf_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEARFLIP_DIR", str(tmp_path / "gf"))
    from gearflip import source
    monkeypatch.setattr(source, "gemini_paused", lambda: False)  # tests never read the live pause
    return tmp_path / "gf"


# ---------------------------------------------------------------- economics
def test_working_item_numbers_add_up():
    m = E.analyze(title="Neumann TLM 103", ask=600, value=1000, n=20, sold_basis=True, local=True, settings=ST)
    assert m["buy_total"] == 615 and m["tax"] == 0 and m["pickup"] == 15  # local: a trip, no tax
    assert m["repair"] is None and m["repair_cost"] == 0
    side = E.sell_side(m["channel"], 1000, "small")
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
    mic = E.analyze(title="Neumann U87 Ai", ask=1400, value=2600, n=30, settings={**ST, "sell_on": "reverb"})
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


def test_small_budget_scores_small_flips_fairly():
    small = E.analyze(title="Shure SM7B", ask=200, value=400, n=30, local=True, settings={**ST, "budget": 500})
    big = E.analyze(title="Shure SM7B", ask=200, value=400, n=30, local=True, settings={**ST, "budget": 5000})
    assert small["score"] > big["score"]


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
    source._recent.update(at=0.0, by_model={})
    from scrapers import comps
    comps._similar_cache.update(at=0.0, data=None)
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


def test_tab_renders(db, monkeypatch):
    flask = pytest.importorskip("flask")
    import jinja2
    from gearflip import engine, settings as S
    from gearflip.web import bp
    db.mark_seen(_listing(1, "Shure SM7B", 200, src="Craigslist — Seattle"))
    for i in range(2, 10):
        db.mark_seen(_listing(i, "Shure SM7B", 380))
    engine.scan({}, S.save({"min_confidence": 0}))
    monkeypatch.setattr(engine, "scan_in_background", lambda cfg=None: False)  # no thread outliving the test
    app = flask.Flask(__name__)
    app.jinja_loader = jinja2.ChoiceLoader([jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                                           app.jinja_loader] if app.jinja_loader else
                                          [jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"})])
    app.register_blueprint(bp)
    c = app.test_client()
    r = c.get("/gearflip", headers={"X-GearScout-Test": "1"})
    assert r.status_code == 200 and b"Gear Flip" in r.data and b"Best buys" in r.data
    assert c.get("/gearflip/api/plan.json").json["bankroll"]["budget"] == 500
    r = c.post("/gearflip/settings", data={"budget": "3000", "alerts": "on"})
    assert r.status_code == 302 and S.load()["budget"] == 3000 and S.load()["scour"] is False


# ---------------------------------------------------------------- not the real thing
@pytest.mark.parametrize("title", ["AKG C414 XLS Box", "Neumann U87 Ai original box", "Box for AKG C414",
                                   "AKG C414 retail box and foam", "Neumann TLM 103 empty box", "Universal Audio 1176 Box",
                                   "AKG C414 style condenser mic", "AKG C214 (like C414)", "Behringer B-2 Pro vs AKG C414",
                                   "C414 clone large diaphragm", "Neumann U87 manual"])
def test_boxes_and_lookalikes_are_not_flips(title):
    from gearflip import verify
    assert verify.problem(title)


@pytest.mark.parametrize("title", ["AKG C 414 B-XLS w/ case", "Neumann U87 Ai with original box", "AKG C414 XLII in box",
                                   "Radial JDI DI box", "Neumann TLM 103 mint, box, manual", "AKG C414 XLS like new",
                                   "Empirical Labs Distressor EL8 original box and manual included", "AKG C414 open box",
                                   "API 500 lunchbox", "Teletronix LA-2A needs tubes"])
def test_real_gear_with_extras_is_kept(title):
    from gearflip import verify
    assert verify.problem(title) is None


def test_far_under_value_is_not_the_real_unit():
    from gearflip import verify
    assert verify.price_problem(150, 900, None) and verify.price_problem(150, 900, "tubes") is None


# ---------------------------------------------------------------- picture analysis
def _pic(**kw):
    from gearflip import photos
    return photos._clean({"is_item": "yes", "matches_model": "yes", "condition": "good", **kw})


def test_photo_effects():
    from gearflip import photos
    assert photos.effect(_pic(is_item="no"))["drop"]
    assert "different model" in photos.effect(_pic(matches_model="no", seen_model="AKG C214"))["drop"]
    ok = photos.effect(_pic())
    assert ok["conf"] > 0 and ok["value_x"] == 1.0 and not ok["drop"]
    bad = photos.effect(_pic(condition="damaged", damage=["dented grille"], missing=["shock mount"]))
    assert bad["repair"] == "check-over" and bad["value_x"] < 0.85 and "visible damage" in bad["tags"]
    assert photos.effect(_pic(stock_photo=True))["conf"] < 0


def test_photo_check_runs_once_and_feeds_the_scan(db, monkeypatch):
    from gearflip import engine, photos, settings as S, source
    for i in range(14):
        db.mark_seen(_listing(i, "Neumann TLM 103 condenser microphone", 1000 + (i % 5) * 20, ship=0))
    l = _listing(100, "Neumann TLM 103 condenser microphone", 560, src="Craigslist — Seattle")
    l.image_url = "https://img.example.com/a.jpg"
    db.mark_seen(l)
    calls = []
    monkeypatch.setattr(source, "gemini_key", lambda cfg: "k")
    monkeypatch.setattr(source, "gemini_paused", lambda: False)  # never the Mac's real pause file
    monkeypatch.setattr(source, "photo_bytes", lambda url: ("image/jpeg", b"x"))
    monkeypatch.setattr(source, "ai_json", lambda key, prompt, photo: calls.append(prompt) or
                        '{"is_item": "no", "matches_model": "unsure", "note": "just the box"}')
    st = S.save({"min_confidence": 0})
    res = engine.scan({}, st)
    assert [o["url"] for o in res["opps"]] == ["https://example.com/100"] and res["photo_ai"]
    assert engine.plan(res, st)["items"] == []  # waits for the photo check
    assert photos.check({}, res["opps"]) == 1 and photos.check({}, res["opps"]) == 0 and len(calls) == 1
    res = engine.scan({}, st)
    assert res["opps"] == [] and res["photo_dropped"][0]["why"].startswith("the photo isn't the unit")


# ---------------------------------------------------------------- your ✕s
def test_dismissals_hide_and_teach(gf_dir):
    from gearflip import feedback as F
    m = E.analyze(title="Neumann TLM 103", ask=560, value=1000, n=14, local=True, settings=ST)
    o = {"url": "u1", "title": "Neumann TLM 103", "model": "neumann:tlm103", "seller": "bob", "m": m}
    F.dismiss(o, "price_off", "they sell for $800 here")
    assert F.hidden_urls() == {"u1"}
    rules = F.learned()
    assert "price_off:neumann:tlm103" in rules
    a = F.adjust(rules, model="neumann:tlm103", title="Neumann TLM 103", repair=None, seller=None)
    assert a["value_x"] == pytest.approx(0.92) and not a["drop"]
    F.dismiss({**o, "url": "u2"}, "not_my_gear")
    F.dismiss({**o, "url": "u3", "model": "neumann:u87"}, "not_my_gear")
    assert F.adjust(F.learned(), model="neumann:km184", title="KM184", repair=None, seller=None)["drop"]
    F.dismiss({**o, "url": "u4"}, "seller")
    assert F.adjust(F.learned(), model="akg:c414", title="AKG C414", repair=None, seller="bob")["drop"]
    F.undo("not_my_gear:neumann")
    assert not F.adjust(F.learned(), model="neumann:km184", title="KM184", repair=None, seller=None)["drop"]
    F.dismiss({**o, "url": "u5", "model": "akg:c414"}, "wrong_model")
    F.dismiss({**o, "url": "u6", "model": "akg:c414"}, "wrong_model")
    r = F.learned()
    assert F.adjust(r, model="akg:c414", title="AKG C414", repair=None, seller=None, ask=300, value=800)["drop"]
    assert not F.adjust(r, model="akg:c414", title="AKG C414", repair=None, seller=None, ask=650, value=800)["drop"]
    ok = F.adjust(r, model="akg:c414", title="AKG C414", repair=None, seller=None, ask=300, value=800,
                  photo={"matches": "yes", "is_item": "yes"})
    assert not ok["drop"] and ok["conf"] < 0


def test_dismiss_route_removes_card_and_rescans(db, monkeypatch):
    flask = pytest.importorskip("flask")
    import jinja2
    from gearflip import engine, feedback, settings as S
    from gearflip.web import bp
    db.mark_seen(_listing(1, "Shure SM7B", 200, src="Craigslist — Seattle"))
    for i in range(2, 10):
        db.mark_seen(_listing(i, "Shure SM7B", 370 + i * 3))
    engine.scan({}, S.save({"min_confidence": 0}))
    assert engine.latest()["opps"]
    monkeypatch.setattr(engine, "scan_in_background", lambda cfg=None: False)
    app = flask.Flask(__name__)
    app.jinja_loader = jinja2.ChoiceLoader([jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                                           app.jinja_loader])
    app.register_blueprint(bp)
    c = app.test_client()
    assert b"Not this one" in c.get("/gearflip", headers={"X-GearScout-Test": "1"}).data
    r = c.post("/gearflip/dismiss", data={"url": "https://example.com/1", "reason": "too_big", "note": "no room"})
    assert r.status_code == 302 and feedback.load()["items"][0]["note"] == "no room"
    page = c.get("/gearflip?thanks=1", headers={"X-GearScout-Test": "1"}).data
    assert b"https://example.com/1" not in page and b"What Gear Flip learned" in page
    assert engine.scan({}, S.load())["opps"] == []


def test_last_six_months_beat_older_prices(db):
    from gearflip import source
    from scrapers.store import _conn
    for i in range(6):
        db.mark_seen(_listing(i, "Shure SM7B", 300 + i))
    for i in range(6, 12):
        db.mark_seen(_listing(i, "Shure SM7B", 450 + i))
    old = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
    with _conn() as c:
        c.execute("UPDATE seen SET first_seen = ? WHERE url IN (" + ",".join(f"'https://example.com/{i}'" for i in range(6, 12)) + ")", (old,))
        c.commit()
    rv = source.recent_values()["shure:sm7b"]
    assert rv["ask_n"] == 6 and 300 <= rv["ask"] <= 306
    db.mark_seen(_listing(99, "Shure SM7B", 150, src="Craigslist — Seattle"))
    rows = [r for r in source.listing_rows() if r["url"].endswith("/99")]
    p = source.prepare(rows[0], {}, True)
    assert p and 300 <= p["value"] <= 306 and p["n"] == 6


def test_every_sort_option_works(db, monkeypatch):
    flask = pytest.importorskip("flask")
    import jinja2
    from gearflip import engine, settings as S
    from gearflip.web import SORTS, bp
    db.mark_seen(_listing(1, "Shure SM7B", 200, src="Craigslist — Seattle"))
    db.mark_seen(_listing(50, "Shure SM7B", 215, src="Craigslist — Seattle"))
    for i in range(2, 10):
        db.mark_seen(_listing(i, "Shure SM7B", 370 + i * 3))
    engine.scan({}, S.save({"min_confidence": 0}))
    monkeypatch.setattr(engine, "scan_in_background", lambda cfg=None: False)
    app = flask.Flask(__name__)
    app.jinja_loader = jinja2.ChoiceLoader([jinja2.DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                                           app.jinja_loader])
    app.register_blueprint(bp)
    c = app.test_client()
    for k in SORTS:
        for d in ("", "rev"):
            assert c.get(f"/gearflip?sort={k}&dir={d}", headers={"X-GearScout-Test": "1"}).status_code == 200
    urls = [o["url"] for o in engine.latest()["opps"]]
    assert "https://example.com/50" in urls, [(o["url"], o["m"]["expected"], o["m"]["roi"]) for o in engine.latest()["opps"]]
    a = c.get("/gearflip?sort=cheapest", headers={"X-GearScout-Test": "1"}).data.split(b'id="all"')[1]
    b = c.get("/gearflip?sort=cheapest&dir=rev", headers={"X-GearScout-Test": "1"}).data.split(b'id="all"')[1]
    i1, i50 = a.find(b"example.com/1\""), a.find(b"example.com/50\"")
    assert 0 < i1 < i50 and b.find(b"example.com/50\"") < b.find(b"example.com/1\"")


def test_photo_wait_falls_back_after_30_minutes_or_when_gemini_rests():
    from gearflip import engine
    now = datetime.now(timezone.utc)
    new = {"first_seen": (now - timedelta(minutes=5)).isoformat()}
    old = {"first_seen": (now - timedelta(minutes=45)).isoformat()}
    on = {"photo_ai": True}
    assert engine.photo_wait(new, on, now) and not engine.photo_wait(old, on, now)
    assert not engine.photo_wait(new, {"photo_ai": True, "photo_paused": True}, now)
    assert not engine.photo_wait({**new, "photo": {"is_item": "yes"}}, on, now)
    assert not engine.photo_wait(new, {}, now)


def test_resale_is_a_quick_sale_price_not_the_typical_ask():
    from gearflip import source
    asks = [800, 850, 900, 950, 1000, 1100, 1300, 1500]
    q = source.quick_sale(asks)
    assert 840 <= q <= 900  # the lower quarter, not the ~$975 median
    assert source.quick_sale([500, 600]) is None and source.quick_sale([500, 600, 700]) == 500


@pytest.mark.parametrize("title,bad", [
    ("Silver TLM103 Neumann Wired Condenser Microphone Matte US STOCK", True),
    ("Black TLM103 Neumann Wired Condenser Microphone Matte US Fast Shipping", True),
    ("BM-800 condenser microphone for PC gaming podcast", True),
    ("Neumann TLM 103 condenser microphone", False),
    ("Shure SM7B", False),
    ("Black Lion Audio Auteur MKII preamp", False),
    ("Black Lion Audio B12A mic preamp", False),
])
def test_knockoff_listings(title, bad):
    from gearflip import verify
    assert verify.knockoff(title) is bad


def test_too_cheap_ads_are_held_then_photo_checked(db, monkeypatch):
    from gearflip import engine, photos, settings as S, source
    for i in range(10):
        db.mark_seen(_listing(i, "Shure SM7B", 380 + i * 4))
    l = _listing(77, "Shure SM7B", 120, src="Craigslist — Seattle")
    l.image_url = "https://img.example.com/sm7b.jpg"
    db.mark_seen(l)
    st = S.save({"min_confidence": 0})
    res = engine.scan({}, st)
    assert [h["url"] for h in res["held"]] == ["https://example.com/77"]
    monkeypatch.setattr(source, "gemini_key", lambda cfg: "k")
    monkeypatch.setattr(source, "photo_bytes", lambda url: ("image/jpeg", b"x"))
    monkeypatch.setattr(source, "ai_json", lambda key, prompt, photo:
                        '{"is_item": "yes", "matches_model": "yes", "condition": "good"}')
    assert photos.check({}, (res.get("opps") or []) + res["held"]) == 1
    res = engine.scan({}, st)
    assert "https://example.com/77" in [o["url"] for o in res["opps"]] and not res["held"]


def test_no_model_value_falls_back_to_similar_ads(db):
    from gearflip import source
    for i in range(8):
        db.mark_seen(_listing(i, "Zorblax ZX-9000 tube preamp", 400 + i * 10))
    v = source.similar_value("Zorblax ZX-9000 tube preamp", "https://example.com/x")
    assert v and 400 <= v[0] <= 430 and v[1] == 8
    assert source.similar_value("Zorblax tube preamp") is None  # no model number: too loose
