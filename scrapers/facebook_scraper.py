"""
Facebook scraper using Playwright.

Two modes:
  1. scrape_facebook_group(source, keywords) — scrape a group for new listings
  2. search_facebook_groups(term)           — find groups by keyword and return
                                              a list for the user to pick from
"""
import json
from contextlib import contextmanager
import threading
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

from .base import Listing, ScrapeResult, keyword_match, truncate

logger = logging.getLogger(__name__)

# Session cookie file — persisted in the Docker volume so login survives restarts
SESSION_FILE = Path("/data/fb_session.json")
FB_LOGIN_URL = "https://www.facebook.com/login"


def _get_browser(playwright, headless: bool = True):
    """Launch Chromium with anti-detection options."""
    browser = playwright.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )
    return browser


def _new_context(browser, playwright_instance=None):
    """Create a browser context, loading saved session if available."""
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    if SESSION_FILE.exists():
        try:
            cookies = json.loads(SESSION_FILE.read_text())
            context.add_cookies(cookies)
            logger.info("Loaded Facebook session from %s", SESSION_FILE)
        except Exception as e:
            logger.warning("Could not load FB session: %s", e)
    return context


_session_lock = threading.Lock()


def _save_session(context):
    """Save browser cookies so the session persists across runs. Several
    Facebook scrapes can run at once, so writes are serialized and atomic
    (temp file + rename) — a reader never sees a half-written file."""
    cookies = context.cookies()
    with _session_lock:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cookies, indent=2))
        tmp.replace(SESSION_FILE)
    logger.info("Facebook session saved to %s", SESSION_FILE)


# One Chromium per worker thread, reused for every Facebook scrape that
# thread runs (see scrapers/runner.py) — launching a fresh browser for each
# of 12+ Facebook sources was a big share of a run's time.
_tls = threading.local()


@contextmanager
def fb_browser_session():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = _get_browser(pw, headless=True)
        _tls.browser = browser
        try:
            yield
        finally:
            _tls.browser = None
            browser.close()


@contextmanager
def _browser():
    shared = getattr(_tls, "browser", None)
    if shared is not None:
        yield shared
        return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = _get_browser(pw, headless=True)
        try:
            yield browser
        finally:
            browser.close()


def _wait_for(page, selector: str, timeout_ms: int = 8000):
    """Continue as soon as `selector` appears (plus a short settle), instead
    of always sleeping a fixed 3s. Missing selector (login wall, empty
    results) just means we move on after the timeout."""
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
        page.wait_for_timeout(400)
    except Exception:
        pass


def _scroll_for_more(page, selector: str, times: int, step_px: int, max_wait_ms: int):
    """Scroll `times` times, each time waiting only until more `selector`
    elements have loaded (up to max_wait_ms), not a fixed pause."""
    for _ in range(times):
        before = len(page.query_selector_all(selector))
        page.evaluate(f"window.scrollBy(0, {step_px})")
        try:
            page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length > n",
                arg=[selector, before], timeout=max_wait_ms,
            )
        except Exception:
            pass


def _is_logged_in(page) -> bool:
    """Check if we're logged into Facebook."""
    return "facebook.com" in page.url and page.query_selector('[aria-label="Your profile"]') is not None


# ---------------------------------------------------------------------------
# Interactive login (run once)
# ---------------------------------------------------------------------------

