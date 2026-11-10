"""Gear Flip's own deep scour, on top of everything Gear Scout already
collects (Facebook Marketplace, Craigslist, eBay, Reverb, auctions...).

Each pass makes a few gentle requests: two Reverb searches for gear that
resells well (rotating through FLIP_TERMS, so the whole list is covered every
few hours), one Reverb read of the newest broken / for-parts pro audio, and
every other pass one eBay search (about 50 eBay calls a day out of ~5,000).
No Facebook, no logins, no captchas. Finds are stored with Gear Scout's
listings as search-only finds (they don't fill Gear Scout's own feeds)."""
import logging
import time
from datetime import datetime, timezone

from gearflip import settings as S, source

logger = logging.getLogger(__name__)

# Studio gear that sells quickly and holds its value: the flip sweet spot.
FLIP_TERMS = [
    "Neumann U87", "Neumann TLM 103", "Neumann KM184", "Neumann U47 fet", "AKG C414", "AKG C12", "Shure SM7B",
    "Royer R-121", "Coles 4038", "Telefunken", "Sennheiser MD421", "Sennheiser MKH 416", "Schoeps", "Beyerdynamic M160",
    "Mojave Audio", "Manley Reference", "Soyuz", "Wunder Audio", "Peluso", "Electro-Voice RE20",
    "Universal Audio 1176", "Universal Audio LA-2A", "Teletronix LA-2A", "Universal Audio 610", "Universal Audio Apollo",
    "Empirical Labs Distressor", "Tube-Tech CL 1B", "Manley Variable Mu", "Manley Massive Passive", "SSL G bus compressor",
    "Neve 1073", "AMS Neve", "BAE 1073", "Chandler TG2", "Chandler Germanium", "API 512c", "API 550", "API 2500",
    "API 500 series", "Rupert Neve Designs", "Great River", "Vintech", "Heritage Audio", "Purple Audio",
    "Pultec EQP-1A", "Warm Audio", "Avalon VT-737", "Millennia HV-3", "Daking", "Retro Instruments",
    "Bricasti M7", "Eventide H9000", "Eventide H3000", "Lexicon 480L", "Lexicon PCM", "Strymon",
    "Apogee Symphony", "RME Fireface", "Antelope Orion", "Burl Audio", "Lynx Aurora",
    "Focal Twin6", "Genelec 8040", "Neumann KH 310", "Barefoot MicroMain", "Yamaha NS-10", "Adam A7X", "Amphion",
    "Moog Minimoog", "Sequential Prophet", "Roland Juno", "Elektron", "Akai MPC", "Roland TR-8S", "Oberheim",
    "Dangerous Music", "SPL", "Thermionic Culture", "Elysia", "Black Lion Audio", "Undertone Audio", "Kush Audio",
]
REVERB_TERMS_PER_PASS = 2
EBAY_EVERY = 2          # one eBay search every Nth pass
REPAIR_QUERIES = ["", "for parts", "not working", "needs repair"]  # "" = newest non-functioning, no keyword


def _state_path():
    return S.data_dir() / "scour_state.json"


def run(cfg: dict, st: dict) -> dict:
    """One gentle pass. Returns what it did."""
    state = S.read_json(_state_path(), {}) or {}
    i, runs = int(state.get("i", 0)), int(state.get("runs", 0))
    out = {"requests": 0, "seen": 0, "new": 0, "terms": [], "errors": []}
    t0 = time.time()
    price_max = int(st.get("budget", 2000))
    batches = []
    for k in range(REVERB_TERMS_PER_PASS):
        term = FLIP_TERMS[(i + k) % len(FLIP_TERMS)]
        batches.append(("reverb", term, {"query": term, "condition": "used", "price_max": price_max}))
    rq = REPAIR_QUERIES[runs % len(REPAIR_QUERIES)]
    batches.append(("reverb", f"broken gear: {rq or 'newest'}",
                    {"condition": "non-functioning", "price_max": price_max, **({"query": rq} if rq else {})}))
    for kind, label, params in batches:
        try:
            out["requests"] += 1
            found = source.reverb_search(params)
            out["seen"] += len(found)
            out["new"] += source.store_listings(found, cfg)
            out["terms"].append(label)
        except Exception as e:
            logger.warning("Gear Flip scour: Reverb %s failed: %s", label, e)
            out["errors"].append(f"Reverb {label}: {e}"[:160])
        time.sleep(1.5)  # be polite
    if runs % EBAY_EVERY == 0:
        term = FLIP_TERMS[int(state.get("ebay_i", 0)) % len(FLIP_TERMS)]
        try:
            found = source.ebay_search(term, cfg)
            if found:
                out["requests"] += 1
                out["seen"] += len(found)
                out["new"] += source.store_listings(found, cfg)
                out["terms"].append(f"eBay: {term}")
        except Exception as e:
            logger.warning("Gear Flip scour: eBay %s failed: %s", term, e)
            out["errors"].append(f"eBay {term}: {e}"[:160])
        state["ebay_i"] = int(state.get("ebay_i", 0)) + 1
    out["seconds"] = round(time.time() - t0, 1)
    totals = state.get("totals") or {}
    totals = {k: int(totals.get(k, 0)) + out[k] for k in ("requests", "seen", "new")}
    state.update(i=i + REVERB_TERMS_PER_PASS, runs=runs + 1, totals=totals,
                 last=dict(out, at=datetime.now(timezone.utc).isoformat()))
    S.write_json(_state_path(), state)
    return out


def status() -> dict:
    s = S.read_json(_state_path(), {}) or {}
    return {"runs": s.get("runs", 0), "totals": s.get("totals") or {}, "last": s.get("last") or {},
            "terms": len(FLIP_TERMS)}
