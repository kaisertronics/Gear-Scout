"""Send a Best Offer on an eBay Buy It Now listing (Trading API PlaceOffer).

Needs a USER token: the owner connects their eBay account once through eBay's
consent page (see docs/ebay_connect_steps.md). The refresh token lives in
config.yaml (ebay_api.user_refresh_token); the dashboard writes it, this module
only reads it. Access tokens are cached in memory. No token, secret or code is
ever put in a result or a log line."""
import base64
import re
import threading
import time
import urllib.parse
from typing import Optional
from xml.sax.saxutils import escape

import requests

from .reverb_offer import copy_message

CONFIG_PATH = "/config/config.yaml"
TRADING_URL = "https://api.ebay.com/ws/api.dll"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
AUTH_URL = "https://auth.ebay.com/oauth2/authorize"
SCOPE = "https://api.ebay.com/oauth/api_scope"
_ITEM_RE = re.compile(r"ebay\.com/itm/(?:[^/?#]*/)?(\d+)", re.I)

_cache = {"token": "", "exp": 0.0}
_lock = threading.Lock()


class NoToken(Exception):
    pass


class NotConnected(NoToken):
    pass


def _cfg() -> dict:
    import yaml
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        raise NoToken("config.yaml could not be read")
    return cfg.get("ebay_api") or {}


def _s(v) -> str:
    return str(v or "").strip()


def _redact(txt, *secrets) -> str:
    txt = str(txt or "")
    for s in secrets:
        if s and len(s) >= 6:
            txt = txt.replace(s, "***")
    # v^1.1#... style eBay tokens and long opaque strings
    txt = re.sub(r"v\^1\.1#[^\s\"'<>]+", "***", txt)
    return txt


def runame_set() -> bool:
    try:
        c = _cfg()
        return bool(_s(c.get("runame")) and _s(c.get("client_id")) and _s(c.get("client_secret")))
    except Exception:
        return False


def is_connected() -> bool:
    try:
        c = _cfg()
        return bool(_s(c.get("user_refresh_token")) and _s(c.get("client_id")) and _s(c.get("client_secret")))
    except Exception:
        return False


def consent_url(state) -> Optional[str]:
    c = _cfg()
    cid, ru = _s(c.get("client_id")), _s(c.get("runame"))
    if not cid or not ru:
        return None
    q = urllib.parse.urlencode({"client_id": cid, "response_type": "code", "redirect_uri": ru,
                                "scope": SCOPE, "state": state}, quote_via=urllib.parse.quote)
    return AUTH_URL + "?" + q


def _basic(c) -> str:
    raw = ("%s:%s" % (_s(c.get("client_id")), _s(c.get("client_secret")))).encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_error(resp, *secrets) -> str:
    txt = ""
    try:
        j = resp.json()
        txt = _s(j.get("error_description") or j.get("error"))
    except Exception:
        pass
    return _redact(txt or getattr(resp, "text", "") or ("HTTP %s" % resp.status_code), *secrets)[:200]


