"""Sonora Home permissions: who may reach what, enforced on the server.

Guests are default-deny: they reach a short allowlist and nothing else, so a
write Sonora adds upstream is closed to them until someone opens it on
purpose."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest

from backend import main
from backend.config import settings
from backend.home import permissions
from backend.home.auth import SESSION_COOKIE, Principal, sign_session
from backend.home.permissions import DISPLAY, GUEST, PUBLIC, allowed

from home_client import Client

GUEST_P = Principal("guest", "guest:abc", "Ana")
USER_P = Principal("user", "sub-1", "Tudor")
SERVICE_P = Principal("service", "ha", "Home Assistant")
DISPLAY_P = Principal("display", "d1", "Living Room TV")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def enable(monkeypatch):
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")


def client(principal: Principal | None = None) -> Client:
    c = Client(main.app, host="sonora.local")
    if principal is not None:
        c.cookies[SESSION_COOKIE] = sign_session(principal, 3600)
    return c


def _sample(path: str) -> str:
    # "1" fits every path parameter, the integer service ids included.
    return re.sub(r"\{[^}]+\}", "1", path)


def _routes(routes):
    """Every route of the app, the ones in included routers too (FastAPI keeps
    an included router as one entry wrapping the original)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _routes(inner.routes)
        else:
            yield route


def _reachable(principal: Principal) -> set[tuple[str, str]]:
    """(method, route path) for every HTTP route of the real app ``principal`` may call."""
    found = set()
    for route in _routes(main.app.routes):
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or ():
            if method != "HEAD" and allowed(principal, method, _sample(path)):
                found.add((method, path))
    return found


def _guest_reachable() -> set[tuple[str, str]]:
    return _reachable(GUEST_P)


# --- the rules ----------------------------------------------------------

#: Every write a guest can make on the real app: the public sign-in routes and
#: the party writes (join, queue add/remove, volume). The transport route's
#: ``{action}`` is sampled as "1", which no guest rule admits, so it is checked
#: on its own below. Anything else showing up means a write was opened to guests.
EXPECTED_GUEST_WRITES = {
    ("POST", "/auth/guest"),
    ("POST", "/auth/logout"),
    ("POST", "/api/parties/{party_id}/join"),
    ("POST", "/api/parties/{party_id}/queue"),
    ("POST", "/api/parties/{party_id}/youtube"),
    ("DELETE", "/api/parties/{party_id}/queue/{index}"),
    ("POST", "/api/parties/{party_id}/volume"),
}

