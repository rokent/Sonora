"""Send this tab's sound: a browser tab's audio over a WebSocket to a party
or a room.

``/ws/tabsound?target=party:<id>`` or ``?target=zone:<uuid>``. The page sends
binary messages: WebM/Opus chunks from ``MediaRecorder`` (the first one
carries the WebM header). They are piped into an ffmpeg decoder that turns
them into PCM (s16le, 44.1 kHz, stereo) for a live session.

Who may send where (spec, Phase 4 Global Constraints): a household member
any party or room; a guest only a running party, never a bare room. The
permission rules let both open the socket; the target rule is enforced here.

The session starts on the first sound, not on connect, so sharing a quiet tab
does not cut into the queue. After the hub ends it for silence (the tab
paused), the next sound starts a new one. A newer sender on the same target,
the party ending, or the server stopping close the socket instead.

The server sends small JSON text messages so the page can say what is going
on: ``{"state": "waiting"}`` (connected, no sound yet or paused) and
``{"state": "live"}`` (the speakers play this tab). Close codes after the
handshake (4xxx, with the reason as the error word):

* 4400 ``bad_target``      the target is not ``party:<id>`` / ``zone:<uuid>``
* 4403 ``forbidden``       a guest asked for a room
* 4404 ``unknown_target``  no such running party or room
* 4409 ``replaced``        someone else started sending to the same target
* 4410 ``party_ended``     the party ended
* 4413 ``too_large``       a message over ``MAX_MESSAGE``
* 4500 ``decoder_stopped`` the decoder exited or stalled
* 4503 ``not_ready`` / ``no_decoder`` / ``shutdown``
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket

from . import live, live_routes, routes

log = logging.getLogger("sonora.home.tabsound")

router = APIRouter()

#: The biggest single message accepted (a 250 ms Opus chunk is a few KB).
MAX_MESSAGE = 256 * 1024
#: How long a write into the decoder may take before it counts as stalled.
WRITE_TIMEOUT = 5.0

DECODER_ARGS = ["-nostdin", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
                "-f", "webm", "-i", "pipe:0",
                "-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"]
PCM_FORMAT: live.InputFormat = ("s16le", 44100, 2)

#: The decoder process factory (``ffmpeg <args>`` with stdin/stdout pipes);
#: tests put a fake here.
spawn = live.spawn_ffmpeg

#: Why a session ended -> how the socket closes. Other reasons (idle,
#: encoder trouble) let the next sound start a fresh session.
_FINAL = {"replaced": (4409, "replaced"), "party_ended": (4410, "party_ended"),
          "shutdown": (4503, "shutdown")}


class Stop(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_target(value: str) -> live.Target | None:
    """``("party", id)`` or ``("zone", uuid)`` from ``party:<id>`` / ``zone:<uuid>``."""
    kind, sep, ident = (value or "").partition(":")
    ident = ident.strip()
    if not sep or kind not in ("party", "zone") or not ident or len(ident) > 128:
        return None
    return kind, ident


def check_target(target: live.Target, principal: Any) -> None:
    """Raise ``Stop`` unless ``principal`` may send to ``target`` now. A guest
    only to a running party (one with speakers); anyone only to a party or
    room that resolves to a speaker."""
    if getattr(principal, "kind", "user") == "guest" and target[0] != "party":
        raise Stop(4403, "forbidden")
    try:
        found = live_routes._resolve(target)
    except Exception:
        found = None
    if found is None:
        raise Stop(4404, "unknown_target")


def source_name(principal: Any) -> str:
    name = str(getattr(principal, "name", "") or "").strip()[:60]
    return f"{name} (tab)" if name else "Browser tab"


class TabSender:
    """One tab's decoded sound into live sessions on one target."""

    def __init__(self, websocket: WebSocket, target: live.Target, principal: Any,
                 decoder: live.Pipe) -> None:
        self.ws, self.target, self.principal, self.decoder = websocket, target, principal, decoder
        self.session: Any = None
        self.state: str | None = None

    async def tell(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        try:
            await self.ws.send_text(json.dumps({"state": state}))
        except Exception:
            log.debug("tab sound: telling the page failed", exc_info=True)

    async def deliver(self, pcm: bytes) -> None:
        session = self.session
        if session is not None and session.ended:
            final = _FINAL.get(session.reason or "")
            if final:
                raise Stop(*final)
            session = self.session = None
        if session is None:
            if not any(pcm):
                await self.tell("waiting")
                return
            check_target(self.target, self.principal)   # a guest's party may have ended
            try:
                hub = live_routes.hub()
            except routes.Refused:
                raise Stop(4503, "not_ready") from None
            try:
                session = await hub.start(target=self.target,
                                          source_name=source_name(self.principal),
                                          by="tab", input_format=PCM_FORMAT)
            except live.NoTarget:
                raise Stop(4404, "unknown_target") from None
            self.session = session
            await self.tell("live")
        await session.feed(pcm)

    async def pump(self) -> tuple[int, str]:
        """Decoded PCM into sessions until something says stop."""
        try:
            while True:
                pcm = await self.decoder.read()
                if not pcm:
                    return 4500, "decoder_stopped"
                await self.deliver(pcm)
        except Stop as stop:
            return stop.code, stop.reason

    async def close(self) -> None:
        session = self.session
        if session is not None and not session.ended:
            try:
                await session.end("disconnected")
            except Exception:
                log.exception("tab sound: ending the session failed")
        try:
            await asyncio.wait_for(self.decoder.close(), 2.0)
        except Exception:
            log.debug("tab sound: closing the decoder failed", exc_info=True)


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass


@router.websocket("/ws/tabsound")
async def tab_sound(websocket: WebSocket) -> None:
    from backend import origin

    from .permissions import ws_allowed

    if not origin.websocket_allowed(websocket) or not ws_allowed(websocket):
        await websocket.close(code=1008)
        return
    principal = websocket.state.principal
    await websocket.accept()
    target = parse_target(websocket.query_params.get("target", ""))
    if target is None:
        await _close(websocket, 4400, "bad_target")
        return
    try:
        check_target(target, principal)
    except Stop as stop:
        await _close(websocket, stop.code, stop.reason)
        return
    try:
        live_routes.hub()
    except routes.Refused:
        await _close(websocket, 4503, "not_ready")
        return
    try:
        decoder = await spawn(list(DECODER_ARGS))
    except Exception as exc:
        log.warning("tab sound: no decoder: %s", exc)
        await _close(websocket, 4503, "no_decoder")
        return
    sender = TabSender(websocket, target, principal, decoder)
    await sender.tell("waiting")
    receiving: asyncio.Future = asyncio.ensure_future(websocket.receive())
    pumping: asyncio.Future = asyncio.ensure_future(sender.pump())
    try:
        while True:
            done, _ = await asyncio.wait({receiving, pumping},
                                         return_when=asyncio.FIRST_COMPLETED)
            if pumping in done:
                code, reason = pumping.result()
                await _close(websocket, code, reason)
                return
            message = receiving.result()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                if len(data) > MAX_MESSAGE:
                    await _close(websocket, 4413, "too_large")
                    return
                try:
                    await asyncio.wait_for(decoder.write(data), WRITE_TIMEOUT)
                except TimeoutError:
                    await _close(websocket, 4500, "decoder_stopped")
                    return
            receiving = asyncio.ensure_future(websocket.receive())
    except Exception:
        log.debug("tab sound socket closed", exc_info=True)
    finally:
        for fut in (receiving, pumping):
            if not fut.done():
                fut.cancel()
        await asyncio.gather(receiving, pumping, return_exceptions=True)
        await sender.close()
