"""Exact visuals for TV mode: the party's own audio, analysed on the server.

When a party plays audio that passes through Sonora (a YouTube item relayed
by ``home/media.py``; Phase 4 live streams through ``register_source``), one
ffmpeg per watched party decodes it at real time from the speaker's current
position to 8-bit mono PCM at 22050 Hz::

    ffmpeg -nostdin [-headers H] -ss <pos> -re -i <url> -f u8 -ac 1 -ar 22050 pipe:1

Its output is read in hops of 735 samples (22050 / 30, so at most 30 frames
a second) and each hop's last 512 samples are published to the party's
``/ws/visual`` subscribers as ``{"t": <track seconds>, "w": <base64 of 512
bytes>}``, ``t`` being the track time of the frame's first sample. Viewers
align frames to the speaker's clock themselves. No audio reaches a viewer.

A per-party loop compares the speaker with the analysis every ``tick``:

* a different track, or a seek that puts them more than 2 s apart,
  restarts ffmpeg at the speaker's position (plus the start-up latency the
  last run of that track measured), and drops every frame still queued for
  the old one, so a viewer never sees the previous track after a skip;
* pause or stop kills ffmpeg; play starts it again;
* a track Sonora does not relay (Spotify, the library) spawns nothing.

Nothing runs at import or without a viewer: the first ``/ws/visual``
subscriber starts the loop for its party and the loop ends, killing ffmpeg,
10 s after the last one leaves. Without ffmpeg on PATH the whole thing is
off and ``visuals_mode()`` says ``"approximate"``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import re
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

__all__ = ["Analyzer", "Playing", "visuals_mode", "analyzer", "register_source",
           "unregister_source", "router"]

router = APIRouter()

RATE = 22050
FRAME_SAMPLES = 512
FPS = 30
HOP_SAMPLES = RATE // FPS          # 735: one published frame per hop
QUEUE_FRAMES = 8                   # per subscriber; the oldest goes first
DRIFT = 2.0                        # seconds apart before a restart
IDLE_STOP = 10.0                   # seconds without a viewer before stopping
MAX_LEAD = 5.0                     # cap on the start-up latency led on restart
BACKOFF_MIN, BACKOFF_MAX = 2.0, 60.0

#: Close codes ``/ws/visual`` uses after accepting, so the page can tell why.
CLOSE_DISABLED = 4001              # no ffmpeg: visuals are approximate
CLOSE_UNKNOWN_PARTY = 4004
CLOSE_TOO_SLOW = 4008              # a send did not complete in SEND_TIMEOUT
SEND_TIMEOUT = 5.0

_MEDIA_PATH = re.compile(r"/api/home/media/([A-Za-z0-9_-]{16,64})\.(?:m4a|aac)")


@dataclass(frozen=True)
class Playing:
    """What a party's coordinator plays: the track URI, its position now (in
    seconds, extrapolated from the last report while playing), and whether it
    is playing."""

    uri: str
    position: float
    playing: bool


Probe = Callable[[str], "Playing | None | Awaitable[Playing | None]"]
Resolve = Callable[[str], Awaitable["tuple[str, dict[str, str]] | None"]]
Spawn = Callable[[list[str]], Awaitable[Any]]


# --- pure helpers ----------------------------------------------------------

def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


# TODO(phase 4): make the mode per party. A registered live source needs no
# ffmpeg, yet without ffmpeg the whole analyzer is off (analyzer() is None)
# and such a party is reported "approximate" too.
def visuals_mode() -> Literal["exact", "approximate"]:
    """``exact`` when ffmpeg is on PATH (server frames), else ``approximate``
    (microphone or a generic animation in the page)."""
    return "exact" if ffmpeg_path() else "approximate"


def media_key(uri: str) -> str | None:
    """The Sonora media key in a queue item's URI, else None."""
    try:
        path = urlparse(uri or "").path
    except ValueError:
        return None
    m = _MEDIA_PATH.fullmatch(path)
    return m.group(1) if m else None


def _safe_header(key: str, value: str) -> bool:
    """A header ffmpeg may send: no CR/LF anywhere (it would inject further
    headers into the request) and no ':' in the name."""
    key, value = str(key), str(value)
    return bool(key) and not any(c in key for c in "\r\n:") and not any(c in value for c in "\r\n")


