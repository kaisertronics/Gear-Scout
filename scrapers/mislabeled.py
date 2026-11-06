"""
Mislabeled-gear finder: an ad titled "old tube mic" or "studio compressor"
is often a bargain because nobody searches for those words. The AI looks at
the photo and names the brand and model; if it's confident and the ad is at
least 50% under that model's used value (and $300+), it's shown on Home.

  run(cfg)   — asks about a few new vague ads (cached; free Gemini tier)
  find(cfg)  — the bargains among the ads already asked about
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DAYS = 21               # only ads first seen within this many days
PER_RUN = 6             # new ads asked about per call (free-tier friendly)
MIN_CONFIDENCE = 0.7
MIN_PRICE = 300         # same floor as Mic Focus
UNDER = 0.5             # at least this far under used value

_VAGUE = re.compile(r"\b(?:mic(?:rophone)?s?|ribbon|tube|valve|condenser|compressors?|limiters?|pre-?amps?|preamplifiers?|"
                    r"channel ?strips?|outboard)\b", re.I)
# A brand or a model code ("U87", "LA-2A", "1176") means it isn't vague.
_BRANDS = (r"\b(?:api|neve|ssl|dbx|urei|teletronix|universal audio|ua|empirical|chandler|manley|avalon|focusrite|presonus|"
           r"behringer|tascam|alesis|rupert|summit|tube-?tech|retro|bellari|art|fmr|aea|royer|rca|ampex|altec|"
           r"sennheiser|shure|rode|audio-technica|electro-voice|beyerdynamic|cad|mxl|blue|sony|yamaha)\b")
_MODEL = re.compile(r"\b[a-z]{0,4}[\s-]?\d{2,}[a-z]*\b", re.I)

PROMPT = """You're helping a used pro-audio buyer. The listing title is vague: {title}
Look at the photo and name the exact brand and model if you can read or recognise it.
Answer in JSON: {{"brand": "<brand or empty>", "model": "<model or empty>", "confidence": <0 to 1>}}
Only be confident (0.7+) when the badge, label or shape makes it clear; otherwise use a low number."""


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS mislabeled_checks (url TEXT PRIMARY KEY, brand TEXT, model TEXT, confidence REAL, at TEXT)")


def is_vague(title: str) -> bool:
    from scrapers.mic_focus import _BRANDS as MIC_BRANDS, _PART, classify
    if not title or len(title) > 60 or not _VAGUE.search(title) or _PART.search(title):
        return False
    return not (re.search(_BRANDS, title, re.I) or MIC_BRANDS.search(title) or _MODEL.search(title) or classify(title))


def _candidates() -> list[dict]:
    from scrapers.enrich import parse_price
    from scrapers.store import _conn
    cut = (datetime.now(timezone.utc) - timedelta(days=DAYS)).isoformat()
    sql = ("SELECT * FROM seen WHERE url LIKE 'http%' AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0"
           " AND COALESCE(pending, 0) = 0 AND price IS NOT NULL AND image_url IS NOT NULL AND image_url != ''"
           " AND (country IS NULL OR country = 'US') AND first_seen >= ? ORDER BY first_seen DESC")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, (cut,))]
    return [r for r in rows if (parse_price(r.get("price")) or 0) >= MIN_PRICE
            and not (r.get("description") or "").startswith("Auction") and is_vague(r.get("title") or "")]


def run(cfg: dict, limit: int = PER_RUN) -> int:
    """Has the AI name the gear in the photos of vague ads not asked about
    yet. Returns how many were checked."""
    key = ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()
    if not key:
        return 0
    from scrapers.assistant import _generate, _photo
    from scrapers.store import _conn
    rows = _candidates()
    with _conn() as conn:
        _ensure(conn)
        done = {u for (u,) in conn.execute("SELECT url FROM mislabeled_checks")}
    n = 0
    for r in rows:
        if n >= limit:
            break
        if r["url"] in done:
            continue
        photo = _photo(r.get("image_url"))
        if not photo:
            continue
        try:
            out = json.loads(_generate(key, PROMPT.format(title=r.get("title") or ""), json_mode=True, images=[photo]))
            conf = float(out.get("confidence") or 0)
        except Exception as e:
            logger.info("Mislabeled check failed for %s: %s", r["url"], e)
            break  # rate limit or outage: try the rest next time
        with _conn() as conn:
            _ensure(conn)
            conn.execute("INSERT OR REPLACE INTO mislabeled_checks VALUES (?, ?, ?, ?, ?)",
                         (r["url"], str(out.get("brand") or "").strip()[:60], str(out.get("model") or "").strip()[:60],
                          max(0.0, min(1.0, conf)), datetime.now(timezone.utc).isoformat()))
            conn.commit()
        n += 1
    return n


def find(cfg: dict) -> list[dict]:
    """Vague ads the AI was confident about that are 50%+ under that model's
    used value, with r['guess'] and r['used_value']. Biggest discount first."""
    from scrapers.enrich import parse_price
    from scrapers.market_value import value
    from scrapers.store import _conn
    with _conn() as conn:
        _ensure(conn)
        guesses = {u: f"{b} {m}".strip() for u, b, m, c in conn.execute(
            "SELECT url, brand, model, confidence FROM mislabeled_checks") if b and m and (c or 0) >= MIN_CONFIDENCE}
    out = []
    for r in _candidates() if guesses else []:
        guess = guesses.get(r["url"])
        price = parse_price(r.get("price"))
        if not guess or not price or price < MIN_PRICE:
            continue
        used = value(guess)[0]
        if used and price <= used * (1 - UNDER):
            out.append(dict(r, guess=guess, used_value=round(used)))
    out.sort(key=lambda r: parse_price(r.get("price")) / r["used_value"])
    return out
