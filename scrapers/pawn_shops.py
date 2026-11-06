"""
Used-gear shop sources (consignment and trade-in stores that price mics by
feel rather than by market data), on by default without touching config.yaml.

All are Shopify "used" collections read by the `shopify` source type. Scheduled
runs, background refreshes, "Scrape now" and live searches add any of them
missing from config.yaml's `sources:`; a config entry with the same name or URL
wins, so adding one there (or setting `enabled: false`) overrides the default.

Unlike liquidation.with_defaults this does NOT match on type: every source here
is `shopify`, and config.yaml already has shopify sources, so a type match
would drop them all.

Checked and left out: Chicago Music Exchange (tc-vintage-and-used is a 2 MB
download that parses to only a handful of in-stock items; tc-pro-audio mixes
new stock the shopify scraper would label "used"), Vintage King (used-gear collection is empty), Wildwood
Guitars (guitar-only used stock), Perfect Circuit / Rudy's / Alto (already in
config.yaml), Guitar Center / Sam Ash (not Shopify).
"""

DEFAULT_SOURCES = [
    {"name": "Mass Street Music — Used",
     "url": "https://www.massstreetmusic.com/collections/used",
     "type": "shopify", "enabled": True},
]


def _key(url: str) -> str:
    return (url or "").split("?")[0].rstrip("/").lower()


def with_defaults(sources: list[dict]) -> list[dict]:
    """`sources` plus the default used-shop sources it doesn't already have
    (matched by name or URL). Returns a new list; the config isn't changed."""
    have_names = {s.get("name") for s in sources}
    have_urls = {_key(s.get("url")) for s in sources}
    return list(sources) + [dict(d) for d in DEFAULT_SOURCES
                            if d["name"] not in have_names and _key(d["url"]) not in have_urls]