def _start_vnc_display(display: str = ":99", width: int = 1280, height: int = 800,
                        vnc_port: int = 5900):
    """
    Start a virtual X display + window manager + VNC server so a REAL
    (non-headless) browser window running inside the container can be seen
    and driven from outside it. All three binaries (Xvfb, fluxbox, x11vnc)
    are already installed in the image for exactly this purpose.
    """
    import subprocess

    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{width}x{height}x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    env = os.environ.copy()
    env["DISPLAY"] = display
    os.environ["DISPLAY"] = display  # so Playwright's own browser.launch() picks it up too

    fluxbox = subprocess.Popen(
        ["fluxbox"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    x11vnc = subprocess.Popen(
        ["x11vnc", "-display", display, "-forever", "-nopw", "-shared",
         "-listen", "0.0.0.0", "-rfbport", str(vnc_port), "-quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1)

    return xvfb, fluxbox, x11vnc


def interactive_login(fb_email: str = "", fb_password: str = ""):
    """
    Open a REAL, visible browser window (via a VNC-exposed virtual display)
    and let you log in to Facebook yourself — email, password, 2FA, and any
    CAPTCHA challenge included.

    A fully scripted headless login reliably gets flagged by Facebook's bot
    detection (confirmed: it serves a Google reCAPTCHA instead of proceeding,
    which this tool will not attempt to solve automatically — that's exactly
    the kind of automated-detection-bypass it won't do). Handing the whole
    flow to a real human in a real browser session sidesteps that cleanly,
    since it isn't automation at all.

    Run with: docker compose run --rm -p 5901:5900 scout fb-login
    (the container always listens on 5900 internally — 5901 is just the host
    side of that mapping; if you mapped a different host port, use that
    instead, since this process has no way to know which one you chose)
    """
    from playwright.sync_api import sync_playwright

    print("\n" + "=" * 60)
    print("FACEBOOK LOGIN — Interactive (VNC) Mode")
    print("=" * 60)

    print("\n  Starting a virtual display + VNC server...")
    xvfb, fluxbox, x11vnc = _start_vnc_display()

    try:
        print("\n  Connect a VNC viewer to the HOST port you mapped with -p, e.g.:")
        print("    vnc://localhost:5901   (if you ran -p 5901:5900, the documented default)")
        print("  On a Mac: Finder → Go → Connect to Server → vnc://localhost:5901")
        print("  NOTE: this is NOT necessarily port 5900 — that's only the port")
        print("  *inside* the container. Use whatever host port your -p flag used.")
        if fb_email:
            print(f"\n  Log in as: {fb_email}")
        print("\n  A browser window will open there. Log in to Facebook completely")
        print("  yourself — including any 2FA code or \"I'm not a robot\" check.")
        print("  Once you're looking at your actual News Feed / home page, come back")
        print("  here and press ENTER.\n")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--start-maximized"],
            )
            context = browser.new_context(
                viewport=None,  # let the real window size drive this
                locale="en-US",
            )
            page = context.new_page()
            page.goto(FB_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            input("  Press ENTER once you're fully logged in (or Ctrl+C to abort)... ")

            cookie_names = {c["name"] for c in context.cookies()}
            if not {"c_user", "xs"} <= cookie_names:
                print("\n❌ Doesn't look like you're logged in yet — no session cookies found.")
                print(f"   Current page: {page.url}")
                print("   Finish logging in in the VNC window, then press ENTER again.")
                input("  Press ENTER once you're fully logged in (or Ctrl+C to abort)... ")
                cookie_names = {c["name"] for c in context.cookies()}

            if not {"c_user", "xs"} <= cookie_names:
                browser.close()
                print("\n❌ Still not logged in. Aborting without saving a session.")
                print("   Re-run fb-login and try again once you're through any pending checkpoint.\n")
                return

            _save_session(context)
            browser.close()

        print("\n  ✅ Login successful. Session saved.")
        print("  Set 'enabled: true' on your Facebook sources in config.yaml")
        print("  Sessions typically last 30–90 days. Re-run fb-login if scraping stops working.\n")

    finally:
        x11vnc.terminate()
        fluxbox.terminate()
        xvfb.terminate()


# ---------------------------------------------------------------------------
# Group scraper
# ---------------------------------------------------------------------------

_GROUP_FEED_JS = r"""() => [...document.querySelectorAll('div[role=feed] > div')].map(c => {
  const listingA = c.querySelector('a[href*="/commerce/listing/"], a[href*="/marketplace/item/"]');
  const listing = listingA ? ((listingA.getAttribute('href') || '').match(/(?:listing|item)\/(\d+)/) || [])[1] || null : null;
  const postA = [...c.querySelectorAll('a[href]')].map(a => a.getAttribute('href') || '')
      .find(h => /\/groups\/[^/]+\/(posts|permalink)\/\d+/.test(h));
  const img = c.querySelector('img[src*="fbcdn"]');
  return {
    listing,
    permalink: postA ? postA.split(/[?#]/)[0] : null,
    lines: (c.innerText || '').split('\n').map(l => l.trim()).filter(l => l && l !== 'Facebook'),
    img: img ? img.getAttribute('src') : null,
  };
}).filter(p => p.listing || p.permalink)"""

_PRICE_LINE = re.compile(r"(\$\d+(?:,\d{3})*(?:\.\d{2})?|free)", re.I)
_POST_UI_LINES = {"message", "like", "comment", "share", "send", "see more", "·", "follow", "reply",
                  "learn more", "admin", "author", "moderator", "top contributor", "rising contributor",
                  "all reactions:", "most relevant", "write a comment…", "write a comment...", "see translation"}
# Facebook's own on-screen text that can end up inside a post's text.
_POST_UI_PATTERNS = re.compile(
    r"^(sorry, we're having trouble playing this video|this video may have been removed"
    r"|\d+[smhdwy]$|\d+ (?:comments?|shares?|reactions?)$|view \d+ more comments?|\+\d+$)",
    re.I,
)
# Sellers often keep a sold post up and just mark it — skip those.
_SOLD_TITLE = re.compile(r"^\W*(?:sold|pending)\b", re.I)


def _is_ui_line(line: str) -> bool:
    return line.lower() in _POST_UI_LINES or bool(_POST_UI_PATTERNS.match(line))


def _parse_group_post(post: dict) -> Optional[dict]:
    """A group feed post -> {title, price, description, location, url, id}.

    For-sale posts (with a Marketplace listing) read, after the author:
    description lines, the price on its own line, "· CITY, ST", then the
    item title (confirmed live). Plain discussion posts use their first
    line as the title and link to the post itself."""
    lines = [l for l in post["lines"] if not l.lower().startswith("comment as")]
    # Skip the author name and the "·" separator after it.
    body_start = next((i + 1 for i, l in enumerate(lines[:4]) if l == "·"), 1)
    body = lines[body_start:]

    if post["listing"]:
        pi = next((i for i, l in enumerate(body) if _PRICE_LINE.fullmatch(l)), None)
        if pi is None:
            return None
        price = None if body[pi].lower() == "free" else body[pi]
        location, ti = None, pi + 1
        if ti < len(body) and body[ti].startswith("·"):
            raw = body[ti].lstrip("· ").strip()
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) == 2 and re.fullmatch(r"[A-Za-z]{2}", parts[1]):
                location = f"{parts[0].title()}, {parts[1].upper()}"
            ti += 1
        title = body[ti] if ti < len(body) and not _is_ui_line(body[ti]) else (body[0] if body else "")
        if not title or _SOLD_TITLE.match(title):
            return None
        return {
            "title": title, "price": price, "location": location,
            "description": " ".join(body[:pi]),
            "url": f"https://www.facebook.com/marketplace/item/{post['listing']}/",
            "id": post["listing"],
        }

    content = [l for l in body if not _is_ui_line(l)]
    if not content or _SOLD_TITLE.match(content[0]):
        return None
    text = " ".join(content)
    price_match = re.search(r"\$\d+(?:,\d{3})*(?:\.\d{2})?", text)
    permalink = post["permalink"]
    if not permalink.startswith("http"):
        permalink = "https://www.facebook.com" + permalink
    post_id = re.search(r"/(?:posts|permalink)/(\d+)", permalink).group(1)
    return {
        "title": content[0], "price": price_match.group(0) if price_match else None,
        "location": None, "description": " ".join(content[1:]),
        "url": permalink, "id": post_id,
    }


def scrape_facebook_group(source: dict, keywords: list[str]) -> ScrapeResult:
    name = source["name"]
    url = source["url"]
    start = time.time()

    # A single search term (typical of the dashboard's live search) can be
    # sent to this group's own built-in search instead of just scrolling
    # its main feed — Facebook's standard /groups/{id}/search/?query= URL
    # scheme, same one the group's own search box uses. Falls back to the
    # plain group URL (unchanged behavior) if that doesn't parse.
    if len(keywords) == 1 and keywords[0].strip():
        group_match = re.search(r'/groups/([^/?]+)', url)
        if group_match:
            url = f"https://www.facebook.com/groups/{group_match.group(1)}/search/?query={quote_plus(keywords[0].strip())}"

    if not SESSION_FILE.exists():
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="No Facebook session found.",
            fix_hint=(
                "You need to log in first. Run:\n"
                "  docker compose run --rm scout fb-login\n"
                "A browser window will open — log in, then press ENTER. "
                "Your session will be saved and reused automatically."
            ),
            duration_seconds=time.time() - start,
        )

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Playwright not installed.",
            fix_hint="Run: pip install playwright && playwright install chromium",
            duration_seconds=time.time() - start,
        )

    try:
        with _browser() as browser:
            context = _new_context(browser)
            page = context.new_page()

            # Navigate to group. Large groups (heavy DOM, lots of embedded
            # post previews/images) can take a lot longer to reach
            # domcontentloaded than a small one — retry once with a much
            # longer timeout before giving up on this run entirely, instead
            # of a single fixed 30s budget for every group regardless of size.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError:
                logger.warning(
                    "Facebook group page load timed out at 45s, retrying "
                    "with a 90s timeout: %s", url,
                )
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            _wait_for(page, 'div[role="feed"]')

            if "this content isn't available" in (page.inner_text("body")[:3000] or "").lower():
                context.close()
                return ScrapeResult(
                    source_name=name, source_url=url, success=False,
                    error="Facebook says this group isn't available.",
                    fix_hint=(
                        "The group was deleted, renamed, or the link is wrong. Open the "
                        "link in your browser to check, then update or remove this source "
                        "on the Sources page."
                    ),
                    duration_seconds=time.time() - start,
                )

            # Check if we got redirected to login — session expired
            if "login" in page.url.lower() or page.query_selector('[data-testid="royal_login_button"]'):
                context.close()
                return ScrapeResult(
                    source_name=name, source_url=url, success=False,
                    error="Facebook session expired — redirected to login page.",
                    fix_hint=(
                        "Your saved session has expired (usually after 30–90 days). "
                        "Re-run:\n  docker compose run --rm scout fb-login\n"
                        "to refresh it."
                    ),
                    duration_seconds=time.time() - start,
                )

            # Check if group requires membership
            join_btn = page.query_selector('[aria-label="Join group"], [data-testid="group-join-button"]')
            if join_btn:
                context.close()
                return ScrapeResult(
                    source_name=name, source_url=url, success=False,
                    error="Not a member of this Facebook group.",
                    fix_hint=(
                        f"You need to join the group at {url} with your Facebook account "
                        "before Gear Scout can scrape it. Join the group, then re-run."
                    ),
                    duration_seconds=time.time() - start,
                )

            # Posts live in the group's feed (div[role=feed] > div). Earlier
            # versions read div[role=article], which on today's layout are
            # mostly Messenger chat bubbles from an open chat window — never
            # posts, so nothing was ever found. The feed is virtualized
            # (posts unload as you scroll), so collect after every scroll.
            collected: dict[str, dict] = {}

            def collect():
                for post in page.evaluate(_GROUP_FEED_JS):
                    key = post["listing"] or post["permalink"]
                    # A post can be captured as an empty placeholder before
                    # Facebook finishes rendering it — keep the fullest copy.
                    if key and len(post["lines"]) > len(collected.get(key, {}).get("lines", [])):
                        collected[key] = post

            post_link = ("div[role=feed] a[href*='/commerce/listing/'], "
                         "div[role=feed] a[href*='/marketplace/item/'], "
                         "div[role=feed] a[href*='/posts/'], div[role=feed] a[href*='/permalink/']")
            try:
                page.wait_for_selector(post_link, timeout=10000)
                page.wait_for_timeout(500)
            except Exception:
                pass

            for step in range(10):
                collect()
                before = len(collected)
                # Reaching the bottom is what makes Facebook load the next
                # batch; a fixed scrollBy often stopped short of it.
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    page.wait_for_timeout(250)
                    collect()
                    if len(collected) > before:
                        break
                logger.debug("FB group %s scroll %d: %d posts collected", name, step, len(collected))
            collect()

            listings = []
            for post in collected.values():
                try:
                    parsed = _parse_group_post(post)
                    if not parsed:
                        continue
                    if not keyword_match(f"{parsed['title']} {parsed['description']}", keywords):
                        continue
                    listings.append(Listing(
                        source_name=name,
                        title=truncate(parsed["title"], 120),
                        url=parsed["url"],
                        price=parsed["price"],
                        description=truncate(parsed["description"], 250),
                        image_url=post["img"],
                        listing_id=parsed["id"],
                        location=parsed["location"],
                    ))
                except Exception as e:
                    logger.debug("Error parsing FB group post: %s", e)

            logger.info("FB group %s: %d posts collected, %d matched", name, len(collected), len(listings))

            # Refresh session cookies
            _save_session(context)
            context.close()

            return ScrapeResult(
                source_name=name, source_url=url, success=True,
                listings=listings, duration_seconds=time.time() - start,
            )

    except Exception as e:
        err = str(e)
        hint = "Unexpected Playwright error. Check logs."
        if "net::ERR_NAME_NOT_RESOLVED" in err:
            hint = "Could not resolve facebook.com — check your internet connection."
        elif "Timeout" in err:
            hint = (
                "Facebook page took too long to load, even after retrying with "
                "a longer timeout (45s, then 90s). This can happen for a very "
                "large or very active group, or when Facebook itself is slow. "
                "Will retry next run."
            )
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=err, fix_hint=hint,
            duration_seconds=time.time() - start,
        )