def exchange_code(code) -> dict:
    """Trade the consent code for tokens. Returns {refresh_token, access_token,
    expires_in}; raises NoToken with a secret-free reason on failure."""
    c = _cfg()
    ru = _s(c.get("runame"))
    if not (ru and _s(c.get("client_id")) and _s(c.get("client_secret"))):
        raise NoToken("eBay keys or RuName not set")
    code = urllib.parse.unquote(str(code or ""))
    r = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code, "redirect_uri": ru},
                      headers={"Authorization": _basic(c), "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=20)
    if not (200 <= r.status_code < 300):
        raise NoToken("eBay refused the sign-in (%s)" % _token_error(r, code, _s(c.get("client_secret"))))
    j = r.json()
    rt = _s(j.get("refresh_token"))
    if not rt:
        raise NoToken("eBay returned no refresh token")
    with _lock:
        _cache["token"] = _s(j.get("access_token"))
        _cache["exp"] = time.time() + float(j.get("expires_in") or 0) - 120
    return {"refresh_token": rt, "access_token": _cache["token"], "expires_in": j.get("expires_in")}


def access_token() -> str:
    with _lock:
        if _cache["token"] and time.time() < _cache["exp"]:
            return _cache["token"]
    c = _cfg()
    rt = _s(c.get("user_refresh_token"))
    if not rt:
        raise NotConnected("eBay account not connected (Settings > eBay > Connect eBay)")
    if not (_s(c.get("client_id")) and _s(c.get("client_secret"))):
        raise NoToken("eBay keys not set")
    r = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": rt, "scope": SCOPE},
                      headers={"Authorization": _basic(c), "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=20)
    if not (200 <= r.status_code < 300):
        raise NotConnected("eBay sign-in expired, reconnect in Settings (%s)"
                           % _token_error(r, rt, _s(c.get("client_secret"))))
    j = r.json()
    tok = _s(j.get("access_token"))
    if not tok:
        raise NotConnected("eBay returned no access token")
    with _lock:
        _cache["token"] = tok
        _cache["exp"] = time.time() + float(j.get("expires_in") or 0) - 120
    return tok


def clear_cache():
    with _lock:
        _cache["token"] = ""
        _cache["exp"] = 0.0


def listing_id_from_url(url) -> Optional[str]:
    m = _ITEM_RE.search(str(url or ""))
    return m.group(1) if m else None


def build_xml(item_id, amount, ip="127.0.0.1") -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<PlaceOfferRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            "<ItemID>%s</ItemID>"
            '<Offer><Action>BestOffer</Action><Amount currencyID="USD">%.2f</Amount><Quantity>1</Quantity></Offer>'
            "<EndUserIP>%s</EndUserIP>"
            "</PlaceOfferRequest>") % (escape(str(item_id)), float(amount), escape(str(ip)))


def _headers(token: str) -> dict:
    return {"X-EBAY-API-CALL-NAME": "PlaceOffer", "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1193", "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml"}


def _num(v) -> Optional[float]:
    try:
        n = float(re.sub(r"[^\d.]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return None
    return n if n > 0 else None


def _tag(xml, name) -> str:
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), xml or "", re.S)
    return m.group(1).strip() if m else ""


def parse_response(xml, token="") -> tuple:
    """(ok, reason). Success/Warning count as sent; Failure gives eBay's LongMessage."""
    xml = xml or ""
    ack = _tag(xml, "Ack")
    if ack in ("Success", "Warning"):
        return True, ""
    err = _tag(xml, "LongMessage") or _tag(xml, "ShortMessage") or re.sub(r"<[^>]+>", " ", xml)
    err = re.sub(r"\s+", " ", re.sub(r"&amp;", "&", err)).strip()
    return False, _redact(err, token)[:300] or "no reason given"


def _result(ok, status, message, amount, payload=None, dry_run=False) -> dict:
    return {"ok": ok, "dry_run": dry_run, "status": status, "message": message,
            "payload_preview": payload, "copy_msg": copy_message(amount) if amount and amount > 0 else ""}


def send_offer(url, amount, dry_run=False, title="", source="eBay", seller="", asking=None) -> dict:
    token = ""
    amt = _num(amount)
    ask = _num(asking)
    try:
        iid = listing_id_from_url(url)
        if not iid:
            return _result(False, None, "Not an eBay item link", amt, dry_run=dry_run)
        if not amt:
            return _result(False, None, "Offer must be more than $0", None, dry_run=dry_run)
        if ask and amt > ask:
            return _result(False, None, "Offer is higher than the asking price", amt, dry_run=dry_run)
        body = build_xml(iid, amt)
        if dry_run:
            if not is_connected():
                raise NotConnected("eBay account not connected (Settings > eBay > Connect eBay)")
            return _result(True, None, "Dry run, nothing sent", amt, body, True)
        token = access_token()
        resp = requests.post(TRADING_URL, data=body.encode("utf-8"), headers=_headers(token), timeout=25)
        ok, why = parse_response(resp.text, token)
        if ok and 200 <= resp.status_code < 300:
            try:
                from . import offer_log
                offer_log.add(url, title, source, seller, ask or amt, amt, "no_reply")
            except Exception:
                pass
            return _result(True, resp.status_code, "Offer sent", amt, body)
        return _result(False, resp.status_code, "eBay said: " + (why or "HTTP %s" % resp.status_code), amt, body)
    except NoToken as e:
        return _result(False, None, _redact(str(e), token), amt)
    except Exception as e:
        msg = _redact("%s: %s" % (type(e).__name__, e), token)
        return _result(False, None, "Could not reach eBay (%s)" % msg[:200], amt)
