"""
"Ask AI": a Gemini-powered assistant over everything Gear Scout has found.

1. Gemini turns your question into a search plan (terms, price limits).
2. Gear Scout searches its own listings (all sites, still for sale, junk
   filtered) and attaches each one's comp.
3. Gemini answers using only those listings, citing them as [#n], which the
   page turns into links.

Uses Google's Gemini API (free tier) with your own key from
https://aistudio.google.com — Settings → AI. Questions and the matching
listings' titles/prices are sent to Google; nothing personal is.
"""
import json
import logging
import re
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
_model_cache: dict = {"key": None, "models": None, "at": 0.0}
_busy: dict = {}  # model -> time it can be tried again


def _headers(key: str) -> dict:
    # The key goes in a header, never the URL, so it can't end up in logs.
    return {"x-goog-api-key": key}


def _pick_models(key: str) -> list[str]:
    """Usable Gemini models, best first: newest "flash" (fast, free-tier
    friendly), then the others as fallbacks when one is overloaded."""
    if _model_cache["key"] == key and _model_cache["models"] and time.time() - _model_cache["at"] < 86400:
        return _model_cache["models"]
    r = requests.get(f"{API}/models", params={"pageSize": 200}, headers=_headers(key), timeout=20)
    if r.status_code in (400, 401, 403):
        raise PermissionError("Google rejected the Gemini API key — check it in Settings → AI.")
    r.raise_for_status()
    names = [m["name"] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]

    def rank(n: str):
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return (("flash" in n) and ("lite" not in n), "preview" not in n and "exp" not in n,
                float(v.group(1)) if v else 0.0, "latest" in n)
    gem = [n for n in names if "gemini" in n and not re.search(r"tts|image|embedding|audio|live", n)]
    if not gem:
        raise RuntimeError("No Gemini text model is available for this key.")
    models = sorted(gem, key=rank, reverse=True)
    _model_cache.update(key=key, models=models, at=time.time())
    return models


def _generate(key: str, prompt: str, json_mode: bool = False, images: Optional[list] = None) -> str:
    """One Gemini call. `images`: [(mime type, bytes)] shown to the model
    alongside the text (it can look at listing photos)."""
    import base64
    parts = [{"text": prompt}]
    for mime, data in (images or [])[:3]:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.3}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    # Google sometimes answers "overloaded" (503) — wait a moment, then try
    # the next-best model.
    # Busy models are skipped for 10 minutes.
    now = time.time()
    models = _pick_models(key)[:6]
    ready = [m for m in models if _busy.get(m, 0) < now] or models
    r = None
    for model in ready[:5]:
        try:
            r = requests.post(f"{API}/{model}:generateContent", headers=_headers(key), json=body, timeout=60)
        except requests.RequestException:
            _busy[model] = time.time() + 600
            continue
        if r.status_code not in (404, 500, 502, 503, 504):
            break
        logger.info("Gemini %s busy (%s), trying another model", model, r.status_code)
        _busy[model] = time.time() + 600
    if r is None or r.status_code in (404, 500, 502, 503, 504):
        raise RuntimeError("Gemini is overloaded right now — try again in a minute.")
    if r.status_code == 429:
        raise RuntimeError("Gemini's free-tier limit was reached for now — try again in a minute.")
    if r.status_code in (400, 401, 403) and "API key" in r.text:
        raise PermissionError("Google rejected the Gemini API key — check it in Settings → AI.")
    if not r.ok:
        raise RuntimeError(f"Gemini answered with an error ({r.status_code}) — try again in a minute.")
    parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts).strip()


