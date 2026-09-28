"""
Facebook site-wide post search ("search bar" → Posts, Recent first) for a
prioritized list of terms grouped by category, plus one-off manual searches.

Facebook deliberately hides two things on post-search results (checked
live): post dates are rendered scrambled, and post permalinks aren't in the
page. So:
  - "Recent" = Facebook's own newest-first sort; the FB Posts page shows what
    Gear Scout first found in the last 14 days.
  - A post's photo link (which opens the post) is used as its link; a
    text-only post links to the search it came from.
Uses the same saved Facebook session as the Marketplace/group sources.
"""
import base64
import hashlib
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

MAX_TERMS_PER_RUN = 40
_RECENT_FILTER = base64.b64encode(json.dumps(
    {"recent_posts:0": json.dumps({"name": "recent_posts", "args": ""})}
).encode()).decode()

_POSTS_JS = r"""() => [...document.querySelectorAll('div[role=feed] > div')].map(k => {
  const text = (k.innerText || '').trim();
  if (text.length < 20) return null;
  const photo = [...k.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '')
      .find(h => /facebook\.com\/(photo|photos|photo\.php)/.test(h) || /^\/(photo|photos)/.test(h));
  const group = [...k.querySelectorAll('a[href*="/groups/"]')].map(a => (a.getAttribute('href') || '').split('?')[0])
      .find(h => /\/groups\/[^/]+\/?$/.test(h));
  // Post photos, not profile pictures (those are small squares).
  const img = [...k.querySelectorAll('img[src*="fbcdn"]')].find(i => (i.naturalWidth || i.width) >= 120);
  return {text, photo: photo || null, group: group || null, img: img ? img.getAttribute('src') : null};
}).filter(Boolean)"""

_UI_LINE = re.compile(
    r"^(like|comment|share|send|reply|follow|see more|see translation|all reactions:?|"
    r"write a comment.*|most relevant|top contributor|admin|author|·|\d+[smhdwy]|\d+\s*(comments?|shares?|reactions?)|"
    r"\+\d+|facebook)$",
    re.I,
)


# Only posts offering something for sale are kept. Facebook doesn't mark
# them, so this reads the wording: clear sale language on its own, or a
# price together with a sale detail (a bare price can be "I paid $2k for
# mine"). Wanted/ISO posts and ones already marked sold are dropped.
_STRONG_SALE = re.compile(
    r"\b(?:for sale|selling|sell(?:ing)? (?:my|this|a|an|off)|fs|f/s|wts|fsot|for trade or sale|"
    r"asking(?: price)?|obo|or best offer|price (?:drop|reduced|is firm)|reduced to|make (?:me )?an offer|"
    r"up for grabs|available for (?:sale|purchase)|need(?:s)? (?:it )?gone)\b",
    re.I,
)
_SALE_DETAIL = re.compile(
    r"\b(?:shipped|shipping|ships|free ship|local pick ?up|pick ?up|firm|trades?|paypal|venmo|zelle|"
    r"cash only|dm (?:me|for)|pm (?:me|for)|message me|serious inquiries|no lowballs?|"
    r"plus shipping|\+ ?shipping|or trade|will ship)\b",
    re.I,
)
_PRICE = re.compile(r"(?:\$|usd\s?|cad\s?)\s?\d[\d,]*(?:\.\d{2})?|\b\d[\d,]*\s?(?:usd|cad|dollars|bucks|obo)\b", re.I)
_WANTED = re.compile(r"\b(?:wtb|iso|in search of|looking for|want(?:ed)? to buy|anyone selling|does anyone have)\b", re.I)
_SOLD = re.compile(r"^\W*(?:sold|pending)\b|\b(?:sold|no longer available|sale pending)\W*$", re.I)


# The group a post was made in shows at the top of its text. A price posted
# in a buy/sell/used-gear group is a sale even without sale wording.
_SALE_GROUP = re.compile(
    r"\b(?:buy|sell|selling|swap|trade|for sale|used|classifieds?|market(?:place)?|exchange|"
    r"garage sale|yard sale|swap ?meet|bst|b/s/t)\b",
    re.I,
)


def is_for_sale(text: str) -> bool:
    if not text or _WANTED.search(text) or _SOLD.search(text.strip()):
        return False
    if _STRONG_SALE.search(text):
        return True
    if not _PRICE.search(text):
        return False
    return bool(_SALE_DETAIL.search(text) or _SALE_GROUP.search(text[:120]))