#: Every read a guest can make on the real app, so a new upstream route that
#: falls under a wildcard (/api/browse*, /api/themes*) is noticed.
EXPECTED_GUEST_READS = {
    ("GET", "/"),
    ("GET", "/join"),
    ("GET", "/tv"),
    ("GET", "/party"),
    ("GET", "/party/{rest:path}"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("GET", "/auth/display"),
    ("GET", "/api/me"),
    ("GET", "/api/health"),
    ("GET", "/api/art"),
    ("GET", "/api/stream/audio/{key}"),
    ("GET", "/api/home/media/{key}.m4a"),          # public: speakers
    ("GET", "/api/state"),
    ("GET", "/api/home/config"),
    ("GET", "/api/favorites"),
    ("GET", "/api/services"),
    ("GET", "/api/services/{sid}/browse"),
    ("GET", "/api/services/{sid}/search"),
    ("GET", "/api/services/{sid}/letters"),
    ("GET", "/api/services/{sid}/item"),
    ("GET", "/api/browse"),
    ("GET", "/api/browse/letters"),
    ("GET", "/api/queue/{uuid}"),
    ("GET", "/api/parties"),
    ("GET", "/api/parties/{party_id}/queue"),
    ("GET", "/api/youtube/search"),
    ("GET", "/api/themes"),
    ("GET", "/api/themes/schema"),
    ("GET", "/api/themes/reference"),
}


def test_every_upstream_write_is_denied_to_guests():
    reachable = _guest_reachable()
    writes = {(m, p) for m, p in reachable if m != "GET"}
    assert writes == EXPECTED_GUEST_WRITES
    # the walk really saw Sonora's routes, included routers too
    all_writes = {(m, getattr(r, "path", "")) for r in _routes(main.app.routes)
                  for m in getattr(r, "methods", None) or () if m not in ("GET", "HEAD")}
    assert len(all_writes) > 50 and ("POST", "/auth/guest") in all_writes


def test_guest_transport_on_the_real_app():
    paths = {getattr(r, "path", "") for r in _routes(main.app.routes)
             if "POST" in (getattr(r, "methods", None) or ())}
    assert "/api/parties/{party_id}/transport/{action}" in paths
    for action in ("play", "pause", "next", "previous"):
        assert allowed(GUEST_P, "POST", f"/api/parties/p1/transport/{action}")
    assert not allowed(GUEST_P, "POST", "/api/parties/p1/transport/stop")


def test_guest_reads_on_the_real_app_are_pinned():
    reads = {(m, p) for m, p in _guest_reachable() if m == "GET"}
    assert reads == EXPECTED_GUEST_READS


#: Everything a display (a TV screen) can reach on the real app: the public
#: routes plus the read-only DISPLAY list. Its only writes are the public
#: sign-in routes, which need no principal at all.
EXPECTED_DISPLAY_REACHABLE = {
    # public
    ("GET", "/"),
    ("GET", "/join"),
    ("GET", "/tv"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("GET", "/auth/display"),
    ("POST", "/auth/guest"),
    ("POST", "/auth/logout"),
    ("GET", "/api/me"),
    ("GET", "/api/health"),
    ("GET", "/api/art"),
    ("GET", "/api/stream/audio/{key}"),
    ("GET", "/api/home/media/{key}.m4a"),
    # DISPLAY
    ("GET", "/api/state"),
    ("GET", "/api/home/config"),
    ("GET", "/api/parties"),
    ("GET", "/api/parties/{party_id}/queue"),
    ("GET", "/api/displays/me"),
}


def test_display_reach_on_the_real_app_is_pinned():
    assert _reachable(DISPLAY_P) == EXPECTED_DISPLAY_REACHABLE


def test_display_list_is_read_only():
    assert {m for m, _ in DISPLAY} == {"GET"}
    for path in ("/ws", "/ws/visual", "/tv", "/api/displays/me"):
        assert allowed(DISPLAY_P, "GET", path), path
    for method, path in (("POST", "/api/parties"), ("POST", "/api/parties/p1/join"),
                         ("POST", "/api/parties/p1/queue"), ("GET", "/api/favorites"),
                         ("GET", "/party"), ("GET", "/api/displays"),
                         ("GET", "/api/ha/status"), ("GET", "/api/queue/RINCON_1")):
        assert not allowed(DISPLAY_P, method, path), (method, path)


def test_guest_allowlist():
    assert allowed(GUEST_P, "POST", "/api/parties/p1/queue")
    assert allowed(GUEST_P, "POST", "/api/parties/p1/join")
    assert allowed(GUEST_P, "POST", "/api/parties/p1/youtube")
    assert allowed(GUEST_P, "GET", "/api/youtube/search")
    assert not allowed(GUEST_P, "POST", "/api/youtube/search")
    assert allowed(GUEST_P, "DELETE", "/api/parties/p1/queue/3")
    assert allowed(GUEST_P, "POST", "/api/parties/p1/volume")
    for action in ("play", "pause", "next", "previous"):
        assert allowed(GUEST_P, "POST", f"/api/parties/p1/transport/{action}")
    assert not allowed(GUEST_P, "POST", "/api/parties/p1/transport/stop")
    assert not allowed(GUEST_P, "POST", "/api/parties")
    assert not allowed(GUEST_P, "DELETE", "/api/parties/p1")
    assert not allowed(GUEST_P, "POST", "/api/zones/x/group")
    assert not allowed(GUEST_P, "POST", "/api/scenes/x/apply")
    assert not allowed(GUEST_P, "POST", "/api/presets")


def test_guest_reads():
    for path in ("/api/state", "/api/home/config", "/ws", "/api/parties", "/api/parties/p1/queue",
                 "/api/favorites", "/api/services", "/api/services/12/browse",
                 "/api/services/12/search", "/api/services/12/letters",
                 "/api/services/12/item", "/api/browse",
                 "/api/browse/letters", "/api/queue/RINCON_1", "/api/themes",
                 "/api/themes/schema", "/join", "/party", "/party/p1"):
        assert allowed(GUEST_P, "GET", path), path
        assert allowed(GUEST_P, "HEAD", path), path
    for path in ("/api/alarms", "/api/scenes", "/api/diagnostics", "/api/log",
                 "/api/guest-pass", "/api/presets", "/api/cloud/status",
                 "/api/zones/x/settings", "/api/queue/x/y", "/license",
                 "/api/library", "/api/parties/p1", "/api/parties/p1/members",
                 "/api/services/abc/browse", "/api/services/12/user-playlists"):
        assert not allowed(GUEST_P, "GET", path), path


def test_public_needs_no_principal():
    for method, path in (("GET", "/"), ("GET", "/join"), ("GET", "/tv"),
                         ("GET", "/auth/display"), ("GET", "/assets/app-1a2b.js"),
                         ("GET", "/favicon.ico"), ("GET", "/manifest.webmanifest"),
                         ("GET", "/auth/login"), ("GET", "/auth/callback"),
                         ("POST", "/auth/guest"), ("POST", "/auth/logout"),
                         ("GET", "/api/me"), ("GET", "/api/stream/audio/k1"),
                         ("GET", "/api/art"), ("GET", "/api/home/media/a/b.mp3"),
                         ("GET", "/api/health")):
        assert allowed(None, method, path), (method, path)
    for method, path in (("GET", "/api/state"), ("GET", "/ws"), ("GET", "/party"),
                         ("POST", "/api/me"), ("POST", "/api/art"), ("GET", "/license"),
                         ("GET", "/api/stream/next")):
        assert not allowed(None, method, path), (method, path)


def test_users_reach_everything_services_only_ha():
    for method, path in (("POST", "/api/zones/x/group"), ("DELETE", "/api/themes/t"),
                         ("GET", "/api/ha/status"), ("GET", "/license")):
        assert allowed(USER_P, method, path)
    assert allowed(SERVICE_P, "GET", "/api/ha/status")
    assert allowed(SERVICE_P, "POST", "/api/ha/parties/p1/start")
    assert allowed(SERVICE_P, "GET", "/api/health")  # public
    assert not allowed(SERVICE_P, "GET", "/api/state")
    assert not allowed(SERVICE_P, "POST", "/api/zones/x/group")
    assert not allowed(SERVICE_P, "GET", "/api/hall")


# --- the middleware, on the real app ------------------------------------

async def test_auth_off_everything_allowed():
    c = client()
    r = await c.request("GET", "/api/me")
    assert r.status == 200
    assert r.json() == {"auth": False,
                        "principal": {"kind": "user", "id": "local", "name": "Local"}}
    r = await c.request("GET", "/license")
    assert r.status not in (302, 401, 403)


async def test_api_without_session_is_401_and_page_redirects(monkeypatch):
    enable(monkeypatch)
    c = client()
    r = await c.request("GET", "/api/state")
    assert r.status == 401 and r.json() == {"error": "login"}
    r = await c.request("POST", "/api/zones/x/group")
    assert r.status == 401 and r.json() == {"error": "login"}
    r = await c.request("GET", "/party?id=p1")
    assert r.status == 302
    loc = urlparse(r.headers["location"])
    assert loc.path == "/auth/login"
    assert parse_qs(loc.query) == {"next": ["/party?id=p1"]}
    r = await c.request("GET", "/api/me")
    assert r.status == 200 and r.json() == {"auth": True, "principal": None}
    r = await c.request("GET", "/api/health")
    assert r.status == 200
    r = await c.request("GET", "/join?token=t")
    assert r.status not in (302, 401, 403, 404)


async def test_a_bad_bearer_is_401(monkeypatch):
    enable(monkeypatch)
    r = await client().request("GET", "/api/state", headers={"authorization": "Bearer nope"})
    assert r.status == 401 and r.json() == {"error": "login"}


async def test_guest_denied_is_403(monkeypatch):
    enable(monkeypatch)
    c = client(GUEST_P)
    r = await c.request("POST", "/api/zones/x/group")
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    r = await c.request("GET", "/api/alarms")
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    r = await c.request("GET", "/api/state")  # allowed: reaches the route
    assert r.status not in (401, 403)


async def test_principal_is_on_the_request(monkeypatch):
    enable(monkeypatch)
    r = await client(USER_P).request("GET", "/api/me")
    assert r.json()["principal"] == {"kind": "user", "id": "sub-1", "name": "Tudor"}


async def test_origin_guard_runs_before_sign_in(monkeypatch):
    """A foreign Host is refused by the origin guard before the sign-in guard
    could answer 401: host and DNS-rebinding refusal come first."""
    enable(monkeypatch)
    c = Client(main.app, host="evil.example")
    r = await c.request("GET", "/api/state")
    assert r.status == 403
    assert r.json()["detail"].startswith("Sonora does not answer to the name evil.example")
    dispatches = [m.kwargs.get("dispatch") for m in main.app.user_middleware]
    # user_middleware[0] is outermost, so it runs first
    assert dispatches.index(main.origin.guard) < dispatches.index(permissions.guard)


async def test_origin_guard_still_runs(monkeypatch):
    enable(monkeypatch)
    r = await client(USER_P).request("POST", "/api/refresh",
                                     headers={"origin": "https://evil.example"})
    assert r.status == 403
    assert r.json() == {"detail": "that request came from another website"}


def test_both_guards_are_installed():
    funcs = [getattr(m.kwargs.get("dispatch"), "__qualname__", "")
             for m in main.app.user_middleware]
    assert "guard" in funcs  # origin.guard and permissions.guard share the name
    dispatches = [m.kwargs.get("dispatch") for m in main.app.user_middleware]
    assert permissions.guard in dispatches
    assert main.origin.guard in dispatches


# --- the socket ---------------------------------------------------------

async def test_ws_requires_session(monkeypatch):
    enable(monkeypatch)
    sent = await client().websocket("/ws")
    assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008
    sent = await client(GUEST_P).websocket("/ws")
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_accepts_a_display(monkeypatch):
    from backend.home.auth import DisplayLinks
    enable(monkeypatch)
    link, _ = DisplayLinks().create("TV")
    sent = await client(Principal("display", link["id"], "TV")).websocket("/ws")
    assert sent[0]["type"] == "websocket.accept"


async def test_ws_open_when_auth_off():
    sent = await client().websocket("/ws")
    assert sent[0]["type"] == "websocket.accept"


async def test_join_and_party_serve_the_page_shell(monkeypatch):
    enable(monkeypatch)
    shell = await client(USER_P).request("GET", "/")
    for path in ("/join?token=t", "/party", "/party/p1"):
        r = await client(GUEST_P).request("GET", path)
        assert (r.status, r.body) == (shell.status, shell.body), path