FEED_SCROLLS = 8
BROAD_SCROLLS = 5
REGION_BUDGET_SECONDS = 240
# Newest-first Marketplace searches per region on scheduled runs (overridable
# with config `facebook_marketplace: broad_terms:`).
DEFAULT_BROAD_TERMS = ["microphone", "preamp", "audio compressor", "audio mixer", "studio monitor", "tube mic"]

_CARD_JS = """els => els.map(e => ({
    href: e.getAttribute('href') || '',
    aria: e.getAttribute('aria-label') || '',
    text: e.innerText || '',
    img: (e.querySelector('img') || {getAttribute: () => null}).getAttribute('src'),
}))"""


def _collect_marketplace_cards(page, collected: dict, scrolls: int) -> None:
    """Scrolls with the mouse wheel (a scripted window scroll doesn't make
    Facebook load more) and gathers cards as they appear; stops early when
    two scrolls in a row bring nothing new."""
    stale = 0
    for i in range(scrolls + 1):
        before = len(collected)
        for c in page.eval_on_selector_all("a[href*='/marketplace/item/']", _CARD_JS):
            key = c["href"].split("?")[0]
            if key not in collected or len(c["aria"]) > len(collected[key]["aria"]):
                collected[key] = c
        stale = stale + 1 if len(collected) == before else 0
        if i == scrolls or stale >= 2:
            break
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1600)


