"""Send a Make-an-Offer on a Reverb listing through Reverb's API.

Reverb doesn't document the offer body, so build_payload() is the one place
to adjust if Reverb rejects it. The token is read from config.yaml
(reverb_api.token) only inside _token() and is never put in a result."""
import re
from typing import Optional

import requests

from .reverb_api import reverb_get

API = "https://api.reverb.com/api"
CONFIG_PATH = "/config/config.yaml"
_ITEM_RE = re.compile(r"reverb\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?item/(\d+)", re.I)


class NoToken(Exception):
    pass


def _token() -> str:
    import yaml
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        raise NoToken("no Reverb token (config.yaml could not be read)")
    tok = str(((cfg.get("reverb_api") or {}).get("token")) or "").strip()
    if not tok:
        raise NoToken("no Reverb token set in config.yaml (reverb_api.token)")
    return tok


def listing_id_from_url(url) -> Optional[str]:
    m = _ITEM_RE.search(str(url or ""))
    return m.group(1) if m else None


def build_payload(amount, address_id=None) -> dict:
    """The offer request body (best guess; Reverb doesn't document it)."""
    p = {"price": {"amount": "%.2f" % float(amount), "currency": "USD"}}
    if address_id:
        p["shipping_address_id"] = address_id
    return p


def _headers(token: str) -> dict:
    return {"Accept-Version": "3.0", "Accept": "application/hal+json",
            "Content-Type": "application/hal+json", "Authorization": "Bearer " + token,
            "User-Agent": "GearScout/1.0 (self-hosted gear alerts)"}


def _num(v) -> Optional[float]:
    try:
        n = float(re.sub(r"[^\d.]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return None
    return n if n > 0 else None


def _reason(resp, token="") -> str:
    """Reverb's own words for a failure, short, never containing the token."""
    txt = ""
    try:
        j = resp.json()
        if isinstance(j, dict):
            txt = j.get("message") or ""
            if j.get("errors"):
                txt = (txt + " " if txt else "") + str(j["errors"])
    except Exception:
        pass
    txt = txt or (getattr(resp, "text", "") or "")
    if token:
        txt = txt.replace(token, "***")
    return re.sub(r"\s+", " ", str(txt)).strip()[:300]


def copy_message(amount) -> str:
    return ("Hi, I'm interested in this. Would you take $%s for it? "
            "I can pay right away. Thanks!" % format(int(round(float(amount))), ","))


def _result(ok, status, message, amount, payload=None, dry_run=False) -> dict:
    return {"ok": ok, "dry_run": dry_run, "status": status, "message": message,
            "payload_preview": payload, "copy_msg": copy_message(amount) if amount and amount > 0 else ""}


def send_offer(url, amount, dry_run=False, title="", source="Reverb", seller="", asking=None) -> dict:
    token = ""
    amt = _num(amount)
    ask = _num(asking)
    try:
        lid = listing_id_from_url(url)
        if not lid:
            return _result(False, None, "Not a Reverb item link", amt, dry_run=dry_run)
        if not amt:
            return _result(False, None, "Offer must be more than $0", None, dry_run=dry_run)
        if ask and amt > ask:
            return _result(False, None, "Offer is higher than the asking price", amt, dry_run=dry_run)
        token = _token()
        h = _headers(token)
        if dry_run:
            return _result(True, None, "Dry run, nothing sent", amt, build_payload(amt, "<address id>"), True)

        r = reverb_get("%s/listings/%s" % (API, lid), headers=h)
        if not (200 <= r.status_code < 300):
            return _result(False, r.status_code, "Reverb said: " + _reason(r, token), amt)
        listing = r.json()
        live = _num(((listing.get("price") or {}).get("amount")))
        if live and amt > live:
            return _result(False, None, "Offer is higher than the asking price ($%s)" % format(int(live), ","), amt)
        ask = ask or live
        href = (((listing.get("_links") or {}).get("make_offer") or {}).get("href"))
        if not href:
            return _result(False, None, "This listing doesn't take offers", amt)

        address_id = None
        ra = reverb_get(API + "/my/addresses", headers=h)
        if 200 <= ra.status_code < 300:
            addrs = (ra.json() or {}).get("addresses") or []
            pick = next((a for a in addrs if a.get("default")), addrs[0] if addrs else None)
            address_id = pick.get("id") if pick else None

        payload = build_payload(amt, address_id)
        resp = requests.post(href, json=payload, headers=h, timeout=20)
        if 200 <= resp.status_code < 300:
            try:
                from . import offer_log
                offer_log.add(url, title, source, seller, ask or amt, amt, "no_reply")
            except Exception:
                pass
            return _result(True, resp.status_code, "Offer sent", amt, payload)
        return _result(False, resp.status_code, "Reverb said: " + _reason(resp, token), amt, payload)
    except NoToken as e:
        return _result(False, None, str(e), amt)
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, str(e))
        if token:
            msg = msg.replace(token, "***")
        return _result(False, None, "Could not reach Reverb (%s)" % msg[:200], amt)
