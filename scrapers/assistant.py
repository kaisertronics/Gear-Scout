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
_model_cache: dict = {"key": None, "model": None, "at": 0.0}


def _pick_model(key: str) -> str:
    """The newest "flash" model this key can use (fast, free-tier friendly)."""
    if _model_cache["key"] == key and _model_cache["model"] and time.time() - _model_cache["at"] < 86400:
        return _model_cache["model"]
    r = requests.get(f"{API}/models", params={"key": key, "pageSize": 200}, timeout=20)
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
    model = sorted(gem, key=rank)[-1]
    _model_cache.update(key=key, model=model, at=time.time())
    return model


def _generate(key: str, prompt: str, json_mode: bool = False) -> str:
    model = _pick_model(key)
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(f"{API}/{model}:generateContent", params={"key": key}, json=body, timeout=60)
    if r.status_code == 429:
        raise RuntimeError("Gemini's free-tier limit was reached for now — try again in a minute.")
    if r.status_code in (400, 401, 403) and "API key" in r.text:
        raise PermissionError("Google rejected the Gemini API key — check it in Settings → AI.")
    r.raise_for_status()
    parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(p.get("text", "") for p in parts).strip()


PLAN_PROMPT = """You help search a database of used pro-audio / studio gear listings
(microphones, preamps, compressors, EQs, consoles, interfaces, monitors, tape machines…)
collected from Facebook Marketplace, Craigslist, eBay, Reverb, OfferUp, ShopGoodwill, Kijiji and dealers.

Turn the user's question into a search plan. Reply with JSON only:
{{"terms": [up to 5 short search terms, each a brand/model or gear type as it would appear in a
listing title, e.g. "neve 1073", "u87", "la-2a", "ribbon mic"],
 "max_price": number or null, "min_price": number or null,
 "deals_only": true if they want bargains / good deals / under market, else false}}

Question: {q}"""

ANSWER_PROMPT = """You are Gear Scout's assistant for a used pro-audio gear buyer.
Answer the question using ONLY the listings below (they are real, currently for sale).
Be concise and practical: recommend the best options and say why (price vs. comp, condition clues,
location). Cite listings as [#n] using their numbers. If nothing fits, say so plainly and suggest
what to search instead. "comp" is what similar gear sells for; "% under" is how far below it the
listing is. Don't invent listings, prices or facts not shown.

Question: {q}

Listings:
{rows}"""


def ask(question: str, cfg: dict) -> dict:
    """{'answer': text with [#n] citations, 'listings': [...], 'plan': {...}}"""
    key = ((cfg.get("ai") or {}).get("gemini_api_key") or "").strip()
    if not key:
        raise PermissionError("Add your free Gemini API key in Settings → AI first.")
    question = (question or "").strip()[:500]
    try:
        plan = json.loads(_generate(key, PLAN_PROMPT.format(q=question), json_mode=True))
    except (ValueError, TypeError):
        plan = {"terms": [question], "max_price": None, "min_price": None, "deals_only": False}
    terms = [str(t).strip() for t in (plan.get("terms") or []) if str(t).strip()][:5] or [question]

    from scrapers.comps import evaluate, price_index, similar_index
    from scrapers.enrich import is_accessory_only, is_not_audio, item_form, parse_price
    from scrapers.market import load_market
    from scrapers.store import search_listings
    market, index, similar = load_market(), price_index(), similar_index()
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
            seen.add(url)
            c = evaluate(title, l.get("price"), l.get("description"), url, market, index, similar)
            if plan.get("deals_only") and not (c and c["worth"]):
                continue
            l["comp"] = c
            found.append(l)
    # Best first: biggest discount vs. comp, then cheapest.
    found.sort(key=lambda l: (-(l["comp"]["pct"] if l.get("comp") else -999), parse_price(l.get("price")) or 1e9))
    found = found[:30]
    if not found:
        return {"answer": "I couldn't find any matching listings Gear Scout has collected right now. "
                          f"(Searched for: {', '.join(terms)}.) Try a live search to check every site.",
                "listings": [], "plan": plan}
    rows = []
    for i, l in enumerate(found, 1):
        c = l.get("comp")
        comp = f"comp {c['label']} ~${c['ref']:,.0f} ({c['pct']}% under)" if c else "no comp"
        loc = (l.get("location") or "").split(" @")[0]
        rows.append(f"#{i} | {l.get('title')} | {l.get('price')} | {comp} | "
                    f"{(l.get('source_name') or '').split(' — ')[0]}{' | ' + loc if loc else ''} | "
                    f"{(l.get('description') or '')[:120]}")
    answer = _generate(key, ANSWER_PROMPT.format(q=question, rows="\n".join(rows)))
    return {"answer": answer, "listings": found, "plan": plan}
