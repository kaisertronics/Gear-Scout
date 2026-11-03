"""
Checks on a deal before it's put in front of you (owner, Oct 31):

  scam_flags   — signs an ad is a scam or a mix-up
  photo_check  — the AI looks at the photo: is it the gear in the title?
  score        — one number to sort deals by (discount, freshness, drop, distance)
  offer        — a suggested offer and message for ads that dropped or sat
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_LOCAL = re.compile(r"facebook|craigslist|offerup|kijiji|nextdoor", re.I)
_SCAM_TEXT = [
    (re.compile(r"\bbrand new\b.*\b(?:\d+|two|several|multiple) (?:available|units|in stock|left)\b|"
                r"\b(?:\d+|two|several) (?:available|in stock)\b", re.I), "a 'brand new, several available' ad"),
    (re.compile(r"\bship(?:ping)? only\b|\bonly ship\b|\bno local pick ?up\b", re.I), "shipping only"),
    (re.compile(r"\bzelle\b|\bcash ?app\b|\bwestern union\b|\bwire transfer\b|\bgift cards?\b|\bcrypto\b|\bbitcoin\b", re.I),
     "asks for Zelle / Cash App / wire"),
    (re.compile(r"[\w.+-]+@[\w-]+\.\w{2,}|\btext me\b|\bcontact me (?:at|on)\b|\bwhats ?app\b", re.I),
     "wants contact off the site"),
]


def scam_flags(r: dict, used: Optional[float], unit: float) -> list[str]:
    """Plain-English reasons to distrust an ad (empty = none seen)."""
    text = f"{r.get('title') or ''} {r.get('description') or ''}"
    out = [why for rx, why in _SCAM_TEXT if rx.search(text)]
    local = bool(_LOCAL.search(r.get("source_name") or ""))
    if used and local and unit < used * 0.35:
        out.append(f"a local ad at {round((1 - unit / used) * 100)}% under")
    return out


def photo_reuse(rows: list[dict]) -> dict:
    """{url: reason} for ads whose photo also appears on a different ad
    (another title or place) — stolen photos are a classic scam."""
    by_img: dict = {}
    for r in rows:
        img = (r.get("image_url") or "").split("?")[0]
        if img:
            by_img.setdefault(img, []).append(r)
    out = {}
    for img, rs in by_img.items():
        places = {((x.get("title") or "").lower()[:40], (x.get("location") or "").lower()) for x in rs}
        if len(rs) > 1 and len(places) > 1:
            for x in rs:
                out[x["url"]] = "the same photo is on another ad"
    return out


_PLACEHOLDER = re.compile(r"placeholder|no[-_]?image|noimage|default|blank|coming[-_]?soon|logo|spacer|avatar|\.gif", re.I)
_MAX_SELLERS = 5   # a photo on more ad-owners than this is a stock/catalogue shot


def _img_key(url: Optional[str]) -> str:
    return (url or "").split("?")[0]


def stolen_photos(conn, imgs: set) -> set:
    """Photo addresses (without ?query) from `imgs` that ALSO appear on an ad
    by a different seller (or, when no seller is known, a different source or
    place). Same seller reposting is fine; stock photos shared by many
    sellers and placeholder images are ignored. Uses stored rows only."""
    imgs = {i for i in imgs if i and not _PLACEHOLDER.search(i)}
    if not imgs:
        return set()
    owners: dict = {}
    for img, seller, src, loc in conn.execute(
            "SELECT substr(image_url, 1, CASE WHEN instr(image_url, '?') > 0 THEN instr(image_url, '?') - 1"
            " ELSE length(image_url) END), seller, source_name, location"
            " FROM seen WHERE image_url IS NOT NULL AND duplicate = 0"):
        if img in imgs:
            who = ("s", seller.strip().lower()) if seller and seller.strip() else \
                  ("p", (src or "").lower(), (loc or "").lower())
            owners.setdefault(img, set()).add(who)
    return {img for img, w in owners.items() if 1 < len(w) <= _MAX_SELLERS}


def score(r: dict, pct: int) -> int:
    """Discount first, then a bit for: posted this week (+12) or month (+6),
    a price drop (+8), local pickup within 150 miles (+5); scam signs −40."""
    s = pct
    when = r.get("posted_at") or r.get("first_seen") or ""
    try:
        d = datetime.fromisoformat(when)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - d
        s += 12 if age < timedelta(days=7) else (6 if age < timedelta(days=30) else 0)
    except (TypeError, ValueError):
        pass
    if r.get("previous_price"):
        s += 8
    if r.get("miles") is not None and r["miles"] <= 150:
        s += 5
    if r.get("flags"):
        s -= 40
    return s


_HAGGLE = re.compile(
    r"\bobo\b|\bor\s+best\s+offer\b|\bbest\s+offer\b|\bmake\s+(?:me\s+)?an?\s+offer\b|\bmake\s+(?:me\s+)?offer\b"
    r"|\bmust\s+sell\b|\bneed\s+to\s+sell\b|\bwilling\s+to\s+negotiate\b|\b(?:price\s+)?negotiable\b"
    r"|\bopen\s+to\s+(?:reasonable\s+)?offers\b", re.I)
_NO_HAGGLE = re.compile(r"\bfirm\b|\bno\s+(?:\w+\s+)?offers?\b|\bno\s+trades?\s+or\s+offers?\b|\bno\s+lowball", re.I)


def haggle(r: dict) -> bool:
    """True when the ad invites offers (OBO, make offer, must sell...) and isn't firm."""
    text = f"{r.get('title') or ''} {r.get('description') or ''}"
    if _NO_HAGGLE.search(text):
        return False
    return bool(_HAGGLE.search(text))


