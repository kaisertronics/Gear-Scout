"""
Liquidation and auction sources, on by default without touching config.yaml.

These are places that sell studio gear far under market (auction houses,
estate and surplus sales). Scheduled runs, background refreshes, "Scrape now"
and live searches add any of them missing from config.yaml's `sources:`; a
config entry with the same type or name wins, so adding one there (or
setting `enabled: false`) overrides the default.

Not in the per-term Wanted sweep (telex.SEARCH_TYPES): that searches every
term every 30 minutes, far more requests than these sites should get.

Checked and left out (see the build notes): Proxibid (Incapsula 403),
GovDeals (Akamai bot manager; search only through a keyed app API),
estatesales.net (sale-level pages only, no lots, prices or bids).
Municibid is in: its /browse?search= page is server-rendered and answered
plain polite HTTP with 200 (Bidspotter, AuctionNinja and Mac.bid were not
needed, so not probed).
"""

DEFAULT_SOURCES = [
    {"name": "HiBid", "url": "https://hibid.com/lots", "type": "hibid", "enabled": True},
    {"name": "Municibid", "url": "https://municibid.com/browse", "type": "municibid", "enabled": True},
]


def with_defaults(sources: list[dict]) -> list[dict]:
    """`sources` plus the default liquidation sources it doesn't already have
    (matched by type or name). Returns a new list; the config isn't changed."""
    have_types = {s.get("type") for s in sources}
    have_names = {s.get("name") for s in sources}
    return list(sources) + [dict(d) for d in DEFAULT_SOURCES
                            if d["type"] not in have_types and d["name"] not in have_names]
