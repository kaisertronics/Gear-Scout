"""
Shopping-list deals.

When a NEW listing (first seen in the last few hours) matches an item on the
studio shopping list (scrapers/shopping_list.py) and its price is a real
deal, one push goes out (all hits in a single push). A "deal" is, in order:
  - at or under the item's "budget" if the list item has one, else
  - 25%+ under the used value from scrapers/market_value.py (when known), else
  - 25%+ under the item's new price on the list.
Items you already set an own-price target for (price_targets) are skipped,
because price_targets pushes for those. Each listing is pushed once
(table shoplist_alerted).
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
NEW_HOURS = 6
UNDER = 0.75  # 25% under


def _threshold(it: dict, title: str, desc):
    b = it.get("budget")
    if b:
        return float(b)
    try:
        from scrapers import market_value
        used, _n = market_value.value(title, desc)
    except Exception:
        used = None
    return round((used or it["new"]) * UNDER, 2)


def _targeted(it: dict, title: str, targets: list) -> bool:
    from scrapers.price_targets import _matches, _words
    for t in targets:
        w = _words(t["text"])
        if w and (_matches(w, it["name"]) or _matches(w, title)):
            return True
    return False


def find(cfg: dict) -> list:
    from scrapers import price_targets, shopping_list
    targets = price_targets.load()
    cut = (datetime.now(timezone.utc) - timedelta(hours=NEW_HOURS)).isoformat()
    rows = [r for r in price_targets._candidates(cfg, NEW_HOURS) if (r.get("first_seen") or "") >= cut]
    out, seen = [], set()
    for r in rows:
        if r["url"] in seen or not shopping_list._usable(r):
            continue
        for it in shopping_list.ITEMS:
            try:
                ok = shopping_list.matches_item(it, r["title"], r.get("description"))
            except Exception:
                ok = False
            if not ok or _targeted(it, r["title"], targets):
                continue
            price, qty, unit = shopping_list._unit_of(r)
            if not unit:
                continue
            ship = r.get("shipping") if isinstance(r.get("shipping"), (int, float)) else 0
            total = unit + ship / max(qty, 1)
            if total <= _threshold(it, r["title"], r.get("description")):
                seen.add(r["url"])
                out.append(dict(r, item=it["name"], unit_total=round(total, 2)))
            break
    return out


def alert(cfg: dict) -> int:
    from scrapers.store import _conn
    rows = find(cfg)
    if not rows:
        return 0
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS shoplist_alerted (url TEXT PRIMARY KEY, at TEXT)")
        done = {u for (u,) in conn.execute("SELECT url FROM shoplist_alerted")}
    rows = sorted((r for r in rows if r["url"] not in done), key=lambda r: r["unit_total"])[:10]
    if not rows:
        return 0
    from scrapers.notify import push_enabled, send_push
    if push_enabled(cfg):
        title = f"Shopping list: {len(rows)} deal{'s' if len(rows) > 1 else ''}"
        body = "\n".join(f"• {r['item'][:40]}: {r['title'][:50]} — {r['price']}" for r in rows[:6])
        send_push(cfg, title, body, url=rows[0]["url"] if len(rows) == 1 else (cfg.get("email") or {}).get("dashboard_url"),
                  tags=["shopping_cart"])
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO shoplist_alerted VALUES (?, ?)", [(r["url"], now) for r in rows])
        conn.commit()
    logger.info("Shopping-list alerts: %d", len(rows))
    return len(rows)
