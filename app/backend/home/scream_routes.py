"""Scream sources and where each one plays (users only: the guard keeps
guests, displays and anonymous callers out of anything not whitelisted)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import scream

router = APIRouter()


def _bad(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


@router.get("/api/scream/sources")
async def sources() -> list[dict]:
    return scream.receiver().listing()


@router.put("/api/scream/sources/{ip}")
async def map_source(ip: str, request: Request):
    clean = scream.valid_ip(ip)
    if clean is None:
        return _bad("invalid_ip")
    try:
        body = await request.json()
    except ValueError:
        return _bad("invalid_body")
    if not isinstance(body, dict) or "target" not in body:
        return _bad("invalid_body")
    target = body["target"]
    if target is not None:
        target = scream.valid_target(target)
        if target is None:
            return _bad("invalid_target")
    scream.receiver().set_target(clean, target)
    return {"ip": clean, "target": target}
