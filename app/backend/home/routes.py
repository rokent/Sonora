"""Parties and presets over HTTP, with live updates and queue attribution.

The party engine and the preset store are made on first use, from the
controller ``main.py`` started, and the engine is wired to the controller's
messages then, so a party follows what the speakers report. Every change is
announced to every open page as ``{"type": "parties"}``; the engine's own
notices (a speaker left a party) go out as they are, followed by the same.

A party's queue is the Sonos queue of its lead. Who added what is kept by the
engine per track URI, oldest adder first. Adds go to the end of the queue, so
a URI's adders are matched to its last copies, oldest adder to the earliest
of those; copies before them (there before the party) have no adder.

``main.py`` is imported inside functions only: it imports this package.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import media, youtube
from .auth import Principal, request_principal
from .parties import MixedHousehold, NoSpeakers, PartyEngine, SpeakerTaken
from .presets import Presets
from .store import home_dir

log = logging.getLogger(__name__)

router = APIRouter()

#: The engine and store in use, and what they were made from: a new
#: controller (a restart, a test) or data folder makes new ones.
_held: dict[str, Any] = {}
#: Who opened each party's page: ``{party: {principal id: {id, name, kind, at}}}``.
#: Presence only, kept in memory; it grants nothing.
_members: dict[str, dict[str, dict]] = {}
#: Reading a queue position then changing it is one step for Sonora's callers.
_queue_lock = asyncio.Lock()

TRANSPORT = {"play": "play", "pause": "pause", "next": "next_track", "previous": "previous_track"}
QUEUE_LIMIT = 500
#: The furthest a party queue page may start.
START_LIMIT = 10000


class Refused(Exception):
    """An answer other than 200, with a JSON body."""

    def __init__(self, status: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status, self.body = status, {"error": error, **extra}


def _answer(exc: Refused) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status)


# -- the engine, the store, the announcements ---------------------------------


def _main():
    from backend import main
    return main


def controller_ready() -> bool:
    return _main().state.get("controller") is not None


def engine() -> PartyEngine:
    """The party engine for the running controller, made (and subscribed to
    the controller's messages) the first time it is asked for."""
    main = _main()
    controller, commands = main.state.get("controller"), main.state.get("commands")
    if controller is None or commands is None:
        raise Refused(503, "not_ready")
    path = home_dir() / "parties.json"
    held = _held.get("engine")
    if held is None or held[0] is not controller or held[1] != path:
        failed = _held.get("failed")
        if failed is not None and failed[0] is controller and failed[1] == path:
            raise Refused(503, "parties_unavailable")
        try:
            found = PartyEngine(path, controller, commands, _scene_metadata, _notify)
        except Exception:
            # Said once; not tried again until the controller or folder changes.
            log.exception("parties: the party engine could not be made from %s", path)
            _held["failed"] = (controller, path)
            raise Refused(503, "parties_unavailable") from None
        _held.pop("failed", None)
        controller.on_state(found.on_state)
        _held["engine"] = held = (controller, path, found)
        _members.clear()
    return held[2]


def presets() -> Presets:
    path = home_dir() / "presets.json"
    held = _held.get("presets")
    if held is None or held[0] != path:
        _held["presets"] = held = (path, Presets(path))
    return held[1]


def reset() -> None:
    """Forget the engine and store in use (tests)."""
    _held.clear()
    _members.clear()


async def _scene_metadata(zone: str, uri: str, title: str) -> str:
    return await _main()._scene_metadata(zone, uri, title)


async def _notify(message: dict) -> None:
    """The engine's notices, passed on as they are, then a refresh."""
    await _main()._fan_out(message)
    await changed()


async def changed() -> None:
    await _main()._fan_out({"type": "parties"})


def _household_of(uuid: str) -> str | None:
    found = _main().state["controller"].household_of(uuid)
    return found.id if found is not None else None


def _room(zone: str) -> str:
    try:
        return str(_main().state["controller"].zone(zone).name)
    except (KeyError, AttributeError):
        return zone


async def _engine_call(coro):
    """Run an engine change, turning its refusals into answers."""
    try:
        return await coro
    except SpeakerTaken as exc:
        raise Refused(409, "speaker_taken", zone=exc.zone, room=_room(exc.zone),
                      party_id=exc.party_id, party_name=exc.party_name) from None
    except MixedHousehold:
        raise Refused(400, "mixed_household") from None
    except NoSpeakers as exc:
        raise Refused(409, "no_speakers", message=str(exc)) from None
    except ValueError as exc:
        raise Refused(400, "invalid", message=str(exc)) from None
    except KeyError as exc:
        raise Refused(404, "not_found", message=str(exc)) from None


# -- reading parties ----------------------------------------------------------


def _with_members(party: dict) -> dict:
    return {**party, "members": list(_members.get(party["id"], {}).values())}


def _party(party_id: str, who: Principal) -> dict:
    """The party, as ``who`` may see it: a guest sees only running ones."""
    found = engine().get(party_id)
    if found is None or (who.is_guest and not found["speakers"]):
        raise Refused(404, "not_found", message=party_id)
    return found


def _lead(party: dict) -> str:
    if not party["speakers"]:
        raise Refused(409, "no_speakers", message="the party has no speakers")
    return party["speakers"][0]


async def _whole_queue(lead: str) -> tuple[dict, list[dict]]:
    """The whole queue ``lead`` plays, as ``/api/queue/{uuid}`` gives it:
    the first page's fields and every item."""
    ctl = _main().state["controller"]
    target = ctl.coordinator_of(lead)
    page = (await ctl.content.queue(target.host, start=0, count=QUEUE_LIMIT)).as_dict(target.host)
    items = list(page["items"])
    while len(items) < page["total"]:
        more = (await ctl.content.queue(target.host, start=len(items),
                                        count=QUEUE_LIMIT)).as_dict(target.host)
        if not more["items"]:
            break
        items += more["items"]
    return page, items


def _adders(party: dict, uri: str) -> list[dict]:
    return party.get("queue_owners", {}).get(uri) or []


def _attribution(items: list[dict], party: dict) -> list[int | None]:
    """For each queue item, the index of its adder in ``queue_owners[uri]``,
    or None when it has none. Adds go to the end of the queue, so a URI's
    adders belong to its last copies: with three copies and two adders, the
    first copy (there before) has none and the other two are the adders',
    oldest first."""
    copies: dict[str, int] = {}
    for item in items:
        uri = item.get("uri") or ""
        copies[uri] = copies.get(uri, 0) + 1
    seen: dict[str, int] = {}
    out: list[int | None] = []
    for item in items:
        uri = item.get("uri") or ""
        k = seen.get(uri, 0)
        seen[uri] = k + 1
        n = k - (copies[uri] - len(_adders(party, uri)))
        out.append(n if n >= 0 else None)
    return out


def _may_remove(party: dict, uri: str, nth: int | None, who: Principal) -> bool:
    """A user may remove anything; a guest only a copy they added."""
    if not who.is_guest:
        return True
    adders = _adders(party, uri)
    return nth is not None and nth < len(adders) and adders[nth]["id"] == who.id


# -- routes: parties ----------------------------------------------------------


class StartBody(BaseModel):
    name: str | None = None
    speakers: list[str] | None = None
    preset_id: str | None = None
    take: bool = False


class SpeakerBody(BaseModel):
    zone: str
    take: bool = False


class TakeBody(BaseModel):
    take: bool = False


class QueueBody(BaseModel):
    uri: str = Field(min_length=1)
    metadata: str = ""
    title: str = ""


class VolumeBody(BaseModel):
    zone: str | None = None
    level: int = Field(ge=0, le=100)


async def _start(who: Principal, *, name: str | None, speakers: list[str] | None,
                 preset: dict | None, take: bool) -> dict:
    kwargs: dict[str, Any] = {"take": take, "by": who}
    if preset is not None:
        kwargs.update(name=name or preset["name"], speakers=speakers or preset["speakers"],
                      volumes=preset.get("volumes") or {}, music=preset.get("music"),
                      preset_id=preset["id"])
    else:
        kwargs.update(name=name or f"{who.name}'s party", speakers=speakers or [])
    party = await _engine_call(engine().start(**kwargs))
    await changed()
    tv = (preset or {}).get("tv")
    if tv:
        party = {**party, **_show_on_tv(tv, party["id"])}
    return party


#: Preset TV shows still running; held so they are not garbage-collected.
_tv_tasks: set[asyncio.Task] = set()


def _show_on_tv(tv: dict, party_id: str) -> dict:
    """Show a just-started preset party on the preset's TV without making the
    start wait: an unknown TV or a bad mode is answered at once as
    ``tv_error``; otherwise the show runs in the background (``tv:
    "pending"``) and its outcome is broadcast as ``{"type": "tv_result"}``."""
    from . import tvs  # lazy: keeps the TV code out of this module's import

    try:
        tvs.prepare(tv["tv_id"], tv.get("show"))
    except tvs.TvError as exc:
        return {"tv_error": exc.error}
    task = asyncio.create_task(_show_in_background(tv, party_id))
    _tv_tasks.add(task)
    task.add_done_callback(_tv_tasks.discard)
    return {"tv": "pending"}


async def _show_in_background(tv: dict, party_id: str) -> None:
    from . import tvs

    error = None
    try:
        await tvs.show(tv["tv_id"], party_id, tv.get("show"))
    except tvs.TvError as exc:
        error = exc.error
    except Exception:                                  # noqa: BLE001 - reported, never raised
        log.exception("tvs: showing party %s on %s failed", party_id, tv.get("tv_id"))
        error = "tv_failed"
    try:
        await _main()._fan_out({"type": "tv_result", "party": party_id, "tv_id": tv["tv_id"],
                                "ok": error is None, "error": error})
    except Exception:                                  # noqa: BLE001
        log.exception("tvs: broadcasting the result for party %s failed", party_id)


@router.get("/api/parties")
async def list_parties(request: Request):
    who = request_principal(request)
    try:
        found = [_with_members(p) for p in engine().list() if not who.is_guest or p["speakers"]]
    except Refused as exc:
        return _answer(exc)
    return {"parties": found, "presets": [] if who.is_guest else presets().list()}


@router.post("/api/parties")
async def start_party(request: Request, body: StartBody):
    who = request_principal(request)
    try:
        preset = None
        if body.preset_id:
            preset = presets().get(body.preset_id)
            if preset is None:
                raise Refused(404, "not_found", message=body.preset_id)
        return await _start(who, name=(body.name or "").strip() or None, speakers=body.speakers,
                            preset=preset, take=body.take)
    except Refused as exc:
        return _answer(exc)


@router.post("/api/parties/{party_id}/end")
async def end_party(request: Request, party_id: str):
    request_principal(request)
    try:
        result = await _engine_call(engine().end(party_id))
    except Refused as exc:
        return _answer(exc)
    _members.pop(party_id, None)
    await changed()
    return result


@router.post("/api/parties/{party_id}/speakers")
async def add_speaker(request: Request, party_id: str, body: SpeakerBody):
    who = request_principal(request)
    try:
        party = await _engine_call(engine().add_speaker(party_id, body.zone, who, take=body.take))
    except Refused as exc:
        return _answer(exc)
    await changed()
    return party


@router.delete("/api/parties/{party_id}/speakers/{zone}")
async def remove_speaker(request: Request, party_id: str, zone: str):
    request_principal(request)
    try:
        party = await _engine_call(engine().remove_speaker(party_id, zone))
    except Refused as exc:
        return _answer(exc)
    await changed()
    return party


@router.post("/api/parties/{party_id}/join")
async def join_party(request: Request, party_id: str):
    who = request_principal(request)
    try:
        _party(party_id, who)
    except Refused as exc:
        return _answer(exc)
    _members.setdefault(party_id, {})[who.id] = {"id": who.id, "name": who.name,
                                                 "kind": who.kind, "at": time.time()}
    await changed()
    return {"ok": True}


@router.get("/api/parties/{party_id}/queue")
async def party_queue(request: Request, party_id: str,
                      start: int = Query(default=0, ge=0, le=START_LIMIT),
                      count: int = Query(default=200, ge=1, le=QUEUE_LIMIT)):
    who = request_principal(request)
    try:
        party = _party(party_id, who)
        lead = _lead(party)
    except Refused as exc:
        return _answer(exc)
    # The whole queue, so each copy of a URI is matched to its own adder.
    page, items = await _whole_queue(lead)
    owners = _attribution(items, party)
    out = []
    for item, nth in list(zip(items, owners))[start:start + count]:
        uri = item.get("uri") or ""
        adders = _adders(party, uri)
        out.append({**item, "added_by": dict(adders[nth]) if nth is not None else None,
                    "can_remove": _may_remove(party, uri, nth, who),
                    **_youtube_fields(uri)})
    return {**page, "start": start, "returned": len(out), "items": out,
            "total": len(items), "has_more": start + len(out) < len(items), "party": party_id}


SEARCH_MAX = 10
SEARCH_TERM_MAX = 100


def _youtube_fields(uri: str) -> dict:
    """``source`` and ``video_id`` for a queue item that is one of our YouTube
    relay URLs (the badge and the video window use them)."""
    m = media.MEDIA_URI.fullmatch(uri)
    video_id = media.keys().video_of(m.group(1)) if m else None
    return {"source": "youtube", "video_id": video_id} if video_id else {}


def _brief(video: youtube.Video) -> dict:
    return {"id": video.id, "title": video.title, "channel": video.channel,
            "duration": video.duration, "thumbnail": video.thumbnail}


@router.get("/api/youtube/search")
async def youtube_search(q: str = ""):
    term = q.strip()
    if not 1 <= len(term) <= SEARCH_TERM_MAX:
        return JSONResponse({"error": "bad_query"}, status_code=400)
    try:
        found = await youtube.search(term, SEARCH_MAX)
    except youtube.YouTubeError:
        return JSONResponse({"error": "youtube_failed"}, status_code=502)
    return {"results": [_brief(v) for v in found[:SEARCH_MAX]]}


class YouTubeBody(BaseModel):
    video: str = Field(min_length=1, max_length=300)
    next: bool = False


@router.post("/api/parties/{party_id}/youtube")
async def add_youtube_to_party_queue(request: Request, party_id: str, body: YouTubeBody):
    who = request_principal(request)
    try:
        lead = _lead(_party(party_id, who))
    except Refused as exc:
        return _answer(exc)
    if body.next and who.is_guest:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    video_id = youtube.parse_video_id(body.video)
    if video_id is None:
        return JSONResponse({"error": "bad_link"}, status_code=400)
    try:
        video = await youtube.info(video_id)
        await youtube.audio_source(video_id)      # probe: nothing is queued if it fails
    except youtube.NoAudio:
        return JSONResponse({"error": "no_audio"}, status_code=422)
    except youtube.YouTubeError:
        return JSONResponse({"error": "youtube_failed"}, status_code=502)
    host = _main().state["controller"].coordinator_of(lead).host
    uri = media.media_url(media.keys().issue(video_id), host)
    metadata = media.didl_for(video, uri)
    async with _queue_lock:
        await _main().state["commands"].add_to_queue(lead, uri, metadata, next_=body.next)
        # Attributed even for Play Next: every add issues a fresh media key,
        # so the URI is unique and the last-copies matching stays right.
        engine().record_add(party_id, uri, who)
    await changed()
    return {"ok": True, "item": _brief(video)}


@router.post("/api/parties/{party_id}/queue")
async def add_to_party_queue(request: Request, party_id: str, body: QueueBody):
    who = request_principal(request)
    try:
        lead = _lead(_party(party_id, who))
    except Refused as exc:
        return _answer(exc)
    metadata = body.metadata or (await _scene_metadata(lead, body.uri, body.title)
                                 if body.title else "")
    async with _queue_lock:
        result = await _main().state["commands"].add_to_queue(lead, body.uri, metadata)
        engine().record_add(party_id, body.uri, who)
    await changed()
    return {"ok": True, **(result or {})}


@router.delete("/api/parties/{party_id}/queue/{index}")
async def remove_from_party_queue(request: Request, party_id: str, index: int):
    """Remove the track at ``index`` (1-based) from the party's queue."""
    who = request_principal(request)
    try:
        party = _party(party_id, who)
        lead = _lead(party)
        if index < 1:
            raise Refused(400, "invalid", message="track positions start at 1")
        async with _queue_lock:
            _, items = await _whole_queue(lead)
            if len(items) < index:
                raise Refused(404, "not_found", message=f"no track {index}")
            uri = items[index - 1].get("uri") or ""
            nth = _attribution(items, party)[index - 1]
            if not _may_remove(party, uri, nth, who):
                raise Refused(403, "not_yours")
            await _main().state["commands"].remove_from_queue(lead, index)
            if nth is not None:
                engine().record_remove(party_id, uri, nth=nth)
    except Refused as exc:
        return _answer(exc)
    await changed()
    return {"ok": True}


@router.post("/api/parties/{party_id}/transport/{action}")
async def party_transport(request: Request, party_id: str, action: str):
    who = request_principal(request)
    try:
        if action not in TRANSPORT:
            raise Refused(404, "not_found", message=f"unknown transport action {action!r}")
        lead = _lead(_party(party_id, who))
    except Refused as exc:
        return _answer(exc)
    await getattr(_main().state["commands"], TRANSPORT[action])(lead)
    return {"ok": True}


@router.post("/api/parties/{party_id}/volume")
async def party_volume(request: Request, party_id: str, body: VolumeBody):
    who = request_principal(request)
    try:
        party = _party(party_id, who)
        lead = _lead(party)
        if body.zone is not None and body.zone not in party["speakers"]:
            raise Refused(400, "not_in_party", zone=body.zone)
    except Refused as exc:
        return _answer(exc)
    commands = _main().state["commands"]
    if body.zone is None:
        await commands.set_group_volume(lead, body.level)
    else:
        await commands.set_volume(body.zone, body.level)
    return {"ok": True}


# -- routes: presets ----------------------------------------------------------


def _preset_call(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise Refused(400, "invalid", message=str(exc)) from None


@router.get("/api/presets")
async def list_presets():
    return {"presets": presets().list()}


@router.post("/api/presets")
async def add_preset(body: dict):
    try:
        if not controller_ready():
            raise Refused(503, "not_ready")
        preset = _preset_call(presets().add, body, _household_of)
    except Refused as exc:
        return _answer(exc)
    await changed()
    return preset


@router.put("/api/presets/{preset_id}")
async def update_preset(preset_id: str, body: dict):
    try:
        if not controller_ready():
            raise Refused(503, "not_ready")
        preset = _preset_call(presets().update, preset_id, body, _household_of)
        if preset is None:
            raise Refused(404, "not_found", message=preset_id)
    except Refused as exc:
        return _answer(exc)
    await changed()
    return preset


@router.delete("/api/presets/{preset_id}")
async def remove_preset(preset_id: str):
    if presets().remove(preset_id) is None:
        return _answer(Refused(404, "not_found", message=preset_id))
    await changed()
    return {"ok": True}


@router.post("/api/presets/{preset_id}/start")
async def start_preset(request: Request, preset_id: str, body: TakeBody | None = None):
    who = request_principal(request)
    try:
        preset = presets().get(preset_id)
        if preset is None:
            raise Refused(404, "not_found", message=preset_id)
        return await _start(who, name=None, speakers=None, preset=preset,
                            take=bool(body and body.take))
    except Refused as exc:
        return _answer(exc)


# -- attribution for Sonora's own queue routes --------------------------------

_SOURCE = re.compile(r"/api/zones/([^/]+)/source")
_QUEUE_ITEM = re.compile(r"/api/queue/([^/]+)/(\d+)")


def _party_of(zone: str) -> str | None:
    try:
        return engine().owner_of(zone)
    except Refused:
        return None


async def attribution(request: Request, call_next):
    """Sonora's own interface adds to and removes from a room's queue through
    its own routes. When that room is a party speaker, the party's record of
    who added what follows: an enqueue is credited to whoever sent it, and a
    removed track drops its adder. Runs after the permission guard, so the
    principal is known and the request was allowed."""
    method, path = request.method, request.url.path
    if method == "POST" and (m := _SOURCE.fullmatch(path)):
        party_id = _party_of(m.group(1)) if controller_ready() else None
        if party_id is None:
            return await call_next(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        response = await call_next(request)
        who = getattr(request.state, "principal", None)
        # Only an add at the end: the party matches adders to a URI's last
        # copies, which a Play Next or a drop between rows would break.
        if (response.status_code == 200 and who is not None and isinstance(body, dict)
                and body.get("enqueue") and not body.get("replace") and body.get("uri")
                and not body.get("next") and not body.get("position")):
            engine().record_add(party_id, str(body["uri"]), who)
            await changed()
        return response
    if method == "DELETE" and (m := _QUEUE_ITEM.fullmatch(path)):
        party_id = _party_of(m.group(1)) if controller_ready() else None
        index = int(m.group(2))
        if party_id is None or index < 1:
            return await call_next(request)
        async with _queue_lock:
            try:
                party = engine().get(party_id) or {}
                _, items = await _whole_queue(m.group(1))
                nth = _attribution(items, party)[index - 1] if len(items) >= index else None
                found = (items[index - 1].get("uri") or "", nth) if nth is not None else None
            except Exception:                          # noqa: BLE001 - Sonora's route answers for itself
                found = None
            response = await call_next(request)
            if response.status_code == 200 and found is not None:
                engine().record_remove(party_id, found[0], nth=found[1])
        if response.status_code == 200 and found is not None:
            await changed()
        return response
    if path.startswith("/api/") and controller_ready():
        # Made at the first call after a start, so a party follows its
        # speakers whether or not anyone opens the party page.
        try:
            engine()
        except Exception:                              # noqa: BLE001 - never in the way of a request
            pass
    return await call_next(request)
