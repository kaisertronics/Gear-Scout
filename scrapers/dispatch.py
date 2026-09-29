"""
Shared "which scraper does this source's URL/type need" routing, used by
both the scheduled run (main.py) and on-demand live search (dashboard.py) so
both send a source through the exact same scraper.
"""
import logging
from urllib.parse import quote_plus

from scrapers.base import ScrapeResult
from scrapers.html_scraper import (
    scrape_craigslist,
    scrape_craigslist_region,
    scrape_guitar_center,
    scrape_sweetwater,
)
from scrapers.rss_scraper import scrape_rss

logger = logging.getLogger(__name__)


_ALWAYS_BLOCKED_HINT = (
    "{site} blocks automated scraping outright ({why}, confirmed repeatedly), "
    "so Gear Scout doesn't load it at all — use the manual link below instead."
)


def _known_blocked(source: dict, keywords: list[str], cfg: dict):
    """Sites that have refused every automated visit — answering with their
    manual link straight away saves loading a page just to be turned away.
    For a single-term live search the link opens that search on the site
    (URLs taken from each site's own search box / condition filter)."""
    url = source.get("url", "")
    term = keywords[0].strip() if len(keywords) == 1 and keywords[0].strip() else ""
    q = quote_plus(term)
    if "guitarcenter.com" in url:
        site, why = "Guitar Center", "Akamai bot protection with a reCAPTCHA challenge"
        link = f"https://www.guitarcenter.com/search?filters=condition:Used&Ntt={q}" if term else url
    elif "sweetwater.com" in url:
        site, why = "Sweetwater", "Akamai/PerimeterX bot protection with a human-verification challenge"
        link = f"https://www.sweetwater.com/used/listings?query={q}" if term else url
    elif "proaudiostar.com" in url:
        site, why = "Pro Audio Star", "Cloudflare bot protection — it refuses every automated request (403)"
        link = (f"https://www.proaudiostar.com/catalogsearch/result/?q={q}" if term
                else "https://www.proaudiostar.com/catalogsearch/result/?q=open+box")
    elif "gearspace.com" in url:
        site, why = "Gearspace", "a Cloudflare security-verification challenge"
        link = url
    elif "ebay.com" in url and not (
        (cfg.get("ebay_api") or {}).get("client_id") and (cfg.get("ebay_api") or {}).get("client_secret")
    ):
        site, why = "eBay", "Akamai bot protection — connect eBay's official API in Settings → eBay for real results"
        link = (f"https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=180014"
                f"&LH_ItemCondition=1500%7C2500%7C3000&_sop=10") if term else url
    else:
        return None
    return ScrapeResult(
        source_name=source["name"], source_url=link, success=False, blocked=True,
        error=f"{site} blocks automated scraping.",
        fix_hint=_ALWAYS_BLOCKED_HINT.format(site=site, why=why),
        duration_seconds=0.0,
    )


def dispatch_scrape(source: dict, keywords: list[str], cfg: dict):
    """Returns a ScrapeResult, or None for an unrecognized source type
    (caller should skip it, same as main.py always has)."""
    stype = source.get("type", "rss")
    url = source.get("url", "")

    if stype == "html":
        blocked = _known_blocked(source, keywords, cfg)
        if blocked:
            return blocked

    if stype == "reddit":
        from scrapers.rss_scraper import scrape_reddit
        oauth_cfg = cfg.get("reddit_oauth", {})
        return scrape_reddit(source, keywords, oauth_cfg=oauth_cfg)
    elif stype in ("rss", "ebay_rss"):
        return scrape_rss(source, keywords)
    elif stype == "html":
        if "guitarcenter" in url:
            return scrape_guitar_center(source, keywords)
        elif "sweetwater" in url:
            return scrape_sweetwater(source, keywords)
        elif "reverb.com" in url:
            from scrapers.html_scraper import scrape_reverb
            return scrape_reverb(source, keywords)
        elif "audiogon.com" in url:
            from scrapers.html_scraper import scrape_audiogon
            return scrape_audiogon(source, keywords)
        elif "vintageking.com" in url:
            from scrapers.html_scraper import scrape_vintageking
            return scrape_vintageking(source, keywords)
        elif "usaudiomart.com" in url:
            from scrapers.html_scraper import scrape_usaudiomart
            return scrape_usaudiomart(source, keywords)
        elif "ebay.com" in url:
            api_cfg = cfg.get("ebay_api") or {}
            if api_cfg.get("client_id") and api_cfg.get("client_secret"):
                from scrapers.ebay_api import scrape_ebay_api
                return scrape_ebay_api(source, keywords, api_cfg)
            from scrapers.html_scraper import scrape_ebay
            return scrape_ebay(source, keywords)
        else:
            from scrapers.html_scraper import scrape_forum_html
            return scrape_forum_html(source, keywords)
    elif stype == "shopify":
        from scrapers.shopify_store import scrape_shopify_collection
        return scrape_shopify_collection(source, keywords)
    elif stype == "facebook_posts":
        from scrapers.fb_posts import scrape_facebook_posts
        return scrape_facebook_posts(source, keywords, cfg)
    elif stype == "long_mcquade":
        from scrapers.long_mcquade import scrape_long_mcquade
        return scrape_long_mcquade(source, keywords)
    elif stype == "kijiji":
        from scrapers.kijiji import scrape_kijiji
        return scrape_kijiji(source, keywords)
    elif stype == "shopgoodwill":
        from scrapers.shopgoodwill import scrape_shopgoodwill
        return scrape_shopgoodwill(source, keywords)
    elif stype == "craigslist":
        return scrape_craigslist(source, keywords)
    elif stype == "craigslist_region":
        return scrape_craigslist_region(source, keywords)
    elif stype == "facebook":
        from scrapers.facebook_scraper import scrape_facebook_group
        return scrape_facebook_group(source, keywords)
    elif stype == "facebook_marketplace_region":
        from scrapers.facebook_scraper import scrape_facebook_marketplace_region
        broad = ((cfg or {}).get("facebook_marketplace") or {}).get("broad_terms")
        return scrape_facebook_marketplace_region({**source, "_broad_terms": broad}, keywords)
    else:
        logger.warning("Unknown source type '%s' for %s — skipping", stype, source.get("name"))
        return None
