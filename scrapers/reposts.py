"""Repost finder: one card per re-listed ad.

A seller who re-lists the same ad (same title + price, same site, same city)
gets a new link every time. Display lists (Home, Mics) pass through
collapse_reposts(): the copies fold into one card carrying
l["repost_count"] = how many times the ad was listed. Nothing stored changes.
"""
import logging


def repost_key(l):
    """source family | normalized title | price [| city | seller], or None when
    the ad has no usable title/price (those are never grouped)."""
    from scrapers.enrich import dup_key, source_family
    key = dup_key(l.get("title"), l.get("price"))
    if not key:
        return None
    fam = source_family(l.get("url"))
    city = str(l.get("location") or "").split(" @")[0].strip().lower()
    seller = str(l.get("seller") or l.get("seller_name") or "").strip().lower()
    return "|".join((fam, key, city, seller))


def _stamp(l):
    return l.get("first_seen") or ""


def collapse_reposts(listings):
    """Return the new list. Same-key ads with different links fold into the
    cheapest (ties: newest) one, placed where the group first appeared, with
    repost_count set (>= 2). Any error returns the input list unchanged."""
    try:
        groups, order = {}, []
        for i, l in enumerate(listings):
            k = repost_key(l) if isinstance(l, dict) else None
            if k is None:
                order.append(("one", l))
                continue
            if k not in groups:
                groups[k] = []
                order.append(("grp", k))
            groups[k].append(l)
        out = []
        for kind, v in order:
            if kind == "one":
                out.append(v)
                continue
            members = groups[v]
            urls = {m.get("url") for m in members}
            if len(urls) < 2:
                out.extend(members)  # same link repeated: not a repost, leave alone
                continue
            from scrapers.enrich import parse_price
            best = min(members, key=lambda m: (parse_price(m.get("price")) or 0, _neg(_stamp(m))))
            best["repost_count"] = len(urls)
            out.append(best)
        return out
    except Exception:
        logging.exception("repost grouping failed; showing ungrouped list")
        return listings


class _neg(str):
    """Sorts strings in reverse so min() prefers the newest timestamp."""
    def __lt__(self, other):
        return str.__gt__(self, other)

    def __gt__(self, other):
        return str.__lt__(self, other)