PLAN_PROMPT = """You help search a database of used pro-audio / studio gear listings
(microphones, preamps, compressors, EQs, consoles, interfaces, monitors, tape machines…)
collected from Facebook Marketplace, Craigslist, eBay, Reverb, OfferUp, ShopGoodwill, Kijiji and dealers.
{history}
Turn the user's latest message into a plan. Reply with JSON only:
{{"terms": [up to 5 short search terms as they'd appear in listing titles, e.g. "neve 1073", "u87",
            "la-2a", "ribbon mic"; reuse the previous terms for follow-ups like "cheaper ones"],
 "max_price": number or null, "min_price": number or null,
 "max_miles": number or null (only if they mention distance / near me),
 "deals_only": true if they want bargains / under market, else false,
 "actions": [zero or more of:
    {{"type": "add_telex", "term": "..."}}                      (track this on the Telex List),
    {{"type": "alert", "term": "...", "max_price": number|null, "max_miles": number|null}}
                                                               (tell me when one shows up / under $X / within N miles),
    {{"type": "favorite", "n": listing number from the previous answer}},
    {{"type": "live_search", "term": "..."}}                   (search every site now)],
 "search": true unless the message is only an action (e.g. "favorite #2")}}

Latest message: {q}"""

ANSWER_PROMPT = """You are Gear Scout's assistant for a used pro-audio gear buyer.
Answer using ONLY the listings below (real, currently for sale) and the conversation so far.
Be concise and practical: recommend the best options and why (price vs. comp, condition clues,
location, how fast that model sells). Cite listings as [#n] using their numbers. If nothing fits,
say so and suggest what to search instead. "comp" is what similar gear sells for; "% under" is how
far below it the listing is. Don't invent listings, prices or facts.
{history}
{actions}
Latest message: {q}

Listings:
{rows}"""


def _history_text(history: list) -> str:
    if not history:
        return ""
    out = ["\nConversation so far (most recent last):"]
    for h in history[-3:]:
        out.append(f"User: {str(h.get('q', ''))[:300]}")
        out.append(f"Assistant: {str(h.get('a', ''))[:800]}")
    return "\n".join(out) + "\n"


def run_actions(actions: list, cfg: dict, previous_ids: list) -> list[str]:
    """Carries out what the assistant was asked to do; returns plain-language
    results to show (and to tell Gemini)."""
    from scrapers.store import set_favorite
    done = []
    for a in actions or []:
        kind = (a or {}).get("type")
        try:
            if kind in ("add_telex", "alert") and a.get("term"):
                term = str(a["term"]).strip()[:80]
                add_telex_term(term)
                msg = f"Added “{term}” to your Telex List"
                if kind == "alert":
                    add_alert(term, a.get("max_price"), a.get("max_miles"))
                    cond = []
                    if a.get("max_price"):
                        cond.append(f"under ${float(a['max_price']):,.0f}")
                    if a.get("max_miles"):
                        cond.append(f"within {int(float(a['max_miles']))} miles")
                    msg = (f"Alert set: you'll get a push + email when “{term}” shows up"
                           f"{' ' + ' and '.join(cond) if cond else ''} (also on your Telex List)")
                done.append(msg)
            elif kind == "favorite" and a.get("n"):
                n = int(a["n"])
                if 1 <= n <= len(previous_ids):
                    set_favorite(previous_ids[n - 1], True)
                    done.append(f"Starred #{n}")
                else:
                    done.append(f"Couldn't find #{n} from the last answer")
            elif kind == "live_search" and a.get("term"):
                start_live_search(str(a["term"]).strip()[:80])
                done.append(f"Started a live search of every site for “{a['term']}” — results appear on the Search page in a few minutes")
        except Exception as e:
            logger.exception("Assistant action failed: %s", a)
            done.append(f"Couldn't do “{kind}”: {e}")
    return done


# Set by the dashboard at start-up (they live in its process).
add_telex_term = lambda term: None  # noqa: E731
start_live_search = lambda term: None  # noqa: E731


