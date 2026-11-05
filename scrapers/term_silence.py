"""
Wanted-term silence alert.

A Wanted term that used to return matches (>= PEAK_MIN at some earlier sweep)
but has returned 0 matches for the last SILENT_RUN sweeps in a row probably
means the search is broken (a site changed). record() is called from the
Wanted sweep; silent_terms() lists the suspects for the Health page.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)
STATE_PATH = Path("/data/term_silence.json")
PEAK_MIN = 5
SILENT_RUN = 6


def _load() -> dict:
    try:
        d = json.loads(STATE_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STATE_PATH)


def record(term: str, matches: int, label: str = "") -> None:
    """Remember one sweep result. Never raises."""
    try:
        key = f"{term}|{label}" if label else str(term)
        d = _load()
        e = d.get(key) or {"peak": 0, "recent": []}
        recent = list(e.get("recent") or [])
        recent.append(int(matches))
        while len(recent) > SILENT_RUN:
            e["peak"] = max(int(e.get("peak") or 0), recent.pop(0))
        e["recent"] = recent
        d[key] = e
        _save(d)
    except Exception:
        logger.debug("term_silence: record failed", exc_info=True)


def silent_terms() -> list[str]:
    """Terms (with site kind) that had >= PEAK_MIN matches earlier but 0 for the last SILENT_RUN sweeps."""
    out = []
    for key, e in _load().items():
        try:
            recent = e.get("recent") or []
            if len(recent) >= SILENT_RUN and not any(recent[-SILENT_RUN:]) and int(e.get("peak") or 0) >= PEAK_MIN:
                term, _, label = key.partition("|")
                out.append(f"{term} ({label})" if label else term)
        except Exception:
            continue
    return sorted(out)
