"""Shopping-list bundle shipping saver. Pure functions, read-only.

When 2+ shopping-list items (still needed) have a live listing from the SAME seller on the
same site, buying together can mean one shipment instead of several. Estimate what that saves."""

MIN_ITEMS = 2          # different shopping-list items from one seller
FLAT_EXTRA = 4.0       # conservative per-extra-item guess when a listing's shipping is unknown/zero
LIMIT = 5


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def estimate_saved(ships):
    """ships = listed shipping per listing (0/None = unknown or free). You still pay the biggest
    shipping once; each extra listing saves its own shipping, or FLAT_EXTRA when unknown."""
    ships = [max(_num(s), 0.0) for s in ships]
    if len(ships) < 2:
        return 0.0
    ships.sort(reverse=True)
    return round(sum(s if s > 0 else FLAT_EXTRA for s in ships[1:]), 2)


def find_groups(items, bought=None):
    """items = shopping_list.build()["items"]; bought = {key: pieces bought}. Returns up to LIMIT
    groups: {seller, source, lines[{name,title,url,pieces,cost}], total, saved}, biggest saving first."""
    try:
        bought = bought or {}
        groups: dict = {}
        for it in items or []:
            left = it["qty"] - min(max(int(bought.get(it["key"], 0) or 0), 0), it["qty"])
            if left <= 0:
                continue
            best: dict = {}
            for m in it.get("by_seller") or []:
                g = (m.get("source"), str(m.get("seller")).strip().lower())
                if g not in best or m["delivered"] < best[g]["delivered"]:
                    best[g] = m
            for g, m in best.items():
                groups.setdefault(g, []).append((it, m, left))
        out = []
        for (source, _sel), rows in groups.items():
            if len(rows) < MIN_ITEMS:
                continue
            lines, ships, total = [], [], 0.0
            for it, m, left in rows:
                pcs = max(1, min(int(m.get("pieces") or 1), left))
                cost = (_num(m.get("unit")) + _num(m.get("ship"))) * pcs
                total += cost
                ships.append(_num(m.get("ship")) * pcs)
                lines.append({"name": it["name"], "title": m["title"], "url": m["url"],
                              "pieces": pcs, "cost": round(cost, 2)})
            saved = estimate_saved(ships)
            if saved <= 0:
                continue
            out.append({"seller": rows[0][1]["seller"], "source": source, "lines": lines,
                        "total": round(total, 2), "saved": saved})
        out.sort(key=lambda g: -g["saved"])
        return out[:LIMIT]
    except Exception:
        return []