def add_alert(term: str, max_price=None, max_miles=None) -> None:
    from scrapers.store import _conn
    from datetime import datetime, timezone
    with _conn() as conn:
        _ensure_alerts(conn)
        conn.execute("INSERT OR REPLACE INTO ai_alerts (term, max_price, max_miles, created_at, checked_at)"
                     " VALUES (?, ?, ?, ?, ?)",
                     (term.lower(), float(max_price) if max_price else None,
                      float(max_miles) if max_miles else None,
                      datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        conn.commit()


def _ensure_alerts(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_alerts (
        term TEXT PRIMARY KEY, max_price REAL, max_miles REAL, created_at TEXT, checked_at TEXT)""")


def list_alerts() -> list[dict]:
    from scrapers.store import _conn
    with _conn() as conn:
        _ensure_alerts(conn)
        return [{"term": t, "max_price": p, "max_miles": m}
                for t, p, m in conn.execute("SELECT term, max_price, max_miles FROM ai_alerts ORDER BY created_at")]


def remove_alert(term: str) -> None:
    from scrapers.store import _conn
    with _conn() as conn:
        _ensure_alerts(conn)
        conn.execute("DELETE FROM ai_alerts WHERE term = ?", (term.lower(),))
        conn.commit()


def check_alerts(cfg: dict) -> int:
    """Pushes + emails new listings (found since the last check) matching an
    alert's term, price limit and distance. Runs hourly in the scraper."""
    import sqlite3
    from datetime import datetime, timezone
    from scrapers.base import fix_brand_spelling
    from scrapers.enrich import is_accessory_only, is_not_audio, is_partial, item_form, parse_price
    from scrapers.geo import distance_miles
    from scrapers.lowest import notify_new_lowest
    from scrapers.store import _conn
    from scrapers.telex import term_matcher
    alerts = list_alerts()
    if not alerts:
        return 0
    home = str(cfg.get("home_zip") or "")
    now = datetime.now(timezone.utc).isoformat()
    sent = 0
    with _conn() as conn:
        _ensure_alerts(conn)
        checked = {t: c for t, c in conn.execute("SELECT term, checked_at FROM ai_alerts")}
        conn.row_factory = sqlite3.Row
        oldest = min(checked.values())
        rows = [dict(r) for r in conn.execute(
            "SELECT title, price, url, source_name, image_url, location, description, first_seen FROM seen"
            " WHERE first_seen > ? AND hidden = 0 AND duplicate = 0 AND COALESCE(sold, 0) = 0", (oldest,))]
    for a in alerts:
        match = term_matcher(a["term"])
        for r in rows:
            if r["first_seen"] <= checked.get(a["term"], now):
                continue
            title = r["title"] or ""
            if not match(fix_brand_spelling(title.lower())) or is_partial(title) or is_accessory_only(title) \
                    or is_not_audio(title) or (item_form(title) == "pedal" and "pedal" not in a["term"]):
                continue
            value = parse_price(r["price"])
            if a["max_price"] and (not value or value > a["max_price"]):
                continue
            if a["max_miles"]:
                d = distance_miles(home, r.get("location")) if home else None
                if d is None or d > a["max_miles"]:
                    continue
            alert = {"title": title, "price": r["price"], "url": r["url"], "source_name": r["source_name"],
                     "image_url": r.get("image_url"), "location": (r.get("location") or "").split(" @")[0] or None,
                     "needs_repair": False, "is_clone": False}
            try:
                notify_new_lowest(cfg, f"{a['term']} (your alert)", alert, None, from_telex=True)
                sent += 1
            except Exception:
                logger.exception("Alert notification failed")
    with _conn() as conn:
        conn.execute("UPDATE ai_alerts SET checked_at = ?", (now,))
        conn.commit()
    return sent


def ask(question: str, cfg: dict, history: Optional[list] = None, previous_ids: Optional[list] = None) -> dict:
    """{'answer', 'listings', 'plan', 'actions_done'}"""
    key = ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()
    if not key:
        raise PermissionError("Add your free Gemini API key in Settings → AI first.")
    question = (question or "").strip()[:500]
    history = history or []
    try:
        plan = json.loads(_generate(key, PLAN_PROMPT.format(q=question, history=_history_text(history)),
                                    json_mode=True))
    except (ValueError, TypeError):
        plan = {"terms": [question], "search": True}
    actions_done = run_actions(plan.get("actions") or [], cfg, previous_ids or [])
    if plan.get("search") is False:
        return {"answer": "\n".join(f"- {a}" for a in actions_done) or "Done.", "listings": [],
                "plan": plan, "actions_done": actions_done}
    terms = [str(t).strip() for t in (plan.get("terms") or []) if str(t).strip()][:5] or [question]

    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import is_accessory_only, is_not_audio, item_form, parse_price
    from scrapers.geo import distance_miles
    from scrapers.market import load_market
    from scrapers.store import search_listings
    market, index, similar = load_market(), price_index(), similar_index()
    home = str(cfg.get("home_zip") or "")
    seen, found = set(), []
    for term in terms:
        for l in search_listings(term, limit=150):
            url = l.get("url")
            if not url or url in seen or l.get("sold") or l.get("hidden") or l.get("pending"):
                continue
            title = l.get("title") or ""
            if is_not_audio(title) or is_accessory_only(title) or (
                    item_form(title) == "pedal" and "pedal" not in question.lower()):
                continue
            value = parse_price(l.get("price"))
            if plan.get("max_price") and value and value > float(plan["max_price"]):
                continue
            if plan.get("min_price") and value and value < float(plan["min_price"]):
                continue
            if plan.get("max_miles") and home:
                d = distance_miles(home, l.get("location"))
                if d is None or d > float(plan["max_miles"]):
                    continue
                l["miles"] = d
            seen.add(url)
            c = evaluate(title, l.get("price"), l.get("description"), url, market, index, similar)
            if plan.get("deals_only") and not (c and c["worth"]):
                continue
            l["comp"] = c
            found.append(l)
    found.sort(key=lambda l: (-(l["comp"]["pct"] if l.get("comp") else -999), parse_price(l.get("price")) or 1e9))
    found = found[:30]
    if not found:
        msg = ("I couldn't find any matching listings Gear Scout has collected right now. "
               f"(Searched for: {', '.join(terms)}.) Try asking me to search every site for it.")
        if actions_done:
            msg = "\n".join(f"- {a}" for a in actions_done) + "\n\n" + msg
        return {"answer": msg, "listings": [], "plan": plan, "actions_done": actions_done}
    from scrapers.learning import sale_stats
    from scrapers.enrich import model_key
    sales = sale_stats()
    rows = []
    for i, l in enumerate(found, 1):
        c = l.get("comp")
        comp = (f"comp {c['label']} ~${c['ref']:,.0f} ({abs(c['pct'])}% {'under' if c['pct'] >= 0 else 'over'})"
                if c else "no comp")
        loc = (l.get("location") or "").split(" @")[0]
        sale = sales.get(model_key(l.get("title")) or "")
        speed = f" | usually sells in ~{sale['days']:.0f} days" if sale else ""
        miles = f" | {l['miles']} mi away" if l.get("miles") is not None else ""
        rows.append(f"#{i} | {l.get('title')} | {l.get('price')} | {comp} | "
                    f"{(l.get('source_name') or '').split(' — ')[0]}{' | ' + loc if loc else ''}{miles}{speed} | "
                    f"{(l.get('description') or '')[:120]}")
    acts = ("Actions already done for this message: " + "; ".join(actions_done)) if actions_done else ""
    answer = _generate(key, ANSWER_PROMPT.format(q=question, rows="\n".join(rows),
                                                 history=_history_text(history), actions=acts))
    return {"answer": answer, "listings": found, "plan": plan, "actions_done": actions_done}


# ---------------------------------------------------------------------------
# "Is this a good deal?" — one listing, looked at closely
# ---------------------------------------------------------------------------

VERDICT_PROMPT = """You are a sharp, honest used pro-audio gear buyer's advisor.
Judge this listing for the buyer. Use the full ad text and the photo (if attached) to spot condition
problems, missing parts, signs it's a clone/copy or a consumer model posing as pro gear, "pickup only",
"firm" prices, or anything off. Compare the price with the comp and how fast that model sells.

Reply with JSON only:
{{"verdict": one of "Great deal", "Good deal", "Fair price", "Overpriced", "Be careful",
 "summary": "2-3 plain sentences: is it worth it and why",
 "red_flags": ["short items", ...] (empty list if none),
 "fair_offer": number (a realistic offer in USD) or null,
 "seller_message": "a short, friendly message to the seller proposing that offer (pickup or shipping as fits the ad), signed 'Thanks!'"}}

Listing: {title}
Price: {price}
Site: {site}{location}
Comp: {comp}
{speed}
Full ad text:
{ad}"""


def _full_ad_text(url: str, stored: str) -> str:
    """The listing's own page text (description, seller notes) when it can
    be read without a browser; otherwise what Gear Scout stored."""
    from bs4 import BeautifulSoup
    try:
        if "reverb.com/item/" in url:
            m = re.search(r"/item/(\d+)", url)
            r = requests.get(f"https://api.reverb.com/api/listings/{m.group(1)}",
                             headers={"Accept-Version": "3.0"}, timeout=15)
            if r.ok:
                d = r.json()
                return f"Condition: {(d.get('condition') or {}).get('display_name', '')}\n" + \
                       BeautifulSoup(d.get("description") or "", "html.parser").get_text(" ", strip=True)[:3500]
        elif "facebook.com" not in url and "ebay.com" not in url:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                           "AppleWebKit/537.36 Chrome/120 Safari/537.36"}, timeout=15)
            if r.ok:
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                body = soup.select_one("#postingbody") or soup.select_one("main") or soup.body
                if body:
                    return body.get_text(" ", strip=True)[:3500]
    except Exception as e:
        logger.debug("Full ad text failed for %s: %s", url, e)
    return stored or "(no description)"


def _photo(image_url: Optional[str]) -> Optional[tuple]:
    if not image_url:
        return None
    try:
        from scrapers.image_cache import cached_path
        p = cached_path(image_url)
        if p:
            return ("image/jpeg", p.read_bytes())
        r = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) < 3_000_000:
            return (r.headers["content-type"].split(";")[0], r.content)
    except Exception:
        pass
    return None


