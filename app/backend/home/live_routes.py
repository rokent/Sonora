"""Live sound over HTTP: the stream speakers fetch, and the hub in use.

``GET /api/home/live/<key>.mp3`` is public (a speaker carries no session);
the key is unguessable and stops working when its session ends. The hub is
made on first use from the controller ``main.py`` started and is hooked to
the party engine, so a party that ends takes its live sound with it.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import media, routes
from .live import LiveHub, Target

router = APIRouter()

_KEY = re.compile(r"[A-Za-z0-9_-]{16,64}")
_HEADERS = {"Cache-Control": "no-cache, no-store", "Connection": "close"}

#: ``(controller, hub)``, and the engine the hub is hooked to.
_held: dict[str, Any] = {}


def _resolve(target: Target) -> str | None:
    kind, ident = target
    controller = routes._main().state.get("controller")
    if kind == "party":
        try:
            party = routes.engine().get(ident)
        except routes.Refused:
            return None
        return party["speakers"][0] if party and party["speakers"] else None
    try:
        controller.zone(ident)
    except (KeyError, AttributeError):
        return None
    return ident


def hub() -> LiveHub:
    """The live hub for the running controller (503 ``Refused`` before the
    controller is up)."""
    main = routes._main()
    controller, commands = main.state.get("controller"), main.state.get("commands")
    if controller is None or commands is None:
        raise routes.Refused(503, "not_ready")
    held = _held.get("hub")
    if held is None or held[0] is not controller:
        held = _held["hub"] = (controller, LiveHub(controller=controller, commands=commands,
                                                   resolve=_resolve, base_url=media.speaker_base))
    found = held[1]
    try:
        engine = routes.engine()
    except routes.Refused:
        engine = None
    if engine is not None and _held.get("engine") != (engine, found):
        engine.on_end(found.end_for_party)
        _held["engine"] = (engine, found)
    return found


def reset() -> None:
    """Forget the hub in use (tests)."""
    _held.clear()


@router.api_route("/api/home/live/{key}.mp3", methods=["GET", "HEAD"],
                  include_in_schema=False)
async def live_stream(key: str, request: Request) -> Response:
    """A live session's MP3 stream for a speaker (no session needed)."""
    try:
        found = hub().session(key) if _KEY.fullmatch(key) else None
    except routes.Refused:
        found = None
    if found is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if request.method == "HEAD":
        return Response(status_code=200, media_type="audio/mpeg", headers=_HEADERS)
    return StreamingResponse(found.listen(), media_type="audio/mpeg", headers=_HEADERS)