def ffmpeg_args(ffmpeg: str, url: str, headers: dict[str, str], position: float) -> list[str]:
    args = [ffmpeg, "-nostdin", "-loglevel", "error"]
    safe = {k: v for k, v in (headers or {}).items() if _safe_header(k, v)}
    if safe:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in safe.items())]
    # only network reads: a crafted URL cannot make ffmpeg open a local file
    args += ["-protocol_whitelist", "https,tls,tcp,http"]
    return args + ["-ss", f"{max(0.0, position):.3f}", "-re", "-i", url,
                   "-f", "u8", "-ac", "1", "-ar", str(RATE), "pipe:1"]


def u8_from_s16le(data: bytes) -> bytes:
    """Signed 16-bit little-endian PCM to unsigned 8-bit (high byte + 128)."""
    return bytes((b + 128) & 0xFF for b in data[1::2])


def _frame(t: float, hop: bytes) -> dict:
    return {"t": round(t, 3), "w": base64.b64encode(hop[-FRAME_SAMPLES:]).decode()}


async def _spawn_ffmpeg(args: list[str]):
    return await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)


# --- the speaker side ---------------------------------------------------------

def _hms(text: str) -> float:
    try:
        return float(sum(int(p) * 60 ** i
                         for i, p in enumerate(reversed(str(text or "0").split(":")))))
    except ValueError:
        return 0.0


def default_probe(party_id: str) -> Playing | None:
    """The party's coordinator's track and position now, read off the
    controller's state as the pages get it (no speaker call)."""
    from backend import main
    from . import routes

    controller = main.state.get("controller")
    if controller is None:
        return None
    try:
        party = routes.engine().get(party_id)
        if not party or not party.get("speakers"):
            return None
        transport = controller.coordinator_of(party["speakers"][0]).transport
    except Exception:
        log.debug("visual probe failed for %s", party_id, exc_info=True)
        return None
    playing = transport.state in ("PLAYING", "TRANSITIONING")
    position = _hms(transport.rel_time)
    if playing and transport.position_read_at:
        position += max(0.0, time.monotonic() - transport.position_read_at)
    return Playing(str(transport.track_uri or ""), position, playing)


def _video_of(key: str) -> str | None:
    from . import media
    return media.keys().video_of(key)


async def default_resolve(uri: str) -> tuple[str, dict[str, str]] | None:
    """The upstream URL and headers ffmpeg should read for a relayed track,
    or None for anything Sonora does not relay (or cannot resolve now)."""
    from backend import main
    from . import youtube

    key = media_key(uri)
    video_id = _video_of(key) if key else None
    if video_id is None:
        return None
    try:
        src = await youtube.audio_source(video_id)
    except Exception as exc:
        log.info("visuals: no audio source for %s: %s", video_id, exc)
        return None
    if not main.fetchguard.safe_to_fetch(src.url):
        main.fetchguard.refuse(src.url, "visual analysis")
        return None
    await youtube.until_available(src)
    return src.url, dict(src.headers)


# --- the analyzer ------------------------------------------------------------

class Subscription:
    """One viewer's frames: a short queue that drops its oldest when full."""

    def __init__(self, party_id: str):
        self.party_id = party_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_FRAMES)

    def put(self, frame: dict) -> None:
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(frame)

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    async def get(self) -> dict:
        return await self.queue.get()


@dataclass(eq=False)
class _Run:
    """One decode: an ffmpeg process reading a track."""

    uri: str
    start: float
    gen: int
    spawned_at: float
    proc: Any = None
    task: asyncio.Task | None = None
    samples: int = 0
    done: bool = False

    def position(self) -> float:
        return self.start + self.samples / RATE


@dataclass(eq=False)
class _Party:
    id: str
    subs: set = field(default_factory=set)
    idle_since: float = 0.0
    run: _Run | None = None
    gen: int = 0
    task: asyncio.Task | None = None
    lead: dict = field(default_factory=dict)         # uri -> measured latency
    retry_at: dict = field(default_factory=dict)     # uri -> clock
    backoff: dict = field(default_factory=dict)      # uri -> seconds
    #: Whether the party shows its registered live source (not its track).
    on_source: bool = False