def scrape_facebook_marketplace_region(source: dict, keywords: list[str]) -> ScrapeResult:
    """Scrapes one Facebook Marketplace region's general browse feed (see
    scrapers/facebook_marketplace_regions.py for the 8 regions), filtering
    matches locally against the full keyword list — same as every other
    source in this app — rather than sending a search query to Facebook's
    own search box, which only supports one phrase requiring every word to
    match (that's also why this hits the plain browse feed, not a keyword
    search URL: 8 broad fetches filtered locally instead of one automated
    search per keyword per region, which would be hundreds of automated
    requests against your actual Facebook account).

    First-pass implementation: Facebook's Marketplace markup is
    React-rendered with obfuscated class names, and unlike the Craigslist/
    eBay/etc. scrapers, there was no logged-in session available to verify
    the exact DOM structure against while writing this. If it stops
    finding cards, inspect a listing card in your own logged-in browser and
    update the selectors below.
    """
    name = source["name"]
    url = source["url"]
    start = time.time()

    # A single search term (typical of the dashboard's live search) can be
    # sent to Facebook's own Marketplace search box — unlike the full
    # keyword list, where an automated search per keyword per region would
    # mean hundreds of requests against the actual Facebook account, one
    # term is exactly what a search box is for.
    is_live = len(keywords) == 1 and bool(keywords[0].strip())
    if is_live:
        from scrapers.facebook_marketplace_regions import facebook_marketplace_search_url

        loc_match = re.search(r'/marketplace/(\d+)', url)
        if loc_match:
            url = facebook_marketplace_search_url(loc_match.group(1), keywords[0].strip())

    if not SESSION_FILE.exists():
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="No Facebook session found.",
            fix_hint='You need to log in first. Go to the dashboard\'s Sources page and click "Log in to Facebook".',
            duration_seconds=time.time() - start,
        )

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
    except ImportError:
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error="Playwright not installed.",
            fix_hint="Run: pip install playwright && playwright install chromium",
            duration_seconds=time.time() - start,
        )

    try:
        with _browser() as browser:
            context = _new_context(browser)
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeoutError:
                logger.warning(
                    "Facebook Marketplace region page load timed out at 45s, "
                    "retrying with a 90s timeout: %s", url,
                )
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            _wait_for(page, "a[href*='/marketplace/item/']")

            if "login" in page.url.lower() or page.query_selector('[data-testid="royal_login_button"]'):
                context.close()
                return ScrapeResult(
                    source_name=name, source_url=url, success=False,
                    error="Facebook session expired — redirected to login page.",
                    fix_hint='Your saved session has expired. Go to the dashboard\'s Sources page and click "Log in to Facebook" again.',
                    duration_seconds=time.time() - start,
                )

            # Marketplace item cards are always wrapped in a
            # /marketplace/item/{id}/ anchor — this URL scheme is far more
            # stable than any class name on a React-rendered page. The list
            # is virtualized (only ~40 cards exist on the page at a time), so
            # cards are collected after every scroll, not once at the end.
            collected: dict[str, dict] = {}
            _collect_marketplace_cards(page, collected, scrolls=(8 if is_live else FEED_SCROLLS))

            # Scheduled runs also search a few broad words newest-first in
            # each region — the browse feed alone is a small, personalized
            # sample. Skipped on the lighter hourly refresh runs.
            loc_id = re.search(r'/marketplace/(\d+)', source["url"])
            if not is_live and not source.get("_light") and loc_id:
                for term in source.get("_broad_terms") or DEFAULT_BROAD_TERMS:
                    # A time budget per region, so one slow page can't stall the run.
                    if time.time() - start > REGION_BUDGET_SECONDS:
                        logger.info("%s: time budget reached, skipping remaining broad searches", name)
                        break
                    time.sleep(1.5)  # pace consecutive Facebook page loads
                    try:
                        page.goto(
                            f"https://www.facebook.com/marketplace/{loc_id.group(1)}/search?query={quote_plus(term)}"
                            "&sortBy=creation_time_descend&exact=false",
                            wait_until="domcontentloaded", timeout=45000)
                        _wait_for(page, "a[href*='/marketplace/item/']")
                        _collect_marketplace_cards(page, collected, scrolls=BROAD_SCROLLS)
                    except Exception as e:
                        logger.warning("FB Marketplace broad search %r failed for %s: %s", term, name, e)
            cards = list(collected.values())
            listings = []
            seen_ids = set()

            for card in cards:
                try:
                    href = card["href"]
                    id_match = re.search(r'/marketplace/item/(\d+)', href)
                    if not id_match:
                        continue
                    item_id = id_match.group(1)
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)

                    # The card's own aria-label is a clean, consistently
                    # formatted string Facebook generates for accessibility:
                    # "{title}, {price|Free}, {city}, {state}, listing {id}"
                    # (confirmed live) — far more reliable than inner_text(),
                    # which sometimes renders with NO line breaks at all
                    # between title/price/location depending on which card
                    # layout variant Facebook serves (also confirmed live —
                    # this was the actual cause of a real bug: a free item's
                    # "FREE" badge got picked up as the title instead of the
                    # real product name, since there was no "$" line to
                    # anchor off of and the two were run together with no
                    # separator at all in inner_text()).
                    aria_label = card["aria"]
                    title = None
                    price = None
                    location = None
                    if aria_label:
                        label = re.sub(r',\s*listing\s+\d+\s*$', '', aria_label, flags=re.IGNORECASE)
                        # Search the raw label directly for the price token
                        # rather than splitting on commas — a price itself
                        # can contain a comma as a thousands separator (e.g.
                        # "$1,200"), which a naive comma-split breaks into
                        # "$1" and "200", silently truncating any price at
                        # or above $1,000 down to its leading digit.
                        price_match = re.search(r'\$\d+(?:,\d{3})*(?:\.\d{2})?|(?<![A-Za-z])[Ff]ree(?![A-Za-z])', label)
                        if price_match:
                            title = label[:price_match.start()].strip().rstrip(',').strip()
                            matched = price_match.group(0)
                            price = None if matched.lower() == "free" else matched
                            # After the price: ["reduced from $X",] City, ST
                            rest = [p.strip() for p in label[price_match.end():].split(",")
                                    if p.strip() and not p.strip().lower().startswith("reduced from")]
                            if len(rest) >= 2 and re.fullmatch(r"[A-Z]{2}", rest[-1]):
                                location = f"{rest[-2]}, {rest[-1]}"

                    text_content = card["text"]
                    match_text = aria_label or text_content
                    if not match_text or not keyword_match(match_text, keywords):
                        continue

                    if not title:
                        # aria-label missing or didn't parse as expected —
                        # fall back to the line-based heuristic.
                        if price is None:
                            price_match = re.search(r'\$\d+(?:,\d{3})*(?:\.\d{2})?', text_content)
                            price = price_match.group(0) if price_match else None
                        lines = [l.strip() for l in text_content.split('\n') if l.strip()]
                        price_idx = next((i for i, l in enumerate(lines) if re.match(r'^\$[\d,]', l)), None)
                        if price_idx is not None and price_idx + 1 < len(lines):
                            title = lines[price_idx + 1]
                        else:
                            skip_markers = (
                                "just listed", "getting popular", "may sell soon",
                                "interested in", "similar to",
                            )
                            candidates = [l for l in lines if not any(m in l.lower() for m in skip_markers)]
                            title = candidates[0] if candidates else (lines[0] if lines else "FB Marketplace Listing")
                    title = title[:120]

                    image_url = card["img"]

                    listing_url = href.split("?")[0]
                    if not listing_url.startswith("http"):
                        listing_url = "https://www.facebook.com" + listing_url

                    listings.append(Listing(
                        source_name=name,
                        title=title,
                        url=listing_url,
                        price=price,
                        image_url=image_url,
                        listing_id=item_id,
                        location=location,
                    ))
                except Exception as e:
                    logger.debug("Error parsing FB Marketplace card: %s", e)
                    continue

            _save_session(context)
            context.close()

            if not cards:
                return ScrapeResult(
                    source_name=name, source_url=url, success=False,
                    error="Could not find any Marketplace listing cards on the page.",
                    fix_hint=(
                        "Facebook may have changed their Marketplace layout, or this "
                        "region's feed loaded empty. Open the URL in your own logged-in "
                        "browser, inspect a listing card, and update the selectors in "
                        "scrapers/facebook_scraper.py → scrape_facebook_marketplace_region()."
                    ),
                    duration_seconds=time.time() - start,
                )

            return ScrapeResult(
                source_name=name, source_url=url, success=True,
                listings=listings, duration_seconds=time.time() - start,
            )

    except Exception as e:
        err = str(e)
        hint = "Unexpected Playwright error. Check logs."
        if "net::ERR_NAME_NOT_RESOLVED" in err:
            hint = "Could not resolve facebook.com — check your internet connection."
        elif "Timeout" in err:
            hint = (
                "Facebook page took too long to load, even after retrying with "
                "a longer timeout (45s, then 90s)."
            )
        return ScrapeResult(
            source_name=name, source_url=url, success=False,
            error=err, fix_hint=hint,
            duration_seconds=time.time() - start,
        )