def verdict(global_id: str, cfg: dict) -> dict:
    import sqlite3
    from scrapers.comps import evaluate
    from scrapers.enrich import model_key
    from scrapers.learning import sale_stats
    from scrapers.store import _conn
    key = ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()
    if not key:
        raise PermissionError("Add your free Gemini API key in Settings → AI first.")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM seen WHERE global_id = ?", (global_id,)).fetchone()
    if not row:
        raise RuntimeError("That listing isn't in Gear Scout any more.")
    r = dict(row)
    c = evaluate(r["title"], r["price"], r.get("description"), r["url"])
    comp = (f"{c['label']} ~${c['ref']:,.0f} — this listing is {abs(c['pct'])}% "
            f"{'under' if c['pct'] >= 0 else 'over'}") if c else "none found"
    sale = sale_stats().get(model_key(r["title"]) or "")
    speed = f"Similar listings usually sell in ~{sale['days']:.0f} days at ~${sale['price']:,.0f}." if sale else ""
    loc = (r.get("location") or "").split(" @")[0]
    ad = _full_ad_text(r["url"], r.get("description") or "")
    photo = _photo(r.get("image_url"))
    text = _generate(key, VERDICT_PROMPT.format(
        title=r["title"], price=r["price"] or "not listed", site=(r["source_name"] or "").split(" — ")[0],
        location=f" ({loc})" if loc else "", comp=comp, speed=speed, ad=ad),
        json_mode=True, images=[photo] if photo else None)
    try:
        out = json.loads(text)
    except ValueError:
        out = {"verdict": "Fair price", "summary": text[:600], "red_flags": [], "fair_offer": None, "seller_message": ""}
    out["looked_at_photo"] = bool(photo)
    out["comp"] = comp
    return out
