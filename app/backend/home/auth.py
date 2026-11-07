"""Who is asking: principals, signed sessions and the guest pass.

A principal is a signed-in household member (``user``), someone let in with
the guest pass (``guest``), a machine such as Home Assistant (``service``), or
a TV screen let in with a display link (``display``, read-only).
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
    kind: Literal["user", "guest", "service", "display"]
    id: str
    name: str

    @property
    def is_user(self) -> bool:
        return self.kind in ("user", "service")

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"

    @property
    def is_display(self) -> bool:
        return self.kind == "display"


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
    elif p.is_display:
        # Tied to the display link's token: deleting the link ends it.
        payload["g"] = DisplayLinks().generation(p.id)
    return sign_payload(payload, max_age, "session")


def read_session(value: str) -> Principal | None:
    data = read_payload(value, "session")
    if not data or data.get("k") not in ("user", "guest", "service", "display"):
        return None
    if not isinstance(data.get("id"), str) or not isinstance(data.get("n"), str):
        return None
    if data["k"] == "guest" and data.get("g") != _generation(GuestPass().token()):
        return None
    if data["k"] == "display":
        link = DisplayLinks()._raw(data["id"])
        if link is None or data.get("g") != _generation(link["token_hash"]):
            return None
        # The link's current name, so a rename shows without a new session.
        return Principal("display", data["id"], str(link.get("name", "")))
    return Principal(data["k"], data["id"], data["n"])


def display_session_removed(value: str | None) -> bool:
    """Whether ``value`` is a genuine display session whose link was deleted
    (or replaced), so the TV can say so instead of asking it to sign in."""
    data = read_payload(value or "", "session")
    return bool(data) and data.get("k") == "display" and read_session(value or "") is None


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


# --- display links -----------------------------------------------------

DISPLAY_MODES = ("video", "visualizer", "cover")
_DISPLAY_FIELDS = ("id", "name", "party", "mode", "lyrics", "created")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DisplayLinks:
    """Long-lived links that let a TV screen in as a read-only ``display``.

    Kept in ``displays.json`` as ``{id, name, token_hash, party, mode, lyrics,
    created}``. Only a hash of each token is stored; the token itself is shown
    once, when the link is made. Every method returns the public fields only.
    """

    def _path(self):
        return home_dir() / "displays.json"

    def _load(self) -> list[dict]:
        found = read_json(self._path(), [])
        return [d for d in found if isinstance(d, dict)] if isinstance(found, list) else []

    def _save(self, links: list[dict]) -> None:
        write_json(self._path(), links)

    @staticmethod
    def _public(link: dict) -> dict:
        return {k: link.get(k) for k in _DISPLAY_FIELDS}

    @staticmethod
    def _check(fields: dict[str, Any]) -> dict[str, Any]:
        """``fields`` cleaned, or ValueError naming the first bad one."""
        out: dict[str, Any] = {}
        if "name" in fields:
            name = fields["name"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name")
            out["name"] = name.strip()[:40].strip()
        if "party" in fields:
            party = fields["party"]
            if party is not None and (not isinstance(party, str) or not party
                                      or len(party) > 64):
                raise ValueError("party")
            out["party"] = party
        if "mode" in fields:
            if fields["mode"] not in DISPLAY_MODES:
                raise ValueError("mode")
            out["mode"] = fields["mode"]
        if "lyrics" in fields:
            if not isinstance(fields["lyrics"], bool):
                raise ValueError("lyrics")
            out["lyrics"] = fields["lyrics"]
        return out

    def list(self) -> list[dict]:
        return [self._public(d) for d in self._load()]

    def _raw(self, link_id: str) -> dict | None:
        for d in self._load():
            if d.get("id") == link_id and isinstance(d.get("token_hash"), str):
                return d
        return None

    def get(self, link_id: str) -> dict | None:
        found = self._raw(link_id)
        return self._public(found) if found else None

    def create(self, name: str, party: str | None = None, mode: str = "visualizer",
               lyrics: bool = False) -> tuple[dict, str]:
        """Make a link; returns ``(public fields, token)``. The token is not
        kept, so this is the only time it can be shown."""
        fields = self._check({"name": name, "party": party, "mode": mode, "lyrics": lyrics})
        token = secrets.token_urlsafe(32)
        link = {"id": secrets.token_urlsafe(9), **fields,
                "token_hash": _token_hash(token), "created": time.time()}
        self._save([*self._load(), link])
        return self._public(link), token

    def update(self, link_id: str, **fields: Any) -> dict | None:
        """Change name/party/mode/lyrics; None if there is no such link.
        The token stays, so the TV's session survives an edit."""
        clean = self._check({k: v for k, v in fields.items()
                             if k in ("name", "party", "mode", "lyrics")})
        links = self._load()
        for d in links:
            if d.get("id") == link_id:
                d.update(clean)
                self._save(links)
                return self._public(d)
        return None

    def delete(self, link_id: str) -> bool:
        links = self._load()
        kept = [d for d in links if d.get("id") != link_id]
        if len(kept) == len(links):
            return False
        self._save(kept)
        return True

    def by_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        want = _token_hash(token)
        for d in self._load():
            if hmac.compare_digest(str(d.get("token_hash", "")), want):
                return self._public(d)
        return None

    def generation(self, link_id: str) -> str | None:
        """What a display session for ``link_id`` is tied to: a short hash of
        the link's token hash. None when the link is gone."""
        found = self._raw(link_id)
        return _generation(found["token_hash"]) if found else None
