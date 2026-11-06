"""The Gear Flip tab: a Flask blueprint mounted at /gearflip.

The page only reads saved files (the latest scan, your ledger, settings), so it
opens instantly; scans run in the background. Its template extends the host
app's base.html for the shared top bar; a stand-alone Gear Flip would ship its
own base.html with the same block names."""
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from gearflip import economics, engine, jobs, ledger, scour, settings as S

bp = Blueprint("gearflip", __name__, url_prefix="/gearflip", template_folder="templates")

SORTS = {
    "score": lambda o: -o["m"]["score"],
    "profit": lambda o: -o["m"]["expected"],
    "roi": lambda o: -o["m"]["roi"],
    "speed": lambda o: o["m"]["days"],
    "confidence": lambda o: -o["m"]["confidence"],
    "newest": lambda o: o["age_days"] if o.get("age_days") is not None else 999,
}


def _ago(iso) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
        s = (datetime.now(timezone.utc) - (d if d.tzinfo else d.replace(tzinfo=timezone.utc))).total_seconds()
    except (TypeError, ValueError):
        return ""
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


def _age(days) -> str:
    if days is None:
        return ""
    if days < 1:
        return f"{max(1, int(days * 24))} h old"
    return f"{int(days)} d old"


@bp.app_template_filter("gf_money")
def _money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    return ("-$" if v < 0 else "$") + f"{abs(v):,.0f}"


@bp.route("")
@bp.route("/")
def index():
    st = S.load()
    res = engine.latest()
    stale = not res or time.time() - _ts(res.get("at")) > st["scan_minutes"] * 60 * 2 or res.get("settings") != st
    if stale and not request.headers.get("X-GearScout-Test"):
        engine.scan_in_background()
    book = ledger.bankroll(st["budget"])
    plan = engine.plan(res, st, book)
    owned = ledger.open_urls()
    opps = [o for o in (res.get("opps") or []) if economics.passes(o["m"], st)]
    kind = request.args.get("kind", "all")
    if kind == "working":
        opps = [o for o in opps if not o["m"]["repair"]]
    elif kind == "repair":
        opps = [o for o in opps if o["m"]["repair"]]
    elif kind == "local":
        opps = [o for o in opps if o["m"]["pickup"]]
    sort = request.args.get("sort", "score")
    opps = sorted(opps, key=SORTS.get(sort, SORTS["score"]))
    sc = scour.status()
    hours = round(len(scour.FLIP_TERMS) / scour.REVERB_TERMS_PER_PASS * st["scan_minutes"] / 60, 1)
    return render_template("gearflip/index.html", st=st, res=res, book=book, plan=plan, opps=opps[:80],
                           total=len(opps), kind=kind, sort=sort, owned=owned, scanning=engine.scanning(),
                           scan_ago=_ago(res.get("at")) if res else "", job=jobs.status(), sc=sc, cover_hours=hours,
                           age=_age, ago=_ago, channels=economics.CHANNELS, repairs=economics.REPAIRS,
                           saved=request.args.get("saved"))


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


@bp.route("/settings", methods=["POST"])
def save_settings():
    f = request.form
    vals = {k: f.get(k) for k in S.DEFAULTS if k in f and not isinstance(S.DEFAULTS[k], bool)}
    for k, d in S.DEFAULTS.items():  # checkboxes: missing = off
        if isinstance(d, bool):
            vals[k] = k in f
    S.save(vals)
    engine.scan_in_background()
    return redirect(url_for("gearflip.index", saved=1) + "#settings")


@bp.route("/scan", methods=["POST"])
def scan_now():
    engine.scan_in_background()
    return redirect(url_for("gearflip.index"))


def _find(url: str):
    for o in engine.latest().get("opps") or []:
        if o.get("url") == url:
            return o
    return None


@bp.route("/buy", methods=["POST"])
def buy():
    url = request.form.get("url") or ""
    o = _find(url) or {"url": url, "title": request.form.get("title") or url, "source": "", "m": {}}
    ledger.buy(o, request.form.get("paid"))
    return redirect(url_for("gearflip.index") + "#mine")


@bp.route("/repaired", methods=["POST"])
def repaired():
    ledger.repaired(request.form.get("id") or "", request.form.get("cost"))
    return redirect(url_for("gearflip.index") + "#mine")


@bp.route("/sold", methods=["POST"])
def sold():
    f = request.form
    ledger.sold(f.get("id") or "", f.get("price"), f.get("ship"), f.get("channel") or "reverb")
    return redirect(url_for("gearflip.index") + "#mine")


@bp.route("/remove", methods=["POST"])
def remove():
    ledger.remove(request.form.get("id") or "")
    return redirect(url_for("gearflip.index") + "#mine")


# JSON for a future stand-alone app or phone widget
@bp.route("/api/opportunities.json")
def api_opps():
    res = engine.latest()
    return jsonify({"at": res.get("at"), "found": res.get("found"), "opportunities": res.get("opps") or []})


@bp.route("/api/plan.json")
def api_plan():
    st = S.load()
    book = ledger.bankroll(st["budget"])
    plan = engine.plan(None, st, book)
    return jsonify({"bankroll": {k: v for k, v in book.items() if not k.endswith("_items")},
                    "plan": plan["t"], "buys": plan["items"]})


@bp.route("/api/status.json")
def api_status():
    return jsonify({"job": jobs.status(), "scour": scour.status(), "scanning": engine.scanning()})
