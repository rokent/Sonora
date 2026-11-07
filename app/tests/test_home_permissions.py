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
from backend.home.permissions import GUEST, PUBLIC, allowed

from home_client import Client

GUEST_P = Principal("guest", "guest:abc", "Ana")
USER_P = Principal("user", "sub-1", "Tudor")
SERVICE_P = Principal("service", "ha", "Home Assistant")


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
    return re.sub(r"\{[^}]+\}", "x", path)


def _matches(rules, method, path) -> bool:
    return any(m in ("*", method) and rx.fullmatch(path) for m, rx in rules)


# --- the rules ----------------------------------------------------------

def test_every_upstream_write_is_denied_to_guests():
    writes = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or ():
            if method in ("GET", "HEAD"):
                continue
            sample = _sample(path)
            if _matches(GUEST, method, sample) or _matches(PUBLIC, method, sample):
                continue
            writes.append((method, sample))
            assert allowed(GUEST_P, method, sample) is False, (method, path)
    assert len(writes) > 50  # the walk really saw Sonora's routes


def test_guest_allowlist():
    assert allowed(GUEST_P, "POST", "/api/parties/p1/queue")
    assert allowed(GUEST_P, "POST", "/api/parties/p1/join")
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
    for path in ("/api/state", "/ws", "/api/parties", "/api/parties/p1",
                 "/api/favorites", "/api/services", "/api/services/12/browse",
                 "/api/services/12/search", "/api/services/12/letters",
                 "/api/services/12/item", "/api/library", "/api/browse",
                 "/api/browse/letters", "/api/queue/RINCON_1", "/api/themes",
                 "/api/themes/schema", "/join", "/party", "/party/p1"):
        assert allowed(GUEST_P, "GET", path), path
        assert allowed(GUEST_P, "HEAD", path), path
    for path in ("/api/alarms", "/api/scenes", "/api/diagnostics", "/api/log",
                 "/api/guest-pass", "/api/presets", "/api/cloud/status",
                 "/api/zones/x/settings", "/api/queue/x/y", "/license"):
        assert not allowed(GUEST_P, "GET", path), path


def test_public_needs_no_principal():
    for method, path in (("GET", "/"), ("GET", "/join"), ("GET", "/assets/app-1a2b.js"),
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


async def test_ws_open_when_auth_off():
    sent = await client().websocket("/ws")
    assert sent[0]["type"] == "websocket.accept"


async def test_join_and_party_serve_the_page_shell(monkeypatch):
    enable(monkeypatch)
    shell = await client(USER_P).request("GET", "/")
    for path in ("/join?token=t", "/party", "/party/p1"):
        r = await client(GUEST_P).request("GET", path)
        assert (r.status, r.body) == (shell.status, shell.body), path