# ---------------------------------------------------------------------------
# Facebook Group Search
# ---------------------------------------------------------------------------

def search_facebook_groups(search_term: str) -> list[dict]:
    """
    Search Facebook for groups matching search_term.
    Returns a list of dicts: {name, url, members, description}

    Run with: docker compose run --rm scout fb-search "vintage studio gear"
    """
    if not SESSION_FILE.exists():
        print("\n❌ No Facebook session found.")
        print("   Run: docker compose run --rm scout fb-login\n")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed.")
        return []

    results = []

    print(f"\n🔍 Searching Facebook Groups for: '{search_term}'\n")

    with sync_playwright() as pw:
        browser = _get_browser(pw, headless=True)
        context = _new_context(browser)
        page = context.new_page()

        # Facebook groups search URL
        encoded = search_term.replace(" ", "%20")
        search_url = f"https://www.facebook.com/search/groups/?q={encoded}"
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        if "login" in page.url.lower():
            print("❌ Session expired. Run: docker compose run --rm scout fb-login")
            browser.close()
            return []

        # Scroll to load more results
        for _ in range(4):
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(1200)

        # Group cards in search results
        group_cards = page.query_selector_all('[data-testid="browsing-results-list"] > div, div[role="listitem"]')
        if not group_cards:
            # Try broader selector
            group_cards = page.query_selector_all('div[class*="x1lliihq"] a[href*="/groups/"]')

        seen_urls = set()

        for card in group_cards[:30]:
            try:
                # Find group link
                link_el = card.query_selector('a[href*="/groups/"]')
                if not link_el:
                    continue
                href = link_el.get_attribute("href") or ""
                # Clean URL
                group_url = re.sub(r'\?.*', '', href)
                if not group_url or group_url in seen_urls:
                    continue
                if "/groups/feed" in group_url or "/groups/discover" in group_url:
                    continue
                seen_urls.add(group_url)

                # Make absolute
                if not group_url.startswith("http"):
                    group_url = "https://www.facebook.com" + group_url

                # Name
                name_el = card.query_selector("span[class*='x193iq5w'], h2, strong")
                group_name = name_el.inner_text().strip() if name_el else group_url

                # Member count and description (best-effort)
                full_text = card.inner_text()
                member_match = re.search(r'([\d,.]+[KkMm]?\s*(?:members|member))', full_text)
                members = member_match.group(1) if member_match else "unknown"

                # Description: everything after the member line
                lines = [l.strip() for l in full_text.split('\n') if l.strip()]
                description = ""
                for line in lines:
                    if group_name not in line and "member" not in line.lower() and len(line) > 15:
                        description = line[:120]
                        break

                results.append({
                    "name": group_name,
                    "url": group_url,
                    "members": members,
                    "description": description,
                })

            except Exception as e:
                logger.debug("Error parsing group card: %s", e)
                continue

        _save_session(context)
        browser.close()

    return results


