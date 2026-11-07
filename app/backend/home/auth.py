"""Who is asking: principals, signed sessions and the guest pass.

A principal is a signed-in household member (``user``), someone let in with
the guest pass (``guest``), or a machine such as Home Assistant (``service``).
Sessions are stateless: ``base64url(json).hmac_sha256`` under a key kept in
the data folder, so a restart signs nobody out. With no OIDC issuer set,
Sonora behaves as it always did and every request is the local user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from fastapi import HTTPException, Request

from .store import home_dir, read_json, write_json

SESSION_COOKIE = "sonora_session"


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "guest", "service"]
    id: str
    name: str

    @property
    def is_user(self) -> bool:
        return self.kind in ("user", "service")

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"


LOCAL = Principal("user", "local", "Local")

_bearers: list[Callable[[str], Principal | None]] = []


def register_bearer(check: Callable[[str], Principal | None]) -> None:
    """Add a checker for ``Authorization: Bearer`` tokens (Home Assistant's)."""
    _bearers.append(check)


def auth_enabled() -> bool:
    return bool(os.environ.get("SONORA_OIDC_ISSUER", "").strip())


# --- signing -----------------------------------------------------------

_key_cache: dict[str, bytes] = {}


def _key() -> bytes:
    """The signing key, made on first use. Created through a private temp file
    and a link, so two starters never see a half-written or differing key."""
    path = home_dir() / "secret.key"
    cached = _key_cache.get(str(path))
    if cached is not None:
        return cached
    key = None
    try:
        found = path.read_bytes()
        if len(found) == 32:
            key = found
    except OSError:
        pass
    if key is None:
        key = secrets.token_bytes(32)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="secret.key.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                # Another start won; use its key (or replace a damaged one).
                found = path.read_bytes()
                if len(found) == 32:
                    key = found
                else:
                    os.replace(tmp, path)
                    tmp = ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)
    _key_cache[str(path)] = key
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_payload(payload: dict[str, Any], max_age: int, typ: str) -> str:
    """Sign ``payload`` for one purpose (``typ``), so a value made for one use
    (the login-state cookie) is never accepted for another (a session)."""
    body = _b64(json.dumps({**payload, "typ": typ, "exp": time.time() + max_age}).encode())
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def read_payload(value: str, typ: str) -> dict[str, Any] | None:
    """The payload of a value from ``sign_payload`` made for ``typ``, or None
    if it was altered, is malformed, is for another purpose or has expired."""
    try:
        body, _, sig = value.partition(".")
        want = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(sig)):
            return None
        data = json.loads(_unb64(body))
        if (not isinstance(data, dict) or data.get("typ") != typ
                or float(data["exp"]) < time.time()):
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None


def _generation(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def sign_session(p: Principal, max_age: int) -> str:
    payload: dict[str, Any] = {"k": p.kind, "id": p.id, "n": p.name}
    if p.is_guest:
        # Tied to the guest pass in force now: rotating it ends this session.
        payload["g"] = _generation(GuestPass().token())
    return sign_payload(payload, max_age, "session")


def read_session(value: str) -> Principal | None:
    data = read_payload(value, "session")
    if not data or data.get("k") not in ("user", "guest", "service"):
        return None
    if not isinstance(data.get("id"), str) or not isinstance(data.get("n"), str):
        return None
    if data["k"] == "guest" and data.get("g") != _generation(GuestPass().token()):
        return None
    return Principal(data["k"], data["id"], data["n"])


# --- who is asking -----------------------------------------------------

def principal_of(session_cookie: str | None, authorization: str | None) -> Principal | None:
    """The principal for a request's ``sonora_session`` cookie value and
    ``Authorization`` header; None when there is none to be had."""
    if not auth_enabled():
        return LOCAL
    if authorization and authorization[:7].lower() == "bearer ":
        token = authorization[7:].strip()
        for check in _bearers:
            found = check(token)
            if found:
                return found
        return None
    if session_cookie:
        return read_session(session_cookie)
    return None


def request_principal(request: Request) -> Principal:
    """The principal the middleware put on the request; 401 if none."""
    found = getattr(request.state, "principal", None)
    if found is None:
        raise HTTPException(401, "Sign in required")
    return found


# --- guest pass --------------------------------------------------------

class GuestPass:
    """The one secret that lets a visitor in; rotating it locks out anyone
    holding the old link. Kept in ``guest.json``."""

    def _path(self):
        return home_dir() / "guest.json"

    def token(self) -> str:
        found = read_json(self._path(), {}).get("token")
        return found if isinstance(found, str) and found else self.rotate()

    def rotate(self) -> str:
        token = secrets.token_urlsafe(24)
        write_json(self._path(), {"token": token})
        return token

    def check(self, t: str) -> bool:
        return isinstance(t, str) and hmac.compare_digest(t.encode(), self.token().encode())