class Analyzer:
    """Frames for every watched party. All hooks are injectable for tests."""

    def __init__(self, *, ffmpeg: str | None, probe: Probe = default_probe,
                 resolve: Resolve = default_resolve, spawn: Spawn | None = None,
                 clock: Callable[[], float] = time.monotonic, tick: float = 0.25,
                 idle_stop: float = IDLE_STOP, drift: float = DRIFT):
        self.ffmpeg = ffmpeg
        self._probe, self._resolve = probe, resolve
        self._spawn = spawn or _spawn_ffmpeg
        self._clock, self._tick = clock, tick
        self._idle_stop, self._drift = idle_stop, drift
        self._parties: dict[str, _Party] = {}
        self._sources: dict[str, AsyncIterator[bytes]] = {}
        self._readers: dict[str, asyncio.Task] = {}
        self.loop = asyncio.get_running_loop()

    @property
    def enabled(self) -> bool:
        return bool(self.ffmpeg)

    # -- viewers --

    def subscribe(self, party_id: str) -> Subscription:
        sub = Subscription(party_id)
        if not self.enabled:
            return sub          # nothing will ever arrive; the socket says why
        party = self._parties.get(party_id)
        if party is None:
            party = self._parties[party_id] = _Party(party_id)
            party.task = asyncio.create_task(self._loop(party))
        party.subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        party = self._parties.get(sub.party_id)
        if party is None or sub not in party.subs:
            return
        party.subs.discard(sub)
        if not party.subs:
            party.idle_since = self._clock()

    def running(self, party_id: str) -> bool:
        return party_id in self._parties

    # -- Phase 4 --

    def register_source(self, party_id: str, pcm: AsyncIterator[bytes]) -> Callable[[], None]:
        """Analyse a live stream (s16le mono 22050 Hz chunks) for a party
        instead of its track. Returns the call that unregisters it.

        The stream is read from now until it ends, raises, or is unregistered,
        viewers or not (a producer is never left blocked); frames are only
        made while someone watches. Viewers coming and going, and the party's
        idle stop, never end it."""
        self.unregister_source(party_id)
        self._sources[party_id] = pcm
        self._readers[party_id] = asyncio.create_task(self._read_pcm(party_id, pcm))

        def unregister() -> None:
            if self._sources.get(party_id) is pcm:
                self.unregister_source(party_id)
        return unregister

    def unregister_source(self, party_id: str) -> None:
        """Stop reading the party's live source; it goes back to its track."""
        self._sources.pop(party_id, None)
        reader = self._readers.pop(party_id, None)
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()

    # -- the per-party loop --

    async def _loop(self, party: _Party) -> None:
        try:
            while await self._step(party):
                await asyncio.sleep(self._tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("visual analysis for party %s failed", party.id)
        finally:
            if self._parties.get(party.id) is party:
                del self._parties[party.id]
            await self._stop(party)

    async def _step(self, party: _Party) -> bool:
        now = self._clock()
        if not party.subs and now - party.idle_since >= self._idle_stop:
            del self._parties[party.id]     # a new viewer from here on starts afresh
            return False
        if party.id in self._sources:
            if not party.on_source:
                await self._stop(party)     # the track's ffmpeg gives way
                party.on_source = True
            return True
        if party.on_source:                 # the live source went away
            await self._stop(party)
        want = self._probe(party.id)
        if inspect.isawaitable(want):
            want = await want
        if want is None or not want.playing or not want.uri:
            await self._stop(party)
            return True
        run = party.run
        if run is not None and run.uri == want.uri:
            if run.done:
                await self._stop(party)     # ffmpeg ended or died: back off first
                self._fail(party, want.uri, now)
                return True
            if run.samples and abs(want.position - run.position()) > self._drift:
                await self._stop(party)     # a seek: start again where the speaker is
            else:
                return True
        elif run is not None:
            await self._stop(party)         # a skip
        if now < party.retry_at.get(want.uri, 0.0):
            return True
        resolved = await self._resolve(want.uri)
        if resolved is None:
            self._fail(party, want.uri, now)
            return True
        url, headers = resolved
        await self._start(party, want, url, headers)
        return True

    def _fail(self, party: _Party, uri: str, now: float) -> None:
        delay = party.backoff.get(uri, BACKOFF_MIN / 2) * 2
        party.backoff[uri] = min(BACKOFF_MAX, delay)
        party.retry_at[uri] = now + party.backoff[uri]

    async def _start(self, party: _Party, want: Playing, url: str,
                     headers: dict[str, str]) -> None:
        start = want.position + party.lead.get(want.uri, 0.0)
        args = ffmpeg_args(self.ffmpeg or "ffmpeg", url, headers, start)
        try:
            proc = await self._spawn(args)
        except OSError as exc:
            log.warning("visuals: ffmpeg did not start: %s", exc)
            self._fail(party, want.uri, self._clock())
            return
        run = _Run(want.uri, start, party.gen, self._clock(), proc=proc)
        party.run = run
        run.task = asyncio.create_task(self._read(party, run))

    async def _read(self, party: _Party, run: _Run) -> None:
        try:
            while True:
                hop = await run.proc.stdout.readexactly(HOP_SAMPLES)
                if not self._emit(party, run, hop):
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            run.done = True

    async def _read_pcm(self, party_id: str, source: AsyncIterator[bytes]) -> None:
        """Drain a live source for as long as it is registered. ``t`` counts
        stream seconds from registration, viewers or not."""
        buf = bytearray()
        need = HOP_SAMPLES * 2
        consumed = 0                         # bytes of the stream behind ``buf``
        try:
            async for chunk in source:
                buf += chunk
                party = self._parties.get(party_id)
                if party is None or not party.subs or not party.on_source:
                    drop = len(buf) - len(buf) % 2   # keep sample alignment
                    consumed += drop
                    del buf[:drop]
                    continue
                while len(buf) >= need:
                    hop = u8_from_s16le(bytes(buf[:need]))
                    del buf[:need]
                    t = (consumed // 2 + HOP_SAMPLES - FRAME_SAMPLES) / RATE
                    consumed += need
                    frame = _frame(t, hop)
                    for sub in party.subs:
                        sub.put(frame)
        except asyncio.CancelledError:
            raise                            # unregistered or shut down
        except Exception:
            log.info("visuals: live source for %s failed", party_id, exc_info=True)
        # ended by itself or failed: unregister it
        if self._sources.get(party_id) is source:
            del self._sources[party_id]
        if self._readers.get(party_id) is asyncio.current_task():
            del self._readers[party_id]

    def _emit(self, party: _Party, run: _Run, hop: bytes) -> bool:
        """Publish one hop; False once ``run`` is no longer current."""
        if run.gen != party.gen or party.run is not run:
            return False
        if not run.samples:
            party.lead[run.uri] = min(MAX_LEAD, max(0.0, self._clock() - run.spawned_at))
            party.backoff.pop(run.uri, None)
            party.retry_at.pop(run.uri, None)
        run.samples += HOP_SAMPLES
        frame = _frame(run.start + (run.samples - FRAME_SAMPLES) / RATE, hop)
        for sub in party.subs:
            sub.put(frame)
        return True

    async def _stop(self, party: _Party) -> None:
        """End the current run and drop whatever it queued."""
        run, party.run, party.on_source = party.run, None, False
        party.gen += 1
        for sub in party.subs:
            sub.clear()
        if run is None:
            return
        if run.proc is not None:
            try:
                run.proc.kill()
            except ProcessLookupError:
                pass
        if run.task is not None and run.task is not asyncio.current_task():
            run.task.cancel()
            try:
                await run.task
            except (asyncio.CancelledError, Exception):
                pass
        if run.proc is not None:
            try:
                await asyncio.wait_for(run.proc.wait(), 2)
            except (TimeoutError, ProcessLookupError):
                pass

    async def close(self) -> None:
        for party_id in list(self._readers):
            self.unregister_source(party_id)
        for party in list(self._parties.values()):
            if party.task is not None:
                party.task.cancel()
                try:
                    await party.task
                except (asyncio.CancelledError, Exception):
                    pass
        self._parties.clear()


# --- the process-wide analyzer (lazy) -------------------------------------------

_singleton: Analyzer | None = None
_pending_sources: dict[str, AsyncIterator[bytes]] = {}


def analyzer() -> Analyzer | None:
    """The analyzer for the running loop, made on first use; None without
    ffmpeg (visuals are approximate then)."""
    global _singleton
    path = ffmpeg_path()
    if not path:
        return None
    loop = asyncio.get_running_loop()
    if _singleton is None or _singleton.loop is not loop:
        _singleton = Analyzer(ffmpeg=path)
        for party_id, pcm in _pending_sources.items():
            _singleton.register_source(party_id, pcm)
    return _singleton


def unregister_source(party_id: str) -> None:
    """Phase 4 hook: stop analysing the party's live source."""
    _pending_sources.pop(party_id, None)
    if _singleton is not None:
        _singleton.unregister_source(party_id)


def register_source(party_id: str, pcm: AsyncIterator[bytes]) -> Callable[[], None]:
    """Phase 4 hook: analyse a live stream for ``party_id`` (PCM s16le mono
    22050 Hz). Returns the call that unregisters it."""
    _pending_sources[party_id] = pcm
    try:
        found = analyzer()
    except RuntimeError:        # no running loop: picked up on first use
        found = None
    inner = found.register_source(party_id, pcm) if found else (lambda: None)

    def unregister() -> None:
        if _pending_sources.get(party_id) is pcm:
            del _pending_sources[party_id]
        inner()
    return unregister


# --- /ws/visual ----------------------------------------------------------------

def party_known(party_id: str, principal) -> bool:
    """Whether the party exists for this viewer (a guest sees running ones only)."""
    from . import routes
    if not party_id or not routes.controller_ready():
        return False
    try:
        found = routes.engine().get(party_id)
    except Exception:
        return False
    if found is None:
        return False
    return principal.kind != "guest" or bool(found.get("speakers"))


@router.websocket("/ws/visual")
async def visual_socket(websocket: WebSocket) -> None:
    """Frames for one party: ``/ws/visual?party=<id>``. Server to client only,
    each message ``{"t": seconds, "w": base64}``. Anyone who may open ``/ws``
    may watch (users, guests, displays); a service token may not."""
    from backend import origin

    if not origin.websocket_allowed(websocket):
        await websocket.close(code=1008)
        return
    from .permissions import ws_allowed

    if not ws_allowed(websocket):
        await websocket.close(code=1008)
        return
    principal = websocket.state.principal
    await websocket.accept()
    found = analyzer()
    if found is None:
        await websocket.close(code=CLOSE_DISABLED, reason="visuals_disabled")
        return
    party_id = websocket.query_params.get("party", "")[:128]
    if not party_known(party_id, principal):
        await websocket.close(code=CLOSE_UNKNOWN_PARTY, reason="unknown_party")
        return
    sub = found.subscribe(party_id)
    receiving = asyncio.ensure_future(websocket.receive())
    getting: asyncio.Future | None = None
    try:
        while True:
            getting = asyncio.ensure_future(sub.get())
            done, _ = await asyncio.wait({receiving, getting},
                                         return_when=asyncio.FIRST_COMPLETED)
            if receiving in done:
                if receiving.result().get("type") == "websocket.disconnect":
                    break
                receiving = asyncio.ensure_future(websocket.receive())
            if getting in done:
                text = json.dumps(getting.result(), separators=(",", ":"))
                getting = None
                try:
                    await asyncio.wait_for(websocket.send_text(text), SEND_TIMEOUT)
                except TimeoutError:
                    log.info("visual socket too slow; closing it")
                    try:
                        await asyncio.wait_for(
                            websocket.close(code=CLOSE_TOO_SLOW, reason="too_slow"), 1)
                    except Exception:
                        pass
                    break
            else:
                getting.cancel()
                getting = None
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("visual socket closed", exc_info=True)
    finally:
        for fut in (receiving, getting):
            if fut is not None and not fut.done():
                fut.cancel()
        found.unsubscribe(sub)