def print_group_search_results(results: list[dict], config_path: str = "config.yaml"):
    """
    Pretty-print search results and offer to add selected groups to config.yaml.
    """
    if not results:
        print("No groups found. Try a different search term.")
        return

    print(f"Found {len(results)} groups:\n")
    print(f"{'#':<4} {'Group Name':<45} {'Members':<15} {'URL'}")
    print("-" * 100)
    for i, g in enumerate(results, 1):
        print(f"{i:<4} {g['name'][:44]:<45} {g['members']:<15} {g['url']}")
        if g["description"]:
            print(f"     {g['description'][:90]}")
        print()

    print("\nEnter the numbers of groups to add to config.yaml (e.g. 1 3 5), or ENTER to skip:")
    raw = input("> ").strip()
    if not raw:
        print("No groups added.")
        return

    try:
        indices = [int(x) - 1 for x in raw.split()]
        chosen = [results[i] for i in indices if 0 <= i < len(results)]
    except (ValueError, IndexError):
        print("Invalid input. No groups added.")
        return

    if not chosen:
        print("No groups added.")
        return

    # Append to config.yaml
    with open(config_path, "a") as f:
        f.write("\n  # Added via fb-search\n")
        for g in chosen:
            safe_name = g['name'].replace('"', "'")
            f.write(f"\n  - name: \"FB — {safe_name}\"\n")
            f.write(f"    url: \"{g['url']}\"\n")
            f.write(f"    type: facebook\n")
            f.write(f"    enabled: true\n")

    print(f"\n✅ Added {len(chosen)} group(s) to config.yaml")
    print("   They're enabled and will be scraped on the next run.")
    print("   Make sure you've run 'docker compose run --rm scout fb-login' first.\n")
