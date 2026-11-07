"""Display links: long-lived links that let a TV screen in as a read-only
``display`` principal, and end its session when the link is deleted."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from backend import main
from backend.config import settings
from backend.home.auth import (
    SESSION_COOKIE, DisplayLinks, Principal, read_session, sign_session,
)
from backend.home.permissions import DISPLAY, allowed

from home_client import Client

USER_P = Principal("user", "sub-1", "Tudor")
GUEST_P = Principal("guest", "guest:abc", "Ana")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    return tmp_path


def client(principal: Principal | None = None) -> Client:
    c = Client(main.app, host="sonora.local")
    if principal is not None:
        c.cookies[SESSION_COOKIE] = sign_session(principal, 3600)
    return c


async def _create(body=None) -> dict:
    r = await client(USER_P).request("POST", "/api/displays",
                                     body=body or {"name": "Living Room TV"})
    assert r.status == 200, r.body
    return r.json()


async def _open(url: str) -> tuple[Client, object]:
    tv = client()
    u = urlparse(url)
    r = await tv.request("GET", f"{u.path}?{u.query}")
    return tv, r


# --- the store ------------------------------------------------------------

def test_store_create_hashes_the_token_and_lists_without_it(home):
    link, token = DisplayLinks().create("Kitchen", party="p1", mode="cover", lyrics=True)
    assert set(link) == {"id", "name", "party", "mode", "lyrics", "created"}
    assert link["name"] == "Kitchen" and link["party"] == "p1"
    assert link["mode"] == "cover" and link["lyrics"] is True
    raw = (home / "home" / "displays.json").read_text()
    assert token not in raw and "token_hash" in raw
    assert DisplayLinks().list() == [link]
    assert DisplayLinks().by_token(token) == link
    assert DisplayLinks().by_token("nope") is None


def test_store_validates(home):
    links = DisplayLinks()
    for kwargs in ({"name": ""}, {"name": "x", "mode": "disco"},
                   {"name": "x", "lyrics": "yes"}, {"name": "x", "party": 3}):
        with pytest.raises(ValueError):
            links.create(**kwargs)
    link, _ = links.create("  TV  ")
    assert link["name"] == "TV" and link["mode"] == "visualizer"
    assert link["party"] is None and link["lyrics"] is False


def test_display_session_ends_when_link_is_deleted(home):
    link, _ = DisplayLinks().create("TV")
    p = Principal("display", link["id"], "TV")
    value = sign_session(p, 3600)
    assert read_session(value) == p
    assert DisplayLinks().delete(link["id"]) is True
    assert read_session(value) is None


def test_display_session_for_unknown_link_is_refused(home):
    assert read_session(sign_session(Principal("display", "nope", "TV"), 3600)) is None


def test_recreated_link_with_same_id_does_not_revive_old_session(home):
    """The session is tied to the token, not only the id."""
    links = DisplayLinks()
    link, _ = links.create("TV")
    value = sign_session(Principal("display", link["id"], "TV"), 3600)
    data = links._load()
    data[0]["token_hash"] = "0" * 64
    links._save(data)
    assert read_session(value) is None


# --- the permission rules -----------------------------------------------

DISPLAY_P = Principal("display", "d1", "TV")


def test_display_allowlist():
    for path in ("/api/state", "/ws", "/ws/visual", "/api/parties", "/api/parties/p1/queue",
                 "/api/home/config", "/api/art", "/api/displays/me", "/tv"):
        assert allowed(DISPLAY_P, "GET", path), path
    for method, path in (("POST", "/api/parties"), ("POST", "/api/parties/p1/queue"),
                         ("POST", "/api/parties/p1/join"), ("POST", "/api/parties/p1/volume"),
                         ("POST", "/api/parties/p1/transport/play"),
                         ("GET", "/api/displays"), ("POST", "/api/displays"),
                         ("DELETE", "/api/displays/d1"), ("GET", "/api/favorites"),
                         ("GET", "/api/guest-pass"), ("GET", "/party"),
                         ("GET", "/api/ha/status"), ("POST", "/api/zones/x/group")):
        assert not allowed(DISPLAY_P, method, path), (method, path)
    assert DISPLAY  # an explicit list, not a branch


def test_tv_and_auth_display_are_public():
    assert allowed(None, "GET", "/tv")
    assert allowed(None, "GET", "/auth/display")
    assert not allowed(None, "GET", "/api/displays/me")


# --- the whole flow, on the real app --------------------------------------

async def test_create_open_link_then_me():
    made = await _create({"name": "Living Room TV", "party": "p1", "mode": "video",
                          "lyrics": True})
    assert set(made) == {"id", "url"}
    u = urlparse(made["url"])
    assert (u.scheme, u.netloc, u.path) == ("http", "sonora.local", "/auth/display")
    token = parse_qs(u.query)["token"][0]
    assert len(token) >= 32

    tv, r = await _open(made["url"])
    assert r.status == 302
    loc = urlparse(r.headers["location"])
    assert loc.path == "/tv" and parse_qs(loc.query) == {"display": [made["id"]]}
    cookie = r.set_cookies[0]
    assert "Max-Age=31536000" in cookie and "HttpOnly" in cookie
    assert "SameSite=lax" in cookie

    r = await tv.request("GET", "/api/displays/me")
    assert r.status == 200
    assert r.json() == {"id": made["id"], "name": "Living Room TV", "party": "p1",
                        "mode": "video", "lyrics": True}
    r = await tv.request("GET", "/api/me")
    assert r.json()["principal"] == {"kind": "display", "id": made["id"],
                                     "name": "Living Room TV"}


async def test_display_is_read_only():
    made = await _create()
    tv, _ = await _open(made["url"])
    r = await tv.request("POST", "/api/parties", body={"name": "x"})
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    r = await tv.request("GET", "/api/displays")
    assert r.status == 403
    r = await tv.request("PUT", f"/api/displays/{made['id']}", body={"mode": "cover"})
    assert r.status == 403
    r = await tv.request("GET", "/api/state")
    assert r.status not in (401, 403)
    r = await tv.request("GET", "/api/home/config")
    assert r.status == 200 and r.json()["remote_browser"] is None


async def test_display_socket_is_accepted():
    made = await _create()
    tv, _ = await _open(made["url"])
    sent = await tv.websocket("/ws")
    assert sent[0]["type"] == "websocket.accept"


async def test_deleted_link_is_401_and_says_so():
    made = await _create()
    tv, _ = await _open(made["url"])
    r = await client(USER_P).request("DELETE", f"/api/displays/{made['id']}")
    assert r.status == 200 and r.json() == {"ok": True}
    r = await tv.request("GET", "/api/displays/me")
    assert r.status == 401 and r.json() == {"error": "display_removed"}
    r = await tv.request("GET", "/api/state")
    assert r.status == 401 and r.json() == {"error": "display_removed"}
    sent = await tv.websocket("/ws")
    assert sent[0]["type"] == "websocket.close"
    # the link itself no longer lets anyone in
    tv2, r = await _open(made["url"])
    assert r.status == 302
    loc = urlparse(r.headers["location"])
    assert loc.path == "/tv" and parse_qs(loc.query) == {"error": ["removed"]}
    assert SESSION_COOKIE not in tv2.cookies


async def test_no_cookie_is_plain_login_401():
    r = await client().request("GET", "/api/displays/me")
    assert r.status == 401 and r.json() == {"error": "login"}


async def test_list_never_returns_the_token():
    made = await _create()
    token = parse_qs(urlparse(made["url"]).query)["token"][0]
    r = await client(USER_P).request("GET", "/api/displays")
    assert r.status == 200
    assert token not in r.body.decode() and "token_hash" not in r.body.decode()
    [link] = r.json()["displays"]
    assert link["id"] == made["id"] and link["name"] == "Living Room TV"
    assert set(link) == {"id", "name", "party", "mode", "lyrics", "created"}


async def test_update_and_validation():
    made = await _create()
    user = client(USER_P)
    r = await user.request("PUT", f"/api/displays/{made['id']}",
                           body={"name": "Bedroom", "mode": "cover", "lyrics": True,
                                 "party": None})
    assert r.status == 200
    assert {k: r.json()[k] for k in ("name", "mode", "lyrics", "party")} == {
        "name": "Bedroom", "mode": "cover", "lyrics": True, "party": None}
    r = await user.request("PUT", f"/api/displays/{made['id']}", body={"mode": "disco"})
    assert r.status == 400
    r = await user.request("PUT", "/api/displays/nope", body={"mode": "cover"})
    assert r.status == 404
    r = await user.request("DELETE", "/api/displays/nope")
    assert r.status == 404
    r = await user.request("POST", "/api/displays", body={"name": ""})
    assert r.status == 400
    r = await user.request("POST", "/api/displays", body=["x"])
    assert r.status == 400
    # the session survives an edit: settings change, the token does not
    tv, _ = await _open(made["url"])
    await user.request("PUT", f"/api/displays/{made['id']}", body={"mode": "video"})
    r = await tv.request("GET", "/api/displays/me")
    assert r.status == 200 and r.json()["mode"] == "video"


async def test_guests_cannot_manage_displays():
    g = client(GUEST_P)
    for method, path in (("GET", "/api/displays"), ("POST", "/api/displays"),
                         ("GET", "/api/displays/me")):
        r = await g.request(method, path, body={"name": "x"} if method == "POST" else None)
        assert r.status == 403, (method, path)


async def test_tv_serves_the_page_shell():
    shell = await client(USER_P).request("GET", "/")
    r = await client().request("GET", "/tv?display=d1")
    assert (r.status, r.body) == (shell.status, shell.body)


async def test_me_for_a_user_reads_the_display_named_in_the_query(monkeypatch):
    """With sign-in off every request is the local user, so a TV opening
    /tv?display=<id> asks for that link's settings by id."""
    made = await _create()
    monkeypatch.delenv("SONORA_OIDC_ISSUER")
    r = await client().request("GET", f"/api/displays/me?display={made['id']}")
    assert r.status == 200 and r.json()["id"] == made["id"]
    r = await client().request("GET", "/api/displays/me")
    assert r.status == 404
    r = await client().request("GET", "/api/displays/me?display=nope")
    assert r.status == 404
