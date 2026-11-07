"""The household's Samsung TVs over HTTP (users only: guests are refused by
the default-deny guard, and a service reaches only ``/api/ha/``)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import tvs
from .routes import Refused, _answer, engine

router = APIRouter()


class ShowBody(BaseModel):
    party_id: str
    mode: str | None = None


def _error(exc: tvs.TvError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status)


def _invalid(exc: ValueError) -> JSONResponse:
    return _answer(Refused(400, "invalid", message=str(exc)))


async def show_party(tv_id: str, party_id: str, mode: str | None) -> dict:
    """Show a known party on a TV; raises ``Refused`` or ``tvs.TvError``."""
    if engine().get(party_id) is None:
        raise Refused(404, "not_found", message=party_id)
    return await tvs.show(tv_id, party_id, mode)


@router.get("/api/tvs")
async def list_tvs():
    return {"tvs": [tvs.public(t) for t in tvs.store().list()]}


@router.post("/api/tvs")
async def add_tv(body: dict):
    try:
        return tvs.public(tvs.store().add(body))
    except ValueError as exc:
        return _invalid(exc)


@router.put("/api/tvs/{tv_id}")
async def update_tv(tv_id: str, body: dict):
    try:
        tv = tvs.store().update(tv_id, body)
    except ValueError as exc:
        return _invalid(exc)
    if tv is None:
        return _answer(Refused(404, "not_found", message=tv_id))
    return tvs.public(tv)


@router.delete("/api/tvs/{tv_id}")
async def delete_tv(tv_id: str):
    tv = tvs.store().get(tv_id)
    if not tvs.store().remove(tv_id):
        return _answer(Refused(404, "not_found", message=tv_id))
    if tv and tv.get("display_id"):                    # its display link goes with it
        from .auth import DisplayLinks
        DisplayLinks().delete(tv["display_id"])
    return {"ok": True}


@router.post("/api/tvs/{tv_id}/pair")
async def pair_tv(tv_id: str):
    try:
        return await tvs.pair(tv_id)
    except tvs.TvError as exc:
        return _error(exc)


@router.post("/api/tvs/{tv_id}/show")
async def show_on_tv(tv_id: str, body: ShowBody):
    try:
        return await show_party(tv_id, body.party_id, body.mode)
    except Refused as exc:
        return _answer(exc)
    except tvs.TvError as exc:
        return _error(exc)
