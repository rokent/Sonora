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

def _key() -> bytes:
    path = home_dir() / "secret.key"
    try:
        key = path.read_bytes()
        if len(key) == 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_payload(payload: dict[str, Any], max_age: int) -> str:
    body = _b64(json.dumps({**payload, "exp": time.time() + max_age}).encode())
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def read_payload(value: str) -> dict[str, Any] | None:
    """The payload of a value from ``sign_payload``, or None if it was
    altered, is malformed or has expired."""
    try:
        body, _, sig = value.partition(".")
        want = hmac.new(_key(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(sig)):
            return None
        data = json.loads(_unb64(body))
        if not isinstance(data, dict) or float(data["exp"]) < time.time():
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None


def sign_session(p: Principal, max_age: int) -> str:
    return sign_payload({"k": p.kind, "id": p.id, "n": p.name}, max_age)


def read_session(value: str) -> Principal | None:
    data = read_payload(value)
    if not data or data.get("k") not in ("user", "guest", "service"):
        return None
    if not isinstance(data.get("id"), str) or not isinstance(data.get("n"), str):
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