def parse_priority_list(text: str) -> dict[str, list[str]]:
    """'[Microphones]\\ngefell um70\\n...' -> {'Microphones': ['gefell um70', ...]}.
    Terms before any [Category] line go under 'General'."""
    out: dict[str, list[str]] = {}
    category = "General"
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            category = m.group(1).strip() or "General"
            continue
        out.setdefault(category, [])
        if line.lower() not in (t.lower() for t in out[category]):
            out[category].append(line)
    return {c: terms for c, terms in out.items() if terms}


def priority_terms(cfg: dict) -> list[tuple[str, str]]:
    """[(category, term), …] from config, in list order."""
    cats = ((cfg.get("fb_post_search") or {}).get("categories")) or {}
    return [(c, str(t)) for c, terms in cats.items() for t in (terms or []) if str(t).strip()]


def search_url(term: str) -> str:
    return f"https://www.facebook.com/search/posts/?q={quote(term)}&filters={_RECENT_FILTER}"


def _parse(post: dict, term: str) -> Optional[dict]:
    lines = [l.strip() for l in post["text"].split("\n")]
    # First line is the poster's name — never used.
    content = [l for l in lines[1:] if l and len(l) > 2 and not _UI_LINE.match(l)]
    if not content:
        return None
    text = " ".join(content)
    if not keyword_match(text, [term]) or not is_for_sale(text):
        return None
    # Title: the first line that mentions the term, else the first line —
    # minus Facebook's own "… See more" expander text.
    title = next((l for l in content if keyword_match(l, [term])), content[0])
    title = re.sub(r"\s*(?:…|\.\.\.)?\s*See more\s*$", "", title).strip() or title
    price = re.search(r"\$\s?\d+(?:,\d{3})*(?:\.\d{2})?", text)
    digest = hashlib.md5(text.encode()).hexdigest()[:16]
    link = post["photo"]
    if link and link.startswith("/"):
        link = "https://www.facebook.com" + link
    if not link:
        # Unique per post so different text-only posts aren't merged as duplicates.
        link = f"{search_url(term)}#post-{digest}"
    return {
        "title": truncate(title, 120), "description": truncate(text, 250),
        "price": price.group(0).replace(" ", "") if price else None,
        "url": link, "image": post["img"], "id": digest,
    }


def search_term(page, term: str, scrolls: int = 5) -> list[dict]:
    """Posts for one term from an open (logged-in) Playwright page."""
    try:
        page.goto(search_url(term), wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        logger.warning("FB post search load failed for %r: %s", term, e)
        return []
    try:
        page.wait_for_selector("div[role=feed] > div", timeout=10000)
        page.wait_for_timeout(800)
    except Exception:
        return []
    collected: dict[str, dict] = {}
    for _ in range(scrolls):
        for post in page.evaluate(_POSTS_JS):
            key = post["photo"] or post["text"][:200]
            if len(post["text"]) > len(collected.get(key, {}).get("text", "")):
                collected[key] = post
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
    parsed = [_parse(p, term) for p in collected.values()]
    return [p for p in parsed if p]


def scrape_facebook_posts(source: dict, keywords: list[str], cfg: Optional[dict] = None) -> ScrapeResult:
    """Scheduled run: every priority term (capped). Live search: the one term."""
    from scrapers.facebook_scraper import SESSION_FILE, _browser, _new_context, _save_session

    name = source["name"]
    start = time.time()
    live_term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    jobs = [("Search", live_term)] if live_term else priority_terms(cfg or {})[:MAX_TERMS_PER_RUN]
    manual_url = search_url(live_term or (jobs[0][1] if jobs else "pro audio"))
    if not jobs:
        return ScrapeResult(source_name=name, source_url=manual_url, success=True, listings=[],
                            duration_seconds=time.time() - start)
    if not SESSION_FILE.exists():
        return ScrapeResult(
            source_name=name, source_url=manual_url, success=False,
            error="No Facebook session found.",
            fix_hint='Log in on the Sources page ("Log in to Facebook").',
            duration_seconds=time.time() - start,
        )

    listings = []
    try:
        with _browser() as browser:
            context = _new_context(browser)
            page = context.new_page()
            for category, term in jobs:
                for p in search_term(page, term):
                    listings.append(Listing(
                        source_name=f"FB Posts — {category}" if not live_term else name,
                        title=p["title"], url=p["url"], price=p["price"],
                        description=p["description"], image_url=p["image"],
                        listing_id=p["id"],
                    ))
                time.sleep(1.5)  # pace consecutive Facebook searches
            _save_session(context)
            context.close()
    except Exception as e:
        return ScrapeResult(source_name=name, source_url=manual_url, success=False, error=str(e),
                            fix_hint="Facebook may be slow or the session expired — check the Sources page.",
                            duration_seconds=time.time() - start)

    return ScrapeResult(source_name=name, source_url=manual_url, success=True,
                        listings=listings, duration_seconds=time.time() - start)