def offer(r: dict, unit: float, used: Optional[float]) -> Optional[dict]:
    """For an ad whose price dropped or that's sat 3+ weeks: a suggested
    offer (about 15% under the asking price) and a short message to copy."""
    when = r.get("posted_at") or r.get("first_seen") or ""
    try:
        d = datetime.fromisoformat(when)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        weeks = (datetime.now(timezone.utc) - d).days // 7
    except (TypeError, ValueError):
        weeks = 0
    if not unit or unit <= 0:
        return None
    if r.get("previous_price") or weeks >= 3:
        amt = max(25, int(round(unit * 0.85 / 25.0)) * 25)  # ~15% under: a normal opening offer
        why = "the price was just cut" if r.get("previous_price") else f"it's been up {weeks} weeks"
    else:
        amt = max(25, int(round(unit * 0.90 / 25.0)) * 25)  # ~10% under
        why = "a fair opening offer"
    open_to = haggle(r)
    if open_to:
        low = max(25, int(round(unit * 0.80 / 25.0)) * 25)  # ~20% under: the ad invites offers
        if low <= amt:
            amt, why = low, "the ad says it's open to offers"
    walk = max(25, int(round(unit * 0.95 / 25.0)) * 25)
    if used and used > 0:
        cap = used * 0.6
        walk = min(walk, cap) if cap >= amt else unit
    walk = int(round(max(walk, amt) / 25.0)) * 25
    walk = max(walk, amt)
    title = re.sub(r"\s+", " ", r.get("title") or "it")[:70]
    msg = (f"Hi! Is the {title} still available? I can pay ${amt:,} and pick up or pay for shipping "
           f"right away. Thanks!")
    out = {"amount": amt, "why": why, "msg": msg, "walk": walk}
    if open_to:
        out["haggle"] = True
    try:  # optional: what your own logged offers got accepted at (scrapers/offer_log.py)
        from scrapers.offer_log import hint
        if hint():
            out["hint"] = hint()
    except Exception:
        pass
    return out


def offers_note(r: dict, unit: float, used: Optional[float] = None) -> str:
    """For a push about a Reverb ad that accepts offers: "Offers OK · offer
    ~$850" (the usual opening offer from offer()); empty for anything else."""
    if not r.get("offers_ok"):
        return ""
    o = offer(r, unit, used)
    return "Offers OK" + (f" · offer ~${o['amount']:,}" if o else "")


# ---------------------------------------------------------------- photo check
PHOTO_PROMPT = """You're checking a used pro-audio listing photo for a buyer.
Listing title: {title}
Answer in JSON: {{"matches": "yes" | "no" | "unsure", "stock_photo": true | false, "note": "<12 words, plain English>"}}
"matches" = does the photo show the item in the title (right brand and model, not a different or cheaper item, not a clone sold as the original)?
"stock_photo" = it's a catalogue / manufacturer image, not a photo of the actual unit."""


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS photo_checks (url TEXT PRIMARY KEY, matches TEXT, stock INTEGER, note TEXT, at TEXT)")


def photo_results(urls: list[str]) -> dict:
    from scrapers.store import _conn
    if not urls:
        return {}
    with _conn() as conn:
        _ensure(conn)
        out = {}
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            for u, m, st, note in conn.execute(
                    f"SELECT url, matches, stock, note FROM photo_checks WHERE url IN ({','.join('?' * len(chunk))})", chunk):
                out[u] = {"matches": m, "stock": bool(st), "note": note}
    return out


def run_photo_checks(rows: list[dict], cfg: dict, limit: int = 12) -> int:
    """Has the AI look at the photos of deals not checked yet (a few per
    sweep; free Gemini tier). Returns how many were checked."""
    key = ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()
    if not key or not rows:
        return 0
    from scrapers.assistant import _generate, _photo
    from scrapers.store import _conn
    done = photo_results([r["url"] for r in rows])
    n = 0
    for r in rows:
        if n >= limit:
            break
        if r["url"] in done or not r.get("image_url"):
            continue
        photo = _photo(r.get("image_url"))
        if not photo:
            continue
        try:
            out = json.loads(_generate(key, PHOTO_PROMPT.format(title=r.get("title") or ""), json_mode=True, images=[photo]))
        except Exception as e:
            logger.info("Photo check failed for %s: %s", r["url"], e)
            break  # rate limit or outage: try the rest next sweep
        m = str(out.get("matches") or "unsure").lower()
        with _conn() as conn:
            _ensure(conn)
            conn.execute("INSERT OR REPLACE INTO photo_checks VALUES (?, ?, ?, ?, ?)",
                         (r["url"], m if m in ("yes", "no", "unsure") else "unsure", 1 if out.get("stock_photo") else 0,
                          str(out.get("note") or "")[:120], datetime.now(timezone.utc).isoformat()))
            conn.commit()
        n += 1
    return n
