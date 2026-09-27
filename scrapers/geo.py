"""
Distance from the user's ZIP code to a listing's town, for local-pickup
sources (Facebook Marketplace, Craigslist).

Uses the GeoNames US postal-code file (https://www.geonames.org, CC BY 4.0),
downloaded once into /data and looked up locally — no per-listing requests
to any outside service.
"""
import io
import logging
import math
import re
import threading
import zipfile
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DATA_URL = "https://download.geonames.org/export/zip/US.zip"
DATA_PATH = Path("/data/geo/US.txt")

_lock = threading.Lock()
_zip: dict[str, tuple[float, float]] = {}
_place: dict[tuple[str, str], tuple[float, float]] = {}
_place_states: dict[str, set[str]] = {}
_loaded = False


def _load() -> bool:
    global _loaded
    with _lock:
        if _loaded:
            return bool(_zip)
        _loaded = True
        try:
            if not DATA_PATH.exists():
                resp = requests.get(DATA_URL, timeout=60)
                resp.raise_for_status()
                DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
                DATA_PATH.write_bytes(zipfile.ZipFile(io.BytesIO(resp.content)).read("US.txt"))
            sums: dict[tuple[str, str], list[float]] = {}
            for line in DATA_PATH.read_text(encoding="utf-8").splitlines():
                cols = line.split("\t")
                if len(cols) < 11 or not cols[9] or not cols[10]:
                    continue
                zip_code, place, state = cols[1], cols[2].lower(), cols[4].upper()
                lat, lon = float(cols[9]), float(cols[10])
                _zip[zip_code] = (lat, lon)
                acc = sums.setdefault((place, state), [0.0, 0.0, 0])
                acc[0] += lat
                acc[1] += lon
                acc[2] += 1
                _place_states.setdefault(place, set()).add(state)
            for key, (la, lo, n) in sums.items():
                _place[key] = (la / n, lo / n)
        except Exception as e:
            logger.warning("Couldn't load US postal-code data for distances: %s", e)
        return bool(_zip)


def zip_coords(zip_code: Optional[str]) -> Optional[tuple[float, float]]:
    z = re.sub(r"\D", "", zip_code or "")[:5]
    if len(z) != 5 or not _load():
        return None
    return _zip.get(z)


def place_coords(location: Optional[str]) -> Optional[tuple[float, float]]:
    """"Claremont, CA" -> coordinates. A bare town name ("Sherman Oaks", as
    Craigslist gives it) only resolves if that name exists in one state —
    ambiguous names like "Springfield" are skipped rather than guessed."""
    if not location or not _load():
        return None
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return None
    place = parts[0].lower()
    state = parts[1].upper() if len(parts) > 1 and re.fullmatch(r"[A-Za-z]{2}", parts[1]) else None
    if state:
        return _place.get((place, state))
    states = _place_states.get(place)
    if states and len(states) == 1:
        return _place.get((place, next(iter(states))))
    return None


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def distance_miles(home_zip: Optional[str], location: Optional[str]) -> Optional[int]:
    home = zip_coords(home_zip)
    there = place_coords(location)
    if not home or not there:
        return None
    return round(miles_between(home, there))
