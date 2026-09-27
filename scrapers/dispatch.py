"""
Shared "which scraper does this source's URL/type need" routing, used by
both the scheduled run (main.py) and on-demand live search (dashboard.py) so
both send a source through the exact same scraper.
"""
import logging

from scrapers.base import ScrapeResult
from scrapers.html_scraper import (
    scrape_craigslist,
    scrape_craigslist_region,
    scrape_guitar_center,
    scrape_sweetwater,
)
from scrapers.rss_scraper import scrape_rss

logger = logging.getLogger(__name__)


def dispatch_scrape(source: dict, keywords: list[str], cfg: dict):
    """Returns a ScrapeResult, or None for an unrecognized source type
    (caller should skip it, same as main.py always has)."""
    stype = source.get("type", "rss")
    url = source.get("url", "")

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
    elif stype == "craigslist":
        return scrape_craigslist(source, keywords)
    elif stype == "craigslist_region":
        return scrape_craigslist_region(source, keywords)
    elif stype == "facebook":
        from scrapers.facebook_scraper import scrape_facebook_group
        return scrape_facebook_group(source, keywords)
    elif stype == "facebook_marketplace_region":
        from scrapers.facebook_scraper import scrape_facebook_marketplace_region
        return scrape_facebook_marketplace_region(source, keywords)
    else:
        logger.warning("Unknown source type '%s' for %s — skipping", stype, source.get("name"))
        return None
