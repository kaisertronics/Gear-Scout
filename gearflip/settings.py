"""Gear Flip's own settings, kept in /data/gearflip/settings.json (never in
Gear Scout's config.yaml). GEARFLIP_DIR moves the whole data folder (tests)."""
import json
import os
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("GEARFLIP_DIR") or "/data/gearflip")


DEFAULTS = {
    "budget": 500,               # starting investment, dollars (bk: start small, $400-500)
    "include_repairs": True,     # consider broken / "for parts" gear
    "min_profit": 40,            # expected profit per item, after everything
    "min_roi": 25,               # percent of the money you put in
    "min_confidence": 35,        # 0-100
    "max_days": 75,              # skip things that would take longer than this to sell
    "sell_on": "best",           # best | reverb | ebay | local
    "buy_tax_pct": 10.4,         # sales tax Reverb/eBay/shops charge you (Edmonds, WA); local private sales: none
    "local_pickup_cost": 15,     # gas and time for a local pickup
    "max_per_model": 1,          # in the plan: how many of the same model to buy at once
    # Alerts
    "alerts": True,
    "alert_min_profit": 60,
    "alert_min_score": 50,
    "max_pushes_per_scan": 3,
    "email_digest": True,        # one morning email with the plan
    "ntfy_topic": "",            # blank = use Gear Scout's phone alerts topic
    # Constant scanning
    "scour": True,               # Gear Flip's own extra Reverb/eBay searches
    "scan_minutes": 15,
}

_LIMITS = {"budget": (50, 1_000_000), "min_profit": (0, 100_000), "min_roi": (0, 1000), "min_confidence": (0, 100),
           "max_days": (3, 365), "buy_tax_pct": (0, 20), "local_pickup_cost": (0, 500), "max_per_model": (1, 5),
           "alert_min_profit": (0, 100_000), "alert_min_score": (0, 100), "max_pushes_per_scan": (0, 10),
           "scan_minutes": (10, 240)}
_CHOICES = {"sell_on": ("best", "reverb", "ebay", "local")}


def path() -> Path:
    return data_dir() / "settings.json"


def clean(raw: dict) -> dict:
    """Defaults filled in, numbers clamped, unknown keys dropped."""
    out = dict(DEFAULTS)
    for k, default in DEFAULTS.items():
        if k not in (raw or {}):
            continue
        v = raw[k]
        if isinstance(default, bool):
            out[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
        elif isinstance(default, (int, float)):
            try:
                n = float(str(v).replace("$", "").replace(",", "").strip())
            except ValueError:
                continue
            lo, hi = _LIMITS.get(k, (float("-inf"), float("inf")))
            n = min(max(n, lo), hi)
            out[k] = int(n) if isinstance(default, int) and n == int(n) else n
        elif k in _CHOICES:
            out[k] = v if v in _CHOICES[k] else default
        else:
            out[k] = str(v).strip()[:200]
    return out


def load() -> dict:
    try:
        return clean(json.loads(path().read_text()))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(values: dict) -> dict:
    s = clean({**load(), **(values or {})})
    write_json(path(), s)
    return s


def write_json(p: Path, data) -> None:
    """Atomic write (a half-written file is never read)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, p)


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default
