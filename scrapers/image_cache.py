"""
Keeps a small local copy of listing pictures whose links expire (Facebook's
picture links stop working after a few days), so they still show later.
Pictures are saved in the data folder and served by the dashboard.
"""
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

IMAGE_DIR = Path("/data/images")
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
EXPIRING_HOSTS = ("fbcdn.net", "fbsbx.com", "scontent")


def needs_cache(url: Optional[str]) -> bool:
    return bool(url) and any(h in url for h in EXPIRING_HOSTS)


def cache_name(url: str) -> str:
    return hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:20] + ".jpg"


def cached_path(url: Optional[str]) -> Optional[Path]:
    if not needs_cache(url):
        return None
    p = IMAGE_DIR / cache_name(url)
    return p if p.exists() else None


def _download(url: str) -> bool:
    p = IMAGE_DIR / cache_name(url)
    if p.exists():
        return True
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image") and len(r.content) < 2_000_000:
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(r.content)
            tmp.replace(p)
            return True
    except Exception as e:
        logger.debug("Image download failed for %s: %s", url, e)
    return False


def cache_images(urls: list[str], workers: int = 6) -> int:
    """Downloads pictures with expiring links (skips ones already saved)."""
    todo = list(dict.fromkeys(u for u in urls if needs_cache(u) and not cached_path(u)))
    if not todo:
        return 0
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="img") as pool:
        saved = sum(pool.map(_download, todo))
    logger.info("Saved %d of %d listing pictures locally", saved, len(todo))
    return saved


def cache_recent(days: int = 3, limit: int = 3000) -> int:
    """Saves pictures for recent listings that don't have a local copy yet
    (their links are still fresh)."""
    from scrapers.store import _conn
    with _conn() as conn:
        urls = [u for (u,) in conn.execute(
            "SELECT image_url FROM seen WHERE image_url IS NOT NULL AND hidden = 0 AND sold = 0"
            " AND first_seen >= datetime('now', ?) ORDER BY first_seen DESC LIMIT ?",
            (f"-{days} days", limit))]
    return cache_images(urls)
