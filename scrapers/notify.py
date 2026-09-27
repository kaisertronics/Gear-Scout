"""
Phone push notifications via ntfy (https://ntfy.sh) — a free app for iOS and
Android with no account needed: the phone subscribes to a topic name, and
anything published to that topic shows up as a notification. Topic names are
effectively the password (anyone who knows one can read it), so Settings
generates a long random one.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"


def push_enabled(cfg: dict) -> bool:
    return bool((cfg.get("notifications") or {}).get("ntfy_topic", "").strip())


def send_push(cfg: dict, title: str, message: str, url: Optional[str] = None,
              tags: Optional[list[str]] = None) -> bool:
    ncfg = cfg.get("notifications") or {}
    topic = (ncfg.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    server = (ncfg.get("ntfy_server") or DEFAULT_SERVER).strip().rstrip("/")
    payload = {"topic": topic, "title": title[:250], "message": message[:3500]}
    if url:
        payload["click"] = url
    if tags:
        payload["tags"] = tags
    try:
        # JSON publishing (POST to the server root) keeps titles/messages
        # UTF-8 safe — header-based publishing can't carry "—" or emoji.
        resp = requests.post(server, json=payload, timeout=15)
        if resp.status_code >= 300:
            logger.warning("ntfy push failed (HTTP %s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("ntfy push failed: %s", e)
        return False
