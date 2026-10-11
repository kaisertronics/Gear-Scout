"""
One-off (safe to re-run): looks up US shipping and the seller's country for stored Reverb and eBay
listings on your Wanted list that were saved before shipping was recorded.
New listings get it from the scrapes themselves.

    python -m scrapers.shipping_backfill [--count]
"""
import logging
import re
import sqlite3
import sys
import time

import requests

logger = logging.getLogger(__name__)
EBAY_MAX = 1500  # eBay's daily allowance is ~5,000 calls; leave plenty for searching


def _todo(cfg: dict) -> list[tuple[str, str]]:
    from scrapers import wanted
    from scrapers.store import _conn
    ms = wanted.matchers(cfg)
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT url, title, price, source_name FROM seen WHERE (shipping IS NULL OR country IS NULL) AND COALESCE(sold, 0) = 0
               AND COALESCE(hidden, 0) = 0 AND COALESCE(duplicate, 0) = 0
               AND (url LIKE '%reverb.com/item/%' OR url LIKE '%ebay.com/itm/%')""").fetchall()
    return [(r["url"], "reverb" if "reverb.com" in r["url"] else "ebay") for r in rows
            if wanted.match(r["title"], r["price"], ms, r["source_name"])]


def main() -> None:
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = yaml.safe_load(open("/config/config.yaml")) or {}
    todo = _todo(cfg)
    reverb = [u for u, s in todo if s == "reverb"]
    ebay = [u for u, s in todo if s == "ebay"][:EBAY_MAX]
    logger.info("Shipping backfill: %d Reverb, %d eBay listings", len(reverb), len(ebay))
    if "--count" in sys.argv:
        return
    from scrapers.reverb_api import HEADERS, country_of as reverb_country, shipping_of as reverb_ship
    from scrapers.store import set_country, set_shipping
    done = 0
    for url in reverb:
        m = re.search(r"/item/(\d+)", url)
        try:
            r = requests.get(f"https://api.reverb.com/api/listings/{m.group(1)}", headers=HEADERS, timeout=20)
            if r.ok:
                ship = reverb_ship(r.json())
                set_shipping(url, ship if ship is not None else None)
                set_country(url, reverb_country(r.json()))
                done += ship is not None
        except Exception as e:
            logger.debug("Reverb shipping failed for %s: %s", url, e)
        time.sleep(0.25)
    api = cfg.get("ebay_api") or {}
    if ebay and api.get("client_id") and api.get("client_secret"):
        from scrapers.ebay_api import SEARCH_URL, _check_response, _get_token, _headers, ebay_paused_until
        from scrapers.ebay_api import country_of as ebay_country, shipping_of as ebay_ship
        token = _get_token(api["client_id"].strip(), api["client_secret"].strip())
        for url in ebay:
            if ebay_paused_until():
                break
            m = re.search(r"/itm/(?:[^/]+/)?(\d{9,})", url)
            if not m:
                continue
            try:
                r = requests.get(SEARCH_URL.replace("item_summary/search", "item/get_item_by_legacy_id"),
                                 params={"legacy_item_id": m.group(1)}, headers=_headers(token), timeout=20)
                _check_response(r)
                if r.ok:
                    ship = ebay_ship(r.json())
                    set_shipping(url, ship)
                    set_country(url, ebay_country(r.json()))
                    done += ship is not None
            except Exception as e:
                logger.debug("eBay shipping failed for %s: %s", url, e)
            time.sleep(0.3)
    logger.info("Shipping backfill: %d listings got a shipping cost", done)


if __name__ == "__main__":
    main()
