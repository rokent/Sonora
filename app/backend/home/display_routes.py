"""Display links over HTTP: a household member makes, edits and deletes the
links that let TV screens in; a TV asks ``/api/displays/me`` what to show.

The token of a new link is in the URL ``POST /api/displays`` answers with and
nowhere else: only its hash is kept, and no list or read returns it.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request

from .auth import DisplayLinks
from .auth_routes import _origin, _require_user

router = APIRouter()

#: What a TV is told about itself.
_ME_FIELDS = ("id", "name", "party", "mode", "lyrics")


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


def _fields(body: dict) -> dict:
    return {k: body[k] for k in ("name", "party", "mode", "lyrics") if k in body}


@router.get("/api/displays")
async def list_displays(request: Request):
    _require_user(request)
    return {"displays": DisplayLinks().list()}


@router.post("/api/displays")
async def create_display(request: Request):
    _require_user(request)
    body = await _body(request)
    try:
        link, token = DisplayLinks().create(**{"name": "", **_fields(body)})
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {exc}") from None
    scheme, host = _origin(request)
    return {"id": link["id"],
            "url": f"{scheme}://{host}/auth/display?{urlencode({'token': token})}"}


@router.put("/api/displays/{display_id}")
async def update_display(request: Request, display_id: str):
    _require_user(request)
    body = await _body(request)
    try:
        link = DisplayLinks().update(display_id, **_fields(body))
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {exc}") from None
    if link is None:
        raise HTTPException(404, "No such display")
    return link


@router.delete("/api/displays/{display_id}")
async def delete_display(request: Request, display_id: str):
    _require_user(request)
    if not DisplayLinks().delete(display_id):
        raise HTTPException(404, "No such display")
    return {"ok": True}


@router.get("/api/displays/me")
async def display_me(request: Request, display: str = ""):
    """A display gets its own settings. A household member (with sign-in off,
    every request) names the link instead: ``?display=<id>``, as ``/tv`` has it."""
    who = getattr(request.state, "principal", None)
    if who is None:
        raise HTTPException(401, "Sign in required")
    if who.is_display:
        link_id = who.id
    elif who.kind == "user":
        link_id = display
    else:
        raise HTTPException(403, "Displays only")
    link = DisplayLinks().get(link_id) if link_id else None
    if link is None:
        raise HTTPException(404, "No such display")
    return {k: link[k] for k in _ME_FIELDS}
