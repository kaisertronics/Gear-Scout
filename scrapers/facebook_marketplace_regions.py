"""
The 8 US Facebook Marketplace regions and their location IDs.

Facebook's own Marketplace search is location-anchored (facebook.com/
marketplace/{location_id}/...), and the radius around that location is
whatever the user has configured inside Marketplace itself (Facebook
remembers it per-account, not something addable to the URL). These 8
location IDs were discovered by inspecting SearchTempest's own public
"open all regions" links — a well-established, unofficial technique long
used by other classifieds-search tools, since Facebook doesn't publish or
support a location-ID API for outside use.

If Facebook ever retires one of these IDs, re-discover it the same way:
do any search on searchtempest.com, open the "Facebook Marketplace" panel,
and inspect the region links' hrefs.
"""
from urllib.parse import quote_plus

FACEBOOK_MARKETPLACE_REGIONS = [
    ("Pacific Northwest", "113093802034968"),
    ("Southwest", "109546952404225"),
    ("Rocky Mountains", "106084172755635"),
    ("Texas", "105590109474550"),
    ("The South", "105701396129318"),
    ("Midwest", "108018822553353"),
    ("Mid-Atlantic", "107524245944156"),
    ("Southeast", "113541638659587"),
]


def facebook_marketplace_search_url(location_id: str, query: str = "") -> str:
    """The URL Facebook's own Marketplace search page uses for a given
    location and keyword. Without a query, this is that region's general
    Marketplace browse feed (the base /marketplace/{location_id} page, no
    /search segment — a "/search" path with no query isn't a real page)."""
    if query:
        return f"https://www.facebook.com/marketplace/{location_id}/search?query={quote_plus(query)}&sortBy=creation_time_descend&daysSinceListed=30"
    return f"https://www.facebook.com/marketplace/{location_id}"


def default_facebook_marketplace_region_sources() -> list[dict]:
    """The 8 canonical region sources as plain source dicts, matching what
    config.yaml.example ships — used by live_search.py the same way the
    Craigslist regions are, to make sure a live search always covers all
    eight even if the user has disabled one for scheduled runs."""
    return [
        {
            "name": f"FB Marketplace — {region_name}",
            "url": facebook_marketplace_search_url(location_id),
            "type": "facebook_marketplace_region",
            "enabled": True,
        }
        for region_name, location_id in FACEBOOK_MARKETPLACE_REGIONS
    ]
