"""
Weekend pickup route: live local-pickup deals (Facebook Marketplace,
Craigslist, OfferUp...) grouped into area clusters, each with a suggested
stop order (nearest-neighbour from home) and an estimated drive distance.

Pure functions only: no database, no network. The dashboard passes in the
already-decorated deal listings and the home coordinates.
"""
import re
from typing import Callable, Optional

CLUSTER_MILES = 10.0  # towns closer than this to a cluster's first stop share a cluster
_LOCAL = re.compile(r"(?:FB |Facebook|Craigslist|OfferUp|seattle group)", re.I)
ROAD_FACTOR = 1.3  # straight-line miles -> rough road miles

Coord = tuple[float, float]


def is_local(l: dict) -> bool:
    return bool(_LOCAL.match(l.get("source_name") or ""))


def is_live(l: dict) -> bool:
    return not (l.get("sold") or l.get("pending") or l.get("hidden") or l.get("duplicate"))


def area_name(location: Optional[str]) -> str:
    return re.sub(r"\s*@.*$", "", location or "").strip()


def select(listings: list[dict]) -> list[dict]:
    """Local-pickup, still-live listings, de-duplicated by URL."""
    seen, out = set(), []
    for l in listings or []:
        url = l.get("url")
        if not url or url in seen or not is_local(l) or not is_live(l):
            continue
        seen.add(url)
        out.append(l)
    return out


def nearest_order(start: Optional[Coord], stops: list[tuple[Coord, object]],
                  dist: Callable[[Coord, Coord], float]) -> tuple[list, float]:
    """Nearest-neighbour order from `start`. stops = [(coord, payload)].
    Returns (payloads in order, straight-line miles from start through each
    stop; no return leg). With no start the westmost stop goes first."""
    left = list(stops)
    order, total = [], 0.0
    cur = start
    if cur is None and left:
        first = min(left, key=lambda s: (s[0][1], s[0][0]))
        left.remove(first)
        order.append(first[1])
        cur = first[0]
    while left:
        nxt = min(left, key=lambda s: dist(cur, s[0]))
        total += dist(cur, nxt[0])
        left.remove(nxt)
        order.append(nxt[1])
        cur = nxt[0]
    return order, total


def build(listings: list[dict], home: Optional[Coord], coords: Callable[[Optional[str]], Optional[Coord]],
          dist: Callable[[Coord, Coord], float], cluster_miles: float = CLUSTER_MILES) -> dict:
    """{'clusters': [...], 'unplaced': [...], 'count': n, 'total_miles': x}.

    Each cluster: {'name', 'listings', 'stops' (ordered listings), 'miles'
    (home -> stops in order -> home, rough road miles), 'from_home'}.
    Listings get 'route_miles' (straight-line from home) when known.
    Clusters are ordered by nearest-neighbour from home too.
    """
    rows = select(listings)
    placed, unplaced = [], []
    for l in rows:
        c = coords(l.get("location"))
        (placed if c else unplaced).append((c, l))
    for l in rows:
        l["area"] = area_name(l.get("location"))
    for c, l in placed:
        l["route_miles"] = round(dist(home, c)) if home else None
    # cluster greedily, closest-to-home (or most listings) first
    placed.sort(key=lambda p: (dist(home, p[0]) if home else 0, area_name(p[1].get("location")), p[1].get("url")))
    clusters: list[dict] = []
    for c, l in placed:
        for cl in clusters:
            if dist(cl["anchor"], c) <= cluster_miles:
                cl["items"].append((c, l))
                break
        else:
            clusters.append({"anchor": c, "items": [(c, l)]})
    out = []
    for cl in clusters:
        items = cl["items"]
        order, leg = nearest_order(home, [(c, l) for c, l in items], dist)
        # distance: home -> first, then between stops, then back home
        pts = [coords(l.get("location")) for l in order]
        miles = 0.0
        if home and pts:
            miles = dist(home, pts[0]) + sum(dist(a, b) for a, b in zip(pts, pts[1:])) + dist(pts[-1], home)
        elif pts:
            miles = sum(dist(a, b) for a, b in zip(pts, pts[1:]))
        names = [area_name(l.get("location")) for l in order]
        name = max(set(names), key=lambda n: (names.count(n), -names.index(n))) if names else "Area"
        miles *= ROAD_FACTOR
        out.append({"name": name, "listings": order, "stops": order, "miles": round(miles),
                    "anchor": cl["anchor"], "from_home": round(dist(home, cl["anchor"])) if home else None})
    # order clusters nearest-neighbour from home
    corder, _ = nearest_order(home, [(c["anchor"], c) for c in out], dist)
    total = sum(c["miles"] for c in corder)
    return {"clusters": corder, "unplaced": [l for _, l in unplaced],
            "count": len(rows), "total_miles": total}
