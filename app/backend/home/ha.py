"""Home Assistant's door: long-lived bearer tokens and a small ``/api/ha/`` API.

A token is shown once when made and kept only as its sha256. Whoever holds one
is the ``service`` principal ``ha:<id>``, which the permission guard lets reach
``/api/ha/`` and nothing else. Because that prefix is open to the service, the
token routes themselves insist on a household member.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import routes
from .auth import Principal, register_bearer, request_principal
from .auth_routes import _origin
from .routes import Refused, _answer, _engine_call, _start, changed, engine, presets
from .store import home_dir, read_json, write_json

router = APIRouter()
_lock = threading.Lock()


def _path():
    return home_dir() / "ha_tokens.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> list[dict]:
    found = read_json(_path(), {}).get("tokens")
    return [t for t in found if isinstance(t, dict)] if isinstance(found, list) else []


def _check(token: str) -> Principal | None:
    digest = _hash(token)
    for t in _load():
        if hmac.compare_digest(str(t.get("hash", "")), digest):
            return Principal("service", f"ha:{t['id']}", "Home Assistant")
    return None


register_bearer(_check)


def _users_only(request: Request) -> None:
    """The token routes sit under ``/api/ha/``, which a service may reach."""
    if request_principal(request).kind != "user":
        raise Refused(403, "forbidden")


class TokenBody(BaseModel):
    label: str = Field(default="Home Assistant", max_length=80)


@router.post("/api/ha/tokens")
async def create_token(request: Request, body: TokenBody | None = None):
    try:
        _users_only(request)
    except Refused as exc:
        return _answer(exc)
    token = secrets.token_urlsafe(32)
    entry = {"id": secrets.token_hex(6), "label": (body.label.strip() if body else "") or "Home Assistant",
             "hash": _hash(token), "created": time.time()}
    with _lock:
        write_json(_path(), {"tokens": [*_load(), entry]})
    return {"id": entry["id"], "token": token}


@router.get("/api/ha/tokens")
async def list_tokens(request: Request):
    try:
        _users_only(request)
    except Refused as exc:
        return _answer(exc)
    return {"tokens": [{"id": t["id"], "label": t.get("label", ""), "created": t.get("created")}
                       for t in _load()]}


@router.delete("/api/ha/tokens/{token_id}")
async def delete_token(request: Request, token_id: str):
    try:
        _users_only(request)
    except Refused as exc:
        return _answer(exc)
    with _lock:
        tokens = _load()
        kept = [t for t in tokens if t.get("id") != token_id]
        if len(kept) == len(tokens):
            return _answer(Refused(404, "not_found", message=token_id))
        write_json(_path(), {"tokens": kept})
    return {"ok": True}


def _track(party: dict, base: str) -> dict:
    empty = {"title": "", "artist": "", "album": "", "art": ""}
    if not party["speakers"]:
        return empty
    try:
        transport = routes._main().state["controller"].coordinator_of(party["speakers"][0]).transport
    except (KeyError, AttributeError):
        return empty
    art = getattr(transport, "album_art_uri", "") or ""
    if art:
        art = f"{base}/api/art?u={quote(art, safe='')}"
    return {"title": getattr(transport, "title", "") or "", "artist": getattr(transport, "artist", "") or "",
            "album": getattr(transport, "album", "") or "", "art": art}


@router.get("/api/ha/state")
async def ha_state(request: Request):
    scheme, host = _origin(request)
    base = f"{scheme}://{host}"
    try:
        parties = engine().list()
    except Refused as exc:
        return _answer(exc)
    return {"presets": [{"id": p["id"], "name": p["name"]} for p in presets().list()],
            "parties": [{"id": p["id"], "name": p["name"],
                         "state": "playing" if p["speakers"] else "ended",
                         "speakers": [routes._room(z) for z in p["speakers"]],
                         "track": _track(p, base)} for p in parties if p["speakers"]]}


@router.post("/api/ha/presets/{preset_id}/start")
async def ha_start(request: Request, preset_id: str):
    who = request_principal(request)
    try:
        preset = presets().get(preset_id)
        if preset is None:
            raise Refused(404, "not_found", message=preset_id)
        return await _start(who, name=None, speakers=None, preset=preset, take=True)
    except Refused as exc:
        return _answer(exc)


@router.post("/api/ha/parties/{party_id}/end")
async def ha_end(request: Request, party_id: str):
    request_principal(request)
    try:
        result = await _engine_call(engine().end(party_id))
    except Refused as exc:
        return _answer(exc)
    routes._members.pop(party_id, None)
    await changed()
    return result


@router.post("/api/ha/parties/{party_id}/tv/{tv_id}")
async def ha_tv(request: Request, party_id: str, tv_id: str):
    request_principal(request)
    return JSONResponse({"error": "phase3"}, status_code=501)
