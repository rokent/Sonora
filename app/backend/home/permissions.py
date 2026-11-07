"""Who may reach what, decided on the server for every request.

Three answers, in order:

* ``PUBLIC`` paths need nobody signed in: the page shell and its bundle, the
  guest join page, the sign-in routes, ``/api/me``, the health check, and
  what the speakers themselves fetch (streams, art, Sonora Home media), since
  a speaker carries no session.
* A household member (``user``) reaches everything. A ``service`` (Home
  Assistant's bearer token) reaches only ``/api/ha/``. A ``display`` (a TV
  let in with a display link) is read-only: only what ``DISPLAY`` lists.
* A ``guest`` is default-deny: only what ``GUEST`` lists. A route Sonora adds
  upstream is therefore closed to guests until someone opens it here on
  purpose.

With sign-in off (no ``SONORA_OIDC_ISSUER``) every request is the local user
and this changes nothing.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import SESSION_COOKIE, Principal, display_session_removed, principal_of

_SEG = r"[^/]+"


def _rule(method: str, pattern: str) -> tuple[str, re.Pattern]:
    """``method`` is an HTTP method or ``*``; the pattern must match the whole path."""
    return method, re.compile(pattern)


#: No principal needed.
PUBLIC: list[tuple[str, re.Pattern]] = [
    _rule("GET", r"/"),
    _rule("GET", r"/join"),                       # the guest join page
    _rule("GET", r"/tv"),                         # TV mode shell; it says when a link was removed
    _rule("GET", r"/assets/.+"),                  # the hashed bundle
    _rule("GET", r"/(favicon\.ico|apple-touch-icon\.png|manifest\.webmanifest"
                 r"|icon-\d+\.png)"),             # root files the page links
    _rule("*", r"/auth/.*"),
    _rule("GET", r"/api/me"),
    _rule("GET", rf"/api/stream/audio/{_SEG}"),   # fetched by speakers
    _rule("GET", r"/api/art"),                    # fetched by speakers and <img>
    _rule("GET", r"/api/home/media/.+"),          # Phase 2+ media for speakers
    _rule("GET", r"/api/home/live/.+"),           # Phase 4 live sound for speakers
    _rule("GET", r"/api/home/files/.+"),          # dropped files, for speakers
    _rule("GET", r"/api/health"),                 # backend.healthcheck
]

#: What a guest may reach. Everything else is refused.
GUEST: list[tuple[str, re.Pattern]] = [
    _rule("GET", r"/api/state"),
    _rule("GET", r"/ws"),
    _rule("GET", r"/party(/.*)?"),                # the party page shell
    _rule("GET", r"/api/home/config"),
    _rule("GET", r"/api/parties"),
    _rule("GET", rf"/api/parties/{_SEG}/queue"),
    _rule("POST", rf"/api/parties/{_SEG}/join"),
    _rule("POST", rf"/api/parties/{_SEG}/queue"),
    _rule("POST", rf"/api/parties/{_SEG}/youtube"),
    _rule("POST", rf"/api/parties/{_SEG}/files"),
    _rule("GET", r"/api/youtube/search"),
    _rule("GET", r"/api/airplay/receivers"),      # the AirPlay names help
    _rule("DELETE", rf"/api/parties/{_SEG}/queue/{_SEG}"),
    _rule("POST", rf"/api/parties/{_SEG}/transport/(play|pause|next|previous)"),
    _rule("POST", rf"/api/parties/{_SEG}/volume"),
    _rule("GET", r"/api/favorites"),
    _rule("GET", r"/api/services"),
    _rule("GET", r"/api/services/\d+/(browse|search|letters|item)"),
    _rule("GET", r"/api/browse(/.*)?"),
    _rule("GET", rf"/api/queue/{_SEG}"),
    _rule("GET", r"/api/(i18n|locales?)(/.*)?"),
    _rule("GET", r"/api/themes(/.*)?"),
]

#: What a ``display`` (a TV screen) may reach: reads that follow a party, and
#: nothing that changes anything. ``/tv`` and ``/api/art`` are public too.
DISPLAY: list[tuple[str, re.Pattern]] = [
    _rule("GET", r"/api/state"),
    _rule("GET", r"/ws"),
    _rule("GET", r"/ws/visual"),
    _rule("GET", r"/api/parties"),
    _rule("GET", rf"/api/parties/{_SEG}/queue"),
    _rule("GET", r"/api/home/config"),
    _rule("GET", r"/api/art"),
    _rule("GET", r"/api/displays/me"),
    _rule("GET", r"/tv"),
]

#: Where a ``service`` principal (Home Assistant) may go.
SERVICE_PREFIX = "/api/ha/"


def _matches(rules: list[tuple[str, re.Pattern]], method: str, path: str) -> bool:
    return any(m in ("*", method) and rx.fullmatch(path) for m, rx in rules)


def allowed(principal: Principal | None, method: str, path: str) -> bool:
    method = method.upper()
    if method == "HEAD":
        method = "GET"     # Starlette answers HEAD with the GET route
    if _matches(PUBLIC, method, path):
        return True
    if principal is None:
        return False
    if principal.kind == "user":
        return True
    if principal.kind == "service":
        return path.startswith(SERVICE_PREFIX)
    if principal.kind == "display":
        return _matches(DISPLAY, method, path)
    if principal.kind == "guest":
        return _matches(GUEST, method, path)
    return False


def _principal(connection) -> Principal | None:
    return principal_of(connection.cookies.get(SESSION_COOKIE),
                        connection.headers.get("authorization"))


def _is_page(method: str, path: str) -> bool:
    """A browser loading a page, as opposed to the interface calling the API."""
    return (method in ("GET", "HEAD") and path not in ("/ws", "/ws/visual")
            and not path.startswith(("/api/", "/auth/")))


async def guard(request: Request, call_next):
    """Put the principal on the request, then let it through or refuse it."""
    principal = _principal(request)
    request.state.principal = principal
    method, path = request.method, request.url.path
    if allowed(principal, method, path):
        return await call_next(request)
    if principal is None:
        if _is_page(method, path):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse("/auth/login?" + urlencode({"next": target}),
                                    status_code=302)
        # A TV whose display link was deleted: say so, so it shows that
        # rather than a sign-in prompt nobody at a TV can answer.
        error = ("display_removed"
                 if display_session_removed(request.cookies.get(SESSION_COOKIE))
                 else "login")
        return JSONResponse({"error": error}, status_code=401)
    return JSONResponse({"error": "forbidden"}, status_code=403)


def ws_allowed(websocket) -> bool:
    """Whether this socket's principal may open ``/ws`` (or ``/ws/visual``):
    users, guests and displays may. HTTP middleware never
    sees a socket, so the endpoint asks this itself."""
    principal = _principal(websocket)
    websocket.state.principal = principal
    return allowed(principal, "GET", websocket.url.path)
