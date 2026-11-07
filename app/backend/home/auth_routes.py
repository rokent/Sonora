"""Sign-in routes: OIDC login, guest join, display links, logout, who-am-I,
guest pass.

The OIDC flow is the authorization code flow with PKCE. The state and the
PKCE verifier ride in a short-lived signed cookie, so no server-side state is
needed between the redirect out and the callback.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .auth import (
    SESSION_COOKIE, DisplayLinks, GuestPass, Principal, auth_enabled, principal_of,
    read_payload, sign_payload, sign_session,
)

router = APIRouter()

OIDC_COOKIE = "sonora_oidc"
USER_MAX_AGE = 30 * 24 * 3600
GUEST_MAX_AGE = 12 * 3600
DISPLAY_MAX_AGE = 365 * 24 * 3600
OIDC_MAX_AGE = 600
DISCOVERY_TTL = 3600

_discovery_cache: dict[str, tuple[float, dict]] = {}


# --- the request, as the household reached it ---------------------------

def _trust_proxy() -> bool:
    return os.environ.get("SONORA_TRUST_PROXY") == "1"


def _origin(request: Request) -> tuple[str, str]:
    """(scheme, host) as the browser used them. Forwarded headers count only
    when a proxy we trust sets them: anyone can send them otherwise."""
    scheme, host = request.url.scheme, request.headers.get("host") or request.url.netloc
    if _trust_proxy():
        scheme = request.headers.get("x-forwarded-proto", scheme).split(",")[0].strip()
        host = request.headers.get("x-forwarded-host", host).split(",")[0].strip()
    return scheme, host


def _safe_next(value: str | None) -> str:
    """``value`` if it is a path on this site, else the home page."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        if "\\" not in value and "\n" not in value and "\r" not in value:
            return value
    return "/"


def _set_cookie(response: Response, name: str, value: str, max_age: int, scheme: str,
                *, cross_site: bool = False) -> None:
    """``cross_site`` (a display session) is ``SameSite=None; Secure`` over
    HTTPS, so the page still signs in when it is framed by another site (Home
    Assistant's iframe card). Browsers refuse ``SameSite=None`` without
    ``Secure``, so over plain HTTP it stays ``Lax``: there Home Assistant and
    Sonora must be the same site (same host name, any port) for the card."""
    secure = scheme == "https"
    samesite = "none" if cross_site and secure else "lax"
    response.set_cookie(name, value, max_age=max_age, httponly=True,
                        samesite=samesite, secure=secure, path="/")


def _principal(request: Request) -> Principal | None:
    found = getattr(request.state, "principal", None)
    if found is not None:
        return found
    return principal_of(request.cookies.get(SESSION_COOKIE),
                        request.headers.get("authorization"))


def _require_user(request: Request) -> Principal:
    p = _principal(request)
    if p is None:
        raise HTTPException(401, "Sign in required")
    if not p.is_user:
        raise HTTPException(403, "Members only")
    return p


# --- OIDC ---------------------------------------------------------------

async def _oidc_json(method: str, url: str, **kwargs: Any) -> dict:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        async with http.request(method, url, **kwargs) as resp:
            if resp.status >= 400:
                raise HTTPException(502, f"Identity provider answered {resp.status}")
            return await resp.json(content_type=None)


async def _discover() -> dict:
    issuer = os.environ["SONORA_OIDC_ISSUER"].strip().rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < DISCOVERY_TTL:
        return cached[1]
    doc = await _oidc_json("GET", f"{issuer}/.well-known/openid-configuration")
    _discovery_cache[issuer] = (time.monotonic(), doc)
    return doc


async def _exchange(code: str, verifier: str, redirect_uri: str) -> dict:
    """Trade the code for tokens, then ask the userinfo endpoint who it is."""
    meta = await _discover()
    tokens = await _oidc_json("POST", meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
        "client_id": os.environ.get("SONORA_OIDC_CLIENT_ID", ""),
        "client_secret": os.environ.get("SONORA_OIDC_CLIENT_SECRET", ""),
    })
    return await _oidc_json("GET", meta["userinfo_endpoint"], headers={
        "Authorization": f"Bearer {tokens['access_token']}"})


