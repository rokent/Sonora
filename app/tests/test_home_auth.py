"""Sonora Home sign-in: who is asking, how that is remembered, how a guest joins."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI

from backend.config import settings
from backend.home import auth, auth_routes
from backend.home.auth import LOCAL, GuestPass, Principal, read_session, sign_session


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONORA_OIDC_CLIENT_ID", "sonora")
    monkeypatch.setenv("SONORA_OIDC_CLIENT_SECRET", "s3cret")
    auth_routes._discovery_cache.clear()
    return tmp_path


class Client:
    """A bare ASGI client (httpx is not installed), keeping cookies."""

    def __init__(self, app, scheme="http"):
        self.app, self.scheme, self.cookies = app, scheme, {}

    async def request(self, method, path, body=None, headers=None):
        path, _, query = path.partition("?")
        raw = json.dumps(body).encode() if body is not None else b""
        hdrs = [(b"host", b"sonora.test"), (b"content-type", b"application/json")]
        hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        if self.cookies:
            jar = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            hdrs.append((b"cookie", jar.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": self.scheme, "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": hdrs, "server": ("sonora.test", 80), "client": ("1.2.3.4", 1)}
        sent = [{"type": "http.request", "body": raw, "more_body": False}]
        out = {"status": 0, "headers": [], "body": b""}

        async def receive():
            return sent.pop(0) if sent else {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg["headers"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        await self.app(scope, receive, send)
        for k, v in out["headers"]:
            if k == b"set-cookie":
                name, _, rest = v.decode().partition("=")
                value = rest.split(";", 1)[0]
                if value in ("", '""') or "Max-Age=0" in rest:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value
        return Response(out)


class Response:
    def __init__(self, out):
        self.status = out["status"]
        self.headers = {k.decode(): v.decode() for k, v in out["headers"]}
        self.set_cookies = [v.decode() for k, v in out["headers"] if k == b"set-cookie"]
        self.body = out["body"]

    def json(self):
        return json.loads(self.body)


def make_app():
    app = FastAPI()
    app.include_router(auth_routes.router)
    return app


def enable(monkeypatch):
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")


def test_session_round_trip():
    p = Principal("user", "u1", "Ana")
    value = sign_session(p, 60)
    assert read_session(value) == p
    assert read_session(value[:-2] + ("aa" if value[-2:] != "aa" else "bb")) is None
    assert read_session("garbage") is None
    assert read_session(sign_session(p, -1)) is None


def test_session_key_is_private_and_stable(home):
    sign_session(LOCAL, 60)
    key = home / "home" / "secret.key"
    assert len(key.read_bytes()) == 32
    assert key.stat().st_mode & 0o777 == 0o600
    first = key.read_bytes()
    sign_session(LOCAL, 60)
    assert key.read_bytes() == first


def test_auth_disabled_is_local():
    assert auth.auth_enabled() is False
    assert auth.principal_of(None, None) == LOCAL


def test_principal_of_reads_cookie_and_bearer(monkeypatch):
    enable(monkeypatch)
    assert auth.principal_of(None, None) is None
    p = Principal("user", "u1", "Ana")
    assert auth.principal_of(sign_session(p, 60), None) == p
    assert auth.principal_of("bad", None) is None
    svc = Principal("service", "ha", "Home Assistant")
    monkeypatch.setattr(auth, "_bearers", [lambda t: svc if t == "tok" else None])
    assert auth.principal_of(None, "Bearer tok") == svc
    assert auth.principal_of(None, "Bearer nope") is None
    assert auth.principal_of(None, "Basic tok") is None


def test_guest_pass_rotate_invalidates():
    gp = GuestPass()
    old = gp.token()
    assert gp.check(old) and not gp.check("x")
    new = gp.rotate()
    assert new != old and not gp.check(old) and gp.check(new)
    assert GuestPass().check(new)  # persisted


async def test_guest_join_sets_cookie(monkeypatch):
    enable(monkeypatch)
    c = Client(make_app())
    token = GuestPass().token()
    r = await c.request("POST", "/auth/guest", {"token": token, "name": " Ana "})
    assert r.status == 200 and r.json()["ok"] is True
    cookie = next(s for s in r.set_cookies if s.startswith("sonora_session="))
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    me = (await c.request("GET", "/api/me")).json()
    assert me["auth"] is True
    assert me["principal"]["kind"] == "guest" and me["principal"]["name"] == "Ana"
    assert me["principal"]["id"].startswith("guest:")

    bad = Client(make_app())
    assert (await bad.request("POST", "/auth/guest", {"token": "no", "name": "Ana"})).status == 403
    assert (await c.request("POST", "/auth/guest", {"token": token, "name": "  "})).status == 400


async def test_me_without_session_is_null_principal_when_enabled(monkeypatch):
    enable(monkeypatch)
    r = await Client(make_app()).request("GET", "/api/me")
    assert r.status == 200 and r.json() == {"auth": True, "principal": None}


async def test_me_when_auth_disabled_is_local():
    me = (await Client(make_app()).request("GET", "/api/me")).json()
    assert me == {"auth": False, "principal": {"kind": "user", "id": "local", "name": "Local"}}


async def test_guest_pass_is_for_users_only(monkeypatch):
    enable(monkeypatch)
    c = Client(make_app())
    token = GuestPass().token()
    await c.request("POST", "/auth/guest", {"token": token, "name": "Ana"})
    assert (await c.request("GET", "/api/guest-pass")).status == 403
    assert (await c.request("POST", "/api/guest-pass/rotate")).status == 403

    u = Client(make_app())
    u.cookies["sonora_session"] = sign_session(Principal("user", "u1", "Ana"), 60)
    got = (await u.request("GET", "/api/guest-pass")).json()
    assert got["token"] == token and "token=" + token in got["url"]
    new = (await u.request("POST", "/api/guest-pass/rotate")).json()["token"]
    assert new != token and not GuestPass().check(token)


def patch_oidc(monkeypatch, userinfo):
    async def oidc_json(method, url, **kw):
        return {"authorization_endpoint": "https://idp.example/authorize",
                "token_endpoint": "https://idp.example/token",
                "userinfo_endpoint": "https://idp.example/userinfo"}

    async def exchange(code, verifier, redirect_uri):
        assert code == "abc" and verifier
        return userinfo

    monkeypatch.setattr(auth_routes, "_oidc_json", oidc_json)
    monkeypatch.setattr(auth_routes, "_exchange", exchange)


async def start_login(c, nxt=None, headers=None):
    path = "/auth/login" + (f"?next={nxt}" if nxt else "")
    r = await c.request("GET", path, headers=headers)
    assert r.status == 302
    loc = urlparse(r.headers["location"])
    return loc, {k: v[0] for k, v in parse_qs(loc.query).items()}


async def test_login_redirects_with_pkce(monkeypatch):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {})
    c = Client(make_app())
    loc, q = await start_login(c)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://idp.example/authorize"
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["scope"] == "openid profile email groups" and q["client_id"] == "sonora"
    assert q["redirect_uri"] == "http://sonora.test/auth/callback"
    assert "sonora_oidc" in c.cookies


async def test_forwarded_headers_only_when_trusted(monkeypatch):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {})
    fwd = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "home.example"}
    _, q = await start_login(Client(make_app()), headers=fwd)
    assert q["redirect_uri"] == "http://sonora.test/auth/callback"
    monkeypatch.setenv("SONORA_TRUST_PROXY", "1")
    _, q = await start_login(Client(make_app()), headers=fwd)
    assert q["redirect_uri"] == "https://home.example/auth/callback"


async def test_callback_signs_user_in(monkeypatch):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {"sub": "u-1", "preferred_username": "ana", "name": "Ana P"})
    c = Client(make_app(), scheme="https")
    _, q = await start_login(c, nxt="/rooms?x=1")
    r = await c.request("GET", f"/auth/callback?code=abc&state={q['state']}")
    assert r.status == 302 and r.headers["location"] == "/rooms?x=1"
    cookie = next(s for s in r.set_cookies if s.startswith("sonora_session="))
    assert "Secure" in cookie and "HttpOnly" in cookie
    assert "sonora_oidc" not in c.cookies
    me = (await c.request("GET", "/api/me")).json()["principal"]
    assert me == {"kind": "user", "id": "u-1", "name": "ana"}


async def test_callback_rejects_bad_state(monkeypatch):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {"sub": "u-1"})
    c = Client(make_app())
    await start_login(c)
    assert (await c.request("GET", "/auth/callback?code=abc&state=wrong")).status == 400
    assert (await Client(make_app()).request("GET", "/auth/callback?code=abc&state=x")).status == 400


async def test_callback_rejects_wrong_group(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("SONORA_OIDC_GROUP", "family")
    patch_oidc(monkeypatch, {"sub": "u-1", "preferred_username": "ana", "groups": ["other"]})
    c = Client(make_app())
    _, q = await start_login(c)
    r = await c.request("GET", f"/auth/callback?code=abc&state={q['state']}")
    assert r.status == 403 and "sonora_session" not in c.cookies


async def test_callback_accepts_right_group(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv("SONORA_OIDC_GROUP", "family")
    patch_oidc(monkeypatch, {"sub": "u-1", "name": "Ana", "groups": ["family"]})
    c = Client(make_app())
    _, q = await start_login(c)
    assert (await c.request("GET", f"/auth/callback?code=abc&state={q['state']}")).status == 302
    assert (await c.request("GET", "/api/me")).json()["principal"]["name"] == "Ana"


@pytest.mark.parametrize("nxt", ["https://evil.example", "//evil.example", "/\\evil.example", "evil"])
async def test_next_must_be_relative(monkeypatch, nxt):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {"sub": "u-1", "name": "Ana"})
    c = Client(make_app())
    _, q = await start_login(c, nxt=nxt)
    r = await c.request("GET", f"/auth/callback?code=abc&state={q['state']}")
    assert r.status == 302 and r.headers["location"] == "/"


async def test_logout_clears_session(monkeypatch):
    enable(monkeypatch)
    c = Client(make_app())
    c.cookies["sonora_session"] = sign_session(Principal("user", "u1", "Ana"), 60)
    assert (await c.request("POST", "/auth/logout")).status == 200
    assert "sonora_session" not in c.cookies


async def test_discovery_is_cached(monkeypatch):
    enable(monkeypatch)
    calls = []

    async def oidc_json(method, url, **kw):
        calls.append(url)
        return {"authorization_endpoint": "a", "token_endpoint": "t", "userinfo_endpoint": "u"}

    monkeypatch.setattr(auth_routes, "_oidc_json", oidc_json)
    await auth_routes._discover()
    await auth_routes._discover()
    assert calls == ["https://idp.example/.well-known/openid-configuration"]


def test_oidc_cookie_is_not_a_session():
    oidc = auth.sign_payload({"k": "user", "id": "u1", "n": "Ana"}, 60, "oidc")
    assert read_session(oidc) is None
    session = sign_session(Principal("user", "u1", "Ana"), 60)
    assert auth.read_payload(session, "oidc") is None


async def test_callback_rejects_a_session_as_login_state(monkeypatch):
    enable(monkeypatch)
    patch_oidc(monkeypatch, {"sub": "u-1"})
    c = Client(make_app())
    c.cookies["sonora_oidc"] = auth.sign_payload({"s": "x", "v": "v", "next": "/"}, 60, "session")
    assert (await c.request("GET", "/auth/callback?code=abc&state=x")).status == 400


async def test_rotating_the_pass_ends_guest_sessions(monkeypatch):
    enable(monkeypatch)
    c = Client(make_app())
    await c.request("POST", "/auth/guest", {"token": GuestPass().token(), "name": "Ana"})
    assert (await c.request("GET", "/api/me")).json()["principal"]["kind"] == "guest"
    GuestPass().rotate()
    assert (await c.request("GET", "/api/me")).json()["principal"] is None
    # Users are unaffected by rotation.
    u = sign_session(Principal("user", "u1", "Ana"), 60)
    GuestPass().rotate()
    assert read_session(u) == Principal("user", "u1", "Ana")


def test_loose_key_permissions_are_tightened(home):
    sign_session(LOCAL, 60)
    key = home / "home" / "secret.key"
    key.chmod(0o644)
    auth._key_cache.clear()
    sign_session(LOCAL, 60)
    assert key.stat().st_mode & 0o777 == 0o600


def test_key_is_cached_in_memory(home):
    sign_session(LOCAL, 60)
    key = home / "home" / "secret.key"
    first = key.read_bytes()
    key.write_bytes(b"x" * 32)  # a swap on disk is not picked up mid-run
    value = sign_session(LOCAL, 60)
    key.write_bytes(first)
    assert read_session(value) == LOCAL


def test_write_json_is_private_and_leaves_no_temp(tmp_path):
    from backend.home.store import read_json, write_json
    path = tmp_path / "a.json"
    write_json(path, {"x": 1})
    write_json(path, {"x": 2})
    assert read_json(path, None) == {"x": 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]
