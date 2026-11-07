"""Live sound from PCs and phones: one encoder per session, many listeners.

Every live source (AirPlay, Scream, a browser tab) hands raw PCM to a
``LiveSession``. The session encodes it once with ffmpeg to MP3 320 kbit/s
and fans the bytes out to every speaker fetching
``/api/home/live/<key>.mp3``; the target's coordinator is told to play
``x-rincon-mp3radio://<ip>:<port>/api/home/live/<key>.mp3``, the scheme
Sonos uses for internet radio.

Starting live sound remembers the queue's track and position (or the
station, line-in or TV input playing). When the sender goes quiet for 8 s (no
data, or silence: ``is_quiet``) or disconnects, the session ends and the
queue resumes there (a station comes back only if it was playing). There is
one live session per set of speakers: a newer session on the same target, or
on a target with the same coordinator, replaces the older one and inherits
the remembered point, so only one resume happens, after the last one ends;
live sound for a room in a running party plays on the party. A party that
ends while live, or a room taken into a party while live, gets no resume.
Continuous sources (AirPlay, Scream) do not take a target back after being
replaced until their sender has been quiet for ``HOLD_SECONDS``.

Party sessions also feed Phase 3's analyzer (``analysis.register_source``)
with a second, tiny ffmpeg converting to s16le mono 22050 Hz, when that
module is present.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = ["LiveHub", "LiveSession", "NoTarget", "Target", "InputFormat",
           "DEFAULT_INPUT", "IDLE_SECONDS", "HOLD_SECONDS", "HOLD_REASONS",
           "MAX_LISTENERS", "is_quiet", "live_didl", "spawn_ffmpeg"]

#: ``("party", party_id)`` or ``("zone", zone_uuid)``.
Target = tuple[str, str]
#: ``(ffmpeg sample format, rate, channels)``, e.g. ``("s16le", 44100, 2)``.
InputFormat = tuple[str, int, int]
DEFAULT_INPUT: InputFormat = ("s16le", 44100, 2)
IDLE_SECONDS = 8.0
#: The most encoded audio a listener is ever behind by (and what a new
#: listener starts with): about 1.6 s at 320 kbit/s.
BACKLOG = 64 * 1024
#: The most analysis PCM held when the analyzer is not reading (~1.5 s).
_ANALYSIS_BACKLOG = 64 * 1024
#: Non-queue sources put back after live sound: stations (radio schemes and
#: plain streams), a line-in, a TV input.
_RESTORABLE = ("x-rincon-mp3radio:", "x-sonosapi-stream:", "x-sonosapi-radio:",
               "x-sonosapi-hls:", "x-sonosapi-hls-static:", "aac:", "hls-radio:",
               "x-rincon-stream:", "x-sonos-htastream:", "http:", "https:")
_READ = 16 * 1024
#: How long an encoder gets to exit on its own before it is killed, and how
#: long ``end`` waits for any pipe at all, so a replacement under the hub's
#: lock is never held up for long.
_EXIT_GRACE = 0.5
_CLOSE_TIMEOUT = 1.0
#: How fresh a just-finished resume must be to be reused as the next
#: session's resume point (the transport model lags the speaker's events).
_JUST_RESUMED = 5.0
#: End reasons after which the queue is not resumed. ``joined_party``: the
#: room of a zone session was taken into a party while live.
_NO_RESUME = frozenset({"replaced", "party_ended", "shutdown", "joined_party"})
#: End reasons after which a continuous source (AirPlay, Scream) must not
#: take the speakers back on its next packet: a newer sender wins. Such a
#: source holds until its own sender has been quiet for ``HOLD_SECONDS``.
HOLD_REASONS = frozenset({"replaced", "joined_party"})
HOLD_SECONDS = 2.0
#: The most speakers fetching one live session at once (a party has a lead
#: and its members join it, so a handful is normal).
MAX_LISTENERS = 16
#: Peak sample value at or below which s16 PCM counts as silence (a few LSB
#: of dither or noise from a "silent" sender); scaled for wider samples.
QUIET_PEAK = 16
_WIDTH = {"s16le": 2, "s24le": 3, "s32le": 4}
_CAST = {2: "h", 4: "i"}
#: About this many frames are looked at per chunk (a stride through it).
_QUIET_FRAMES = 256

try:  # Phase 3's analyzer; absent in builds without it
    from . import analysis as _analysis_module
except ImportError:  # pragma: no cover - depends on what is merged
    _analysis_module = None


class NoTarget(Exception):
    """The party is not running or the room is unknown."""


class Pipe(Protocol):
    """A running encoder: PCM in, bytes out."""

    async def write(self, data: bytes) -> None: ...

    async def read(self) -> bytes:
        """The next bytes, ``b""`` at the end."""
        ...

    async def close(self) -> None:
        """End the input and stop the process."""
        ...


Spawn = Callable[[list[str]], Awaitable[Pipe]]


class _FfmpegPipe:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def write(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            return
        try:
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def read(self) -> bytes:
        assert self._proc.stdout is not None
        try:
            return await self._proc.stdout.read(_READ)
        except (ConnectionResetError, BrokenPipeError):
            return b""

    async def close(self) -> None:
        stdin = self._proc.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), _EXIT_GRACE)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
        except ProcessLookupError:  # pragma: no cover
            pass


async def spawn_ffmpeg(args: list[str]) -> Pipe:
    """Run ``ffmpeg <args>`` with pipes on stdin and stdout."""
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg is not installed")
    proc = await asyncio.create_subprocess_exec(
        path, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    return _FfmpegPipe(proc)


def _input_args(fmt: InputFormat) -> list[str]:
    sample, rate, channels = fmt
    return ["-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
            "-f", str(sample), "-ar", str(int(rate)), "-ac", str(int(channels)), "-i", "pipe:0"]


def encoder_args(fmt: InputFormat) -> list[str]:
    return [*_input_args(fmt), "-ar", "44100", "-ac", "2", "-flush_packets", "1",
            "-f", "mp3", "-b:a", "320k", "pipe:1"]


def analysis_args(fmt: InputFormat) -> list[str]:
    return [*_input_args(fmt), "-f", "s16le", "-ar", "22050", "-ac", "1", "pipe:1"]


def is_quiet(pcm: bytes, fmt: InputFormat = DEFAULT_INPUT) -> bool:
    """Whether ``pcm`` (in ``fmt``) is silence or near-silence: no sample in
    a stride of frames through it is louder than ``QUIET_PEAK`` (for s16;
    scaled up for s24 and s32). An unknown sample format counts only exact
    zeros as quiet."""
    sample, _rate, channels = fmt
    width = _WIDTH.get(str(sample))
    if width is None:
        return not any(pcm)
    channels = max(1, int(channels))
    frame = width * channels
    usable = len(pcm) - len(pcm) % frame
    if usable <= 0:
        return True
    step = max(1, (usable // frame) // _QUIET_FRAMES)
    peak = QUIET_PEAK << (8 * (width - 2))
    view = memoryview(pcm)[:usable]
    code = _CAST.get(width)
    if code is not None and sys.byteorder == "little":
        samples = view.cast(code)
        for channel in range(channels):
            picked = samples[channel::step * channels]
            if max(picked) > peak or min(picked) < -peak:
                return False
        return True
    return not any(abs(int.from_bytes(view[i:i + width], "little", signed=True)) > peak
                   for start in range(0, usable, frame * step)
                   for i in range(start, start + frame, width))


def _x(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def live_didl(title: str) -> str:
    """Radio-station metadata for a live stream, titled with the source."""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f"<dc:title>{_x(title)}</dc:title>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        '<desc id="cdudn" nameSpace="urn:schemas-rinconnetworks-com:metadata-1-0/">'
        "SA_RINCON65031_</desc>"
        "</item></DIDL-Lite>"
    )


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _seconds(hms: str) -> int:
    try:
        parts = [int(p) for p in str(hms).split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


class _Listener:
    """What one speaker has not been sent yet, never more than ``BACKLOG``."""

    def __init__(self, start: bytes = b"") -> None:
        self.buf = bytearray(start[-BACKLOG:])
        self.ready = asyncio.Event()
        self.closed = False
        if self.buf:
            self.ready.set()

    def push(self, data: bytes) -> None:
        self.buf += data
        excess = len(self.buf) - BACKLOG
        if excess > 0:
            del self.buf[:excess]
        self.ready.set()

    def close(self) -> None:
        self.closed = True
        self.ready.set()


class LiveSession:
    """One sender's sound on one target."""

    def __init__(self, hub: "LiveHub", *, target: Target, source_name: str, by: str,
                 input_format: InputFormat) -> None:
        self._hub = hub
        self.key = secrets.token_urlsafe(24)
        self.target = target
        self.source_name = source_name
        self.by = by
        self.input_format = input_format
        self.started = time.time()
        self.ended = False
        self.reason: str | None = None
        self._last_sound = hub._clock()
        self._encoder: Pipe | None = None
        self._analysis: Pipe | None = None
        self._analysis_buf: deque[bytes] = deque()
        self._analysis_size = 0
        self._analysis_ready = asyncio.Event()
        self._unregister: Callable[[], None] | None = None
        self._recent = bytearray()
        self._listeners: set[_Listener] = set()
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        #: ``end`` calls started from inside the session, kept referenced.
        self._ending: set[asyncio.Task] = set()

    def _end_soon(self, reason: str) -> None:
        task = asyncio.ensure_future(self.end(reason))
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)

    def as_dict(self) -> dict:
        return {"key": self.key, "target": {"kind": self.target[0], "id": self.target[1]},
                "source": self.source_name, "by": self.by, "started": self.started}

    # -- running -------------------------------------------------------------

    async def _open(self) -> None:
        self._encoder = await self._hub._spawn(encoder_args(self.input_format))
        self._tasks.append(asyncio.ensure_future(self._pump()))
        register = self._hub._analysis
        if register is not None and self.target[0] == "party":
            try:
                self._analysis = await self._hub._spawn(analysis_args(self.input_format))
                self._tasks.append(asyncio.ensure_future(self._pump_analysis()))
                self._unregister = register(self.target[1], self._analysis_pcm())
            except Exception:
                log.exception("live: the analyzer feed could not be started")
        self._tasks.append(asyncio.ensure_future(self._watchdog()))

    async def _pump(self) -> None:
        assert self._encoder is not None
        try:
            while True:
                data = await self._encoder.read()
                if not data:
                    break
                self._recent += data
                if len(self._recent) > BACKLOG:
                    del self._recent[:len(self._recent) - BACKLOG]
                for listener in self._listeners:
                    listener.push(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live: reading the encoder failed")
        if not self.ended:
            # The encoder stopped by itself: nothing more will come.
            self._end_soon("encoder_stopped")

    async def _pump_analysis(self) -> None:
        assert self._analysis is not None
        try:
            while True:
                data = await self._analysis.read()
                if not data:
                    break
                self._analysis_buf.append(data)
                self._analysis_size += len(data)
                while self._analysis_size > _ANALYSIS_BACKLOG and len(self._analysis_buf) > 1:
                    self._analysis_size -= len(self._analysis_buf.popleft())
                self._analysis_ready.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live: reading the analyzer feed failed")

    async def _analysis_pcm(self) -> AsyncIterator[bytes]:
        while True:
            while not self._analysis_buf:
                if self.ended:
                    return
                self._analysis_ready.clear()
                await self._analysis_ready.wait()
            data = self._analysis_buf.popleft()
            self._analysis_size -= len(data)
            yield data

    async def _watchdog(self) -> None:
        while not self.ended:
            await self._hub._sleep(1.0)
            await self.check_idle()

    async def check_idle(self) -> bool:
        """End the session if no sound came for ``IDLE_SECONDS``; whether it
        ended now."""
        if self.ended or self._hub._clock() - self._last_sound < IDLE_SECONDS:
            return False
        await self.end("idle")
        return True

    async def feed(self, pcm: bytes) -> None:
        """Hand PCM in the session's input format to the encoder."""
        if self.ended or not pcm:
            return
        if not is_quiet(pcm, self.input_format):
            self._last_sound = self._hub._clock()
        async with self._write_lock:
            if self.ended:
                return
            try:
                if self._encoder is not None:
                    await self._encoder.write(pcm)
                if self._analysis is not None:
                    await self._analysis.write(pcm)
            except Exception:
                log.exception("live: writing to the encoder failed")
                self._end_soon("encoder_failed")

    def has_room(self) -> bool:
        """Whether another speaker may listen (at most ``MAX_LISTENERS``)."""
        return len(self._listeners) < MAX_LISTENERS

    def listen(self) -> AsyncIterator[bytes]:
        """The encoded stream from now on (with up to ``BACKLOG`` of what
        just went by), until the session ends."""
        listener = _Listener(bytes(self._recent))
        if self.ended:
            listener.close()
        else:
            self._listeners.add(listener)
        return self._listen(listener)

    async def _listen(self, listener: _Listener) -> AsyncIterator[bytes]:
        try:
            while True:
                if not listener.buf:
                    if listener.closed:
                        return
                    listener.ready.clear()
                    await listener.ready.wait()
                    continue
                data = bytes(listener.buf)
                listener.buf.clear()
                yield data
        finally:
            self._listeners.discard(listener)

    async def end(self, reason: str) -> None:
        """Stop the session; listeners see the end of the stream, the key
        stops working, and (unless ``reason`` says otherwise) the queue the
        session interrupted resumes."""
        if self.ended:
            return
        self.ended, self.reason = True, reason
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        for listener in list(self._listeners):
            listener.close()
        self._analysis_ready.set()
        if self._unregister is not None:
            try:
                self._unregister()
            except Exception:  # pragma: no cover
                log.exception("live: unregistering the analyzer feed failed")
        for pipe in (self._encoder, self._analysis):
            if pipe is not None:
                try:
                    await asyncio.wait_for(pipe.close(), _CLOSE_TIMEOUT)
                except TimeoutError:
                    log.warning("live: an encoder did not stop within %.1f s", _CLOSE_TIMEOUT)
                except Exception:
                    log.exception("live: stopping the encoder failed")
        log.info("live: session %s on %s:%s ended (%s)", self.key[:6], *self.target, reason)
        await self._hub._finished(self, reason)


class LiveHub:
    """The live sessions now on, at most one per target, and the queue
    positions to come back to."""

    def __init__(self, *, controller, commands, resolve: Callable[[Target], str | None],
                 base_url: Callable[[str], str], spawn: Spawn = spawn_ffmpeg,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 analysis: Callable[[str, AsyncIterator[bytes]], Callable[[], None]] | None | str = "auto",
                 party_of: Callable[[str], str | None] | None = None) -> None:
        """``resolve`` maps a target to the zone whose coordinator plays it
        (a party's lead), or None when the party is not running / the room is
        unknown. ``base_url`` gives ``http://<ip>:<port>`` as the speaker at
        a host reaches this server. ``party_of`` gives the running party a
        room is in (None when it is in none): live sound for such a room
        plays on the party."""
        self._controller = controller
        self._commands = commands
        self._resolve = resolve
        self._party_of = party_of
        self._base_url = base_url
        self._spawn = spawn
        self._clock = clock
        self._sleep = sleep
        if analysis == "auto":
            analysis = getattr(_analysis_module, "register_source", None)
        self._analysis = analysis
        self._sessions: dict[str, LiveSession] = {}
        self._current: dict[Target, LiveSession] = {}
        #: ``{target: {zone, track_no, position, playing}}``: where to come back to.
        self._resume: dict[Target, dict] = {}
        #: A resume under way or waiting for the lock, per target.
        self._resuming: dict[Target, asyncio.Task] = {}
        #: ``{target: (point, clock)}``: the last resume done, reused as the
        #: next session's point while the transport model may still lag.
        self._just_resumed: dict[Target, tuple[dict, float]] = {}
        self._lock = asyncio.Lock()
        #: The task holding ``_lock`` in ``start`` (an end inside it must not
        #: wait for a resume that needs the lock).
        self._lock_owner: asyncio.Task | None = None
        #: Resume tasks whose party ended before they ran: they do nothing.
        self._skip: set[asyncio.Task] = set()
        #: Ends started from ``rooms_changed``, kept referenced.
        self._ending: set[asyncio.Task] = set()

    # -- reading ---------------------------------------------------------------

    def session(self, key: str) -> LiveSession | None:
        found = self._sessions.get(key)
        return found if found is not None and not found.ended else None

    def current(self, target: Target) -> LiveSession | None:
        return self._current.get(tuple(target))

    def sessions(self) -> list[LiveSession]:
        return list(self._current.values())

    # -- starting and ending --------------------------------------------------

    def _coordinator(self, zone: str) -> str:
        try:
            return str(self._controller.coordinator_of(zone).uuid)
        except (KeyError, AttributeError):
            return zone

    def _resume_point(self, zone: str) -> dict | None:
        """Where the coordinator of ``zone`` is: its queue position, or the
        station / line-in / TV it plays (``uri`` + ``metadata``, kept only
        while it plays). None when there is nothing to come back to."""
        try:
            coord = self._controller.coordinator_of(zone)
        except KeyError:
            return None
        tr = coord.transport
        playing = getattr(tr, "state", "") in ("PLAYING", "TRANSITIONING")
        uri = str(getattr(tr, "media_uri", "") or "")
        if not uri.startswith("x-rincon-queue:"):
            return self._source_point(coord, tr, uri, playing)
        track = int(getattr(tr, "track_number", 0) or 0)
        if track <= 0:
            return None
        position = _seconds(getattr(tr, "rel_time", "0:00:00"))
        read_at = float(getattr(tr, "position_read_at", 0.0) or 0.0)
        if playing and read_at > 0:
            position += max(0.0, time.monotonic() - read_at)
        return {"zone": coord.uuid, "track_no": track, "position": _hms(position),
                "playing": playing}

    @staticmethod
    def _source_point(coord, tr, uri: str, playing: bool) -> dict | None:
        """A playing station, line-in or TV input to put back after live
        sound (not a group member, a cloud session or live sound itself)."""
        if not playing:
            return None
        track_uri = str(getattr(tr, "track_uri", "") or "")
        if track_uri.startswith("x-sonos-htastream:") and not uri:
            uri = track_uri
        if (not uri or uri.startswith("x-rincon:") or uri.endswith("#vli")
                or "/api/home/live/" in uri or getattr(tr, "is_external_session", False)):
            return None
        if not uri.startswith(_RESTORABLE):
            return None
        return {"zone": coord.uuid, "uri": uri,
                "metadata": str(getattr(coord, "media_metadata", "") or ""),
                "playing": True}

    async def start(self, *, target: Target, source_name: str, by: str,
                    input_format: InputFormat = DEFAULT_INPUT) -> LiveSession:
        """Play live sound on ``target``, ending any session already there."""
        target = (str(target[0]), str(target[1]))
        if target[0] not in ("party", "zone"):
            raise NoTarget(target)
        party = self._party_holding(target[1]) if target[0] == "zone" else None
        if party is not None:
            # The room is in a running party: one session per set of speakers.
            target = ("party", party)
        # A resume on this target finishes before anything is read for the
        # new session (it also needs the lock; waiting outside it avoids
        # depending on who reached the lock first).
        while (pending := self._resuming.get(target)) is not None:
            if pending.done():
                # Its done callback has not run yet; awaiting would not yield.
                self._resuming.pop(target, None)
                break
            await self._wait_resume(pending)
        async with self._lock:
            self._lock_owner = asyncio.current_task()
            try:
                return await self._start_locked(target, source_name, by, input_format)
            finally:
                self._lock_owner = None

    def _party_holding(self, zone: str) -> str | None:
        if self._party_of is None:
            return None
        try:
            found = self._party_of(zone)
        except Exception:                                  # noqa: BLE001
            log.exception("live: reading the party of %s failed", zone)
            return None
        return str(found) if found else None

    def _fresh_point(self, target: Target, zone: str) -> dict | None:
        just = self._just_resumed.pop(target, None)
        if just is not None:
            point, at = just
            age = self._clock() - at
            if 0 <= age < _JUST_RESUMED:
                point = dict(point)
                if point["playing"] and "position" in point:
                    point["position"] = _hms(_seconds(point["position"]) + age)
                return point
        return self._resume_point(zone)

    async def _start_locked(self, target: Target, source_name: str, by: str,
                            input_format: InputFormat) -> LiveSession:
        zone = self._resolve(target)
        if zone is None:
            raise NoTarget(target)
        old = self._current.get(target)
        # Other sessions playing on the same coordinator (a room grouped with
        # this one, say): one live session per set of speakers. Their resume
        # point comes along.
        coordinator = self._coordinator(zone)
        others = []
        for t, s in list(self._current.items()):
            if t == target:
                continue
            other_zone = self._resolve(t)
            if other_zone is not None and self._coordinator(other_zone) == coordinator:
                others.append(s)
        handed = None
        for other in others:
            point = self._resume.pop(other.target, None)
            if handed is None:
                handed = point
            await other.end("replaced")
        if old is not None:
            # The queue position stays the one from before the first
            # session: the coordinator now plays the old live stream.
            if handed is not None:
                self._resume.setdefault(target, handed)
            await old.end("replaced")
        elif others:
            if handed is not None:
                self._resume[target] = handed
            else:
                self._resume.pop(target, None)
        else:
            point = self._fresh_point(target, zone)
            if point is not None:
                self._resume[target] = point
            else:
                self._resume.pop(target, None)
        session = LiveSession(self, target=target, source_name=source_name, by=by,
                              input_format=input_format)
        try:
            await session._open()
        except Exception:
            await session.end("failed")
            point = self._resume.pop(target, None)
            if (old is not None or others) and point is not None:
                # The old session is gone and nothing replaces it: the
                # queue it interrupted comes back (after this start).
                self._schedule_resume(target, point)
            raise
        self._sessions[session.key] = session
        self._current[target] = session
        try:
            coord = self._controller.coordinator_of(zone)
            base = self._base_url(coord.host)
            uri = "x-rincon-mp3radio://" + base.split("://", 1)[-1] + \
                f"/api/home/live/{session.key}.mp3"
            await self._commands.play_now(coord.uuid, uri, live_didl(source_name))
        except Exception:
            await session.end("failed")
            raise
        log.info("live: %s started %r on %s:%s", by, source_name, *target)
        return session

    async def _finished(self, session: LiveSession, reason: str) -> None:
        self._sessions.pop(session.key, None)
        if self._current.get(session.target) is not session:
            return      # already replaced
        del self._current[session.target]
        if reason == "replaced":
            return      # the newer session inherits the resume point
        point = self._resume.pop(session.target, None)
        if point is None or reason in _NO_RESUME:
            return
        task = self._schedule_resume(session.target, point)
        if self._lock_owner is not asyncio.current_task():
            await self._wait_resume(task)

    @staticmethod
    async def _wait_resume(task: asyncio.Task) -> None:
        """Wait for a resume to be over. The resume being cancelled (rather
        than the waiter) just means it is over."""
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    def _schedule_resume(self, target: Target, point: dict) -> asyncio.Task:
        task = asyncio.ensure_future(self._resume_now(target, point))
        self._resuming[target] = task

        def done(t: asyncio.Task) -> None:
            self._skip.discard(t)
            if self._resuming.get(target) is t:
                del self._resuming[target]
        task.add_done_callback(done)
        return task

    async def _resume_now(self, target: Target, point: dict) -> None:
        """Put the queue back where ``point`` says, under the hub's lock so
        it never interleaves with a start's ``play_now``."""
        async with self._lock:
            if asyncio.current_task() in self._skip:
                return      # its party ended meanwhile
            if target in self._current:
                # A newer session got in first: it owns the way back.
                self._resume.setdefault(target, point)
                return
            zone = self._resolve(target)
            if zone is None:
                return      # the party is gone
            coordinator = self._coordinator(zone)
            for other in list(self._current):
                other_zone = self._resolve(other)
                if other_zone is not None and self._coordinator(other_zone) == coordinator:
                    # A newer session on another target streams to the same
                    # speakers: seeking now would cut it. It owns the way back.
                    self._resume.setdefault(other, point)
                    return
            try:
                zone = self._controller.coordinator_of(zone).uuid
            except KeyError:
                zone = point["zone"]
            try:
                if "uri" in point:
                    await self._commands.set_source(zone, point["uri"], point["metadata"])
                else:
                    await self._commands.seek_track(zone, point["track_no"])
                    await self._commands.seek_time(zone, point["position"])
                if point["playing"]:
                    await self._commands.play(zone)
            except Exception:
                log.exception("live: resuming the queue on %s failed", zone)
                return
            self._just_resumed[target] = (point, self._clock())

    async def end_for_party(self, party_id: str) -> None:
        """The party is ending: stop its live sound, resume nothing."""
        target = ("party", str(party_id))
        self._resume.pop(target, None)
        self._just_resumed.pop(target, None)
        pending = self._resuming.get(target)
        if pending is not None:
            # Not cancelled (its waiters would see a CancelledError): it
            # sees this mark once it has the lock and does nothing.
            self._skip.add(pending)
        session = self._current.get(target)
        if session is not None:
            await session.end("party_ended")

    def rooms_changed(self) -> None:
        """The parties changed (not awaited): a room session whose room is
        now in a running party ends without a resume (the party has the
        room's speakers now)."""
        for target, pending in list(self._resuming.items()):
            if target[0] == "zone" and self._party_holding(target[1]) is not None:
                self._skip.add(pending)     # the party has the room's speakers now
        for target, session in list(self._current.items()):
            if target[0] != "zone" or self._party_holding(target[1]) is None:
                continue
            self._resume.pop(target, None)
            self._just_resumed.pop(target, None)
            task = asyncio.ensure_future(session.end("joined_party"))
            self._ending.add(task)
            task.add_done_callback(self._ending.discard)

    async def close(self) -> None:
        """End every session (shutdown); nothing is resumed."""
        for session in list(self._current.values()):
            await session.end("shutdown")