@router.get("/auth/login")
async def login(request: Request, next: str | None = None):
    if not auth_enabled():
        raise HTTPException(404, "Sign-in is not enabled")
    scheme, host = _origin(request)
    meta = await _discover()
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = urlencode({
        "response_type": "code",
        "client_id": os.environ.get("SONORA_OIDC_CLIENT_ID", ""),
        "redirect_uri": f"{scheme}://{host}/auth/callback",
        "scope": "openid profile email groups",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    response = RedirectResponse(f"{meta['authorization_endpoint']}?{query}", status_code=302)
    cookie = sign_payload({"s": state, "v": verifier, "next": _safe_next(next)}, OIDC_MAX_AGE, "oidc")
    _set_cookie(response, OIDC_COOKIE, cookie, OIDC_MAX_AGE, scheme)
    return response


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    if not auth_enabled():
        raise HTTPException(404, "Sign-in is not enabled")
    scheme, host = _origin(request)
    saved = read_payload(request.cookies.get(OIDC_COOKIE, ""), "oidc")
    if (not saved or not code or not state
            or not secrets.compare_digest(str(saved.get("s", "")), state)):
        raise HTTPException(400, "Sign-in expired, try again")
    info = await _exchange(code, saved["v"], f"{scheme}://{host}/auth/callback")
    required = os.environ.get("SONORA_OIDC_GROUP", "").strip()
    groups = info.get("groups") or []
    if required and (not isinstance(groups, list) or required not in groups):
        raise HTTPException(403, "Your account is not allowed in")
    sub = info.get("sub")
    if not sub:
        raise HTTPException(502, "Identity provider sent no subject")
    name = str(info.get("preferred_username") or info.get("name") or sub)
    response = RedirectResponse(_safe_next(saved.get("next")), status_code=302)
    _set_cookie(response, SESSION_COOKIE,
                sign_session(Principal("user", str(sub), name), USER_MAX_AGE),
                USER_MAX_AGE, scheme)
    response.delete_cookie(OIDC_COOKIE, path="/")
    return response


# --- guests, logout, who am I -------------------------------------------

@router.post("/auth/guest")
async def guest_join(request: Request):
    scheme, _ = _origin(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    if not GuestPass().check(str(body.get("token", ""))):
        raise HTTPException(403, "This guest link is no longer valid")
    name = str(body.get("name", "")).strip()[:30].strip()
    if not name:
        raise HTTPException(400, "Tell us your name")
    p = Principal("guest", f"guest:{secrets.token_urlsafe(9)}", name)
    response = JSONResponse({"ok": True, "principal": _dump(p)})
    _set_cookie(response, SESSION_COOKIE, sign_session(p, GUEST_MAX_AGE),
                GUEST_MAX_AGE, scheme)
    return response


@router.get("/auth/display")
async def display_join(request: Request, token: str = ""):
    """A TV opens its display link: a year-long read-only session, then the
    TV mode page. A deleted (or wrong) link lands on the page with
    ``error=removed`` so it can say so; no session is set."""
    scheme, _ = _origin(request)
    link = DisplayLinks().by_token(token)
    if link is None:
        return RedirectResponse("/tv?" + urlencode({"error": "removed"}), status_code=302)
    p = Principal("display", link["id"], link["name"])
    response = RedirectResponse("/tv?" + urlencode({"display": link["id"]}), status_code=302)
    _set_cookie(response, SESSION_COOKIE, sign_session(p, DISPLAY_MAX_AGE),
                DISPLAY_MAX_AGE, scheme, cross_site=True)
    return response


@router.post("/auth/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def _dump(p: Principal) -> dict:
    return {"kind": p.kind, "id": p.id, "name": p.name}


@router.get("/api/me")
async def me(request: Request):
    """Public: the frontend asks it to decide whether to send you to login."""
    p = _principal(request)
    return {"auth": auth_enabled(), "principal": _dump(p) if p else None}


@router.get("/api/guest-pass")
async def guest_pass(request: Request):
    _require_user(request)
    scheme, host = _origin(request)
    token = GuestPass().token()
    return {"token": token, "url": f"{scheme}://{host}/join?{urlencode({'token': token})}"}


@router.post("/api/guest-pass/rotate")
async def guest_pass_rotate(request: Request):
    _require_user(request)
    scheme, host = _origin(request)
    token = GuestPass().rotate()
    return {"token": token, "url": f"{scheme}://{host}/join?{urlencode({'token': token})}"}
