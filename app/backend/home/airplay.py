"""AirPlay receivers: one per running party and one per room.

Each receiver is a shairport-sync process (classic AirPlay, RAOP) named
``Sonora: <party name>`` or ``Sonora: <room name>``, listening on its own RTSP
port in 5100-5199 and writing what it is sent to stdout as raw PCM (s16le,
44.1 kHz, stereo). That PCM goes to the live hub (``live.py``): the first
sound starts a session on the receiver's party or room, the rest is fed to it.
When the sender stops, shairport-sync stops writing, and the hub's 8-second
watchdog ends the session; the next sound starts a new one.

Findings behind the choices (docs/airplay.md, task report):

* Debian's shairport-sync advertises through avahi only and exits when no
  avahi daemon answers on the system D-Bus. The container starts its own
  dbus and avahi daemons as root (``docker-entrypoint.sh``); shairport-sync
  finds them on its own.
* Several instances run side by side with ``-a NAME -p PORT -o stdout`` and
  one shared configuration that gives them the same UDP range (each session
  takes three free ports from it), leaves the volume to the speakers
  (``ignore_volume_control``, or a sender at -20 dB arrives 43 dB down), and
  turns metadata off.
* Two instances with the same name collide on the network (only one is
  seen), and names past 50 bytes are cut, so names are cut here and made
  unique with " 2", " 3"...

The supervisor reconciles every 5 seconds and whenever the party list
changes, restarts a process that exits with backoff (1 s doubling to 30 s,
back to 1 s after a run of a minute), and is off, with a log line, where
shairport-sync is not installed.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from fastapi import APIRouter, FastAPI

from .live import NoTarget, Target

log = logging.getLogger(__name__)

router = APIRouter()

PORTS = range(5100, 5200)
#: Longest service name shairport-sync keeps, in bytes.
NAME_LIMIT = 50
PREFIX = "Sonora: "
PCM = ("s16le", 44100, 2)
FRAME = 4                      # bytes per s16le stereo frame
INTERVAL = 5.0                 # seconds between reconciles
BACKOFF_FIRST, BACKOFF_MAX = 1.0, 30.0
#: A process that ran this long was healthy; its next restart waits the least.
HEALTHY_RUN = 60.0
#: After a refused start (party gone, controller not up), sound is dropped
#: this long before trying again.
RETRY_START = 5.0
READ_SIZE = 16384

CONFIG = """\
// Written by Sonora (backend/home/airplay.py). The name, RTSP port and
// output come from the command line, one instance per receiver.
general = {
  udp_port_base = %d;
  udp_port_range = %d;
  mdns_backend = "avahi";
  output_backend = "stdout";
  ignore_volume_control = "yes";
};
metadata = {
  enabled = "no";
};
""" % (PORTS.start, len(PORTS))


class Process(Protocol):
    """A running receiver process, as the supervisor uses it."""

    async def read(self) -> bytes:
        """The next PCM bytes from stdout; ``b""`` once it has exited."""

    async def stop(self) -> None:
        """Stop it (idempotent)."""


Spawn = Callable[[list[str]], Awaitable[Process]]


# -- names ----------------------------------------------------------------------


def _cut(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore").rstrip()


def receiver_names(parties: list[dict], zones: dict[str, str]) -> dict[Target, str]:
    """What should be advertised: running parties (oldest first), then rooms
    (by name), each ``Sonora: <name>``, cut to fit and made unique."""
    wanted: list[tuple[Target, str]] = []
    for party in parties:
        if party.get("speakers"):
            wanted.append((("party", str(party["id"])), str(party.get("name") or "Party")))
    for uuid, name in sorted(zones.items(), key=lambda kv: (str(kv[1]).casefold(), kv[0])):
        wanted.append((("zone", str(uuid)), str(name or uuid)))
    names: dict[Target, str] = {}
    taken: set[str] = set()
    for target, label in wanted:
        base = _cut(PREFIX + " ".join(label.split()), NAME_LIMIT)
        name, n = base, 1
        while name.casefold() in taken:
            n += 1
            suffix = f" {n}"
            name = _cut(base, NAME_LIMIT - len(suffix.encode())) + suffix
        taken.add(name.casefold())
        names[target] = name
    return names


# -- one receiver -----------------------------------------------------------------


class Receiver:
    def __init__(self, target: Target, name: str, port: int) -> None:
        self.target, self.name, self.port = target, name, port
        self.task: asyncio.Task | None = None
        self.process: Process | None = None
        self.session: Any = None
        self.stopping = False
        self.restarts = 0
        self.delay = BACKOFF_FIRST
        #: Bytes read from the current process, for frame alignment.
        self.offset = 0
        self.hold_until = 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "target": {"kind": self.target[0], "id": self.target[1]},
                "port": self.port}


class AirPlaySupervisor:
    """Keeps one shairport-sync process per wanted receiver."""

    def __init__(self, *, executable: str, config_path: Path, spawn: Spawn,
                 parties: Callable[[], list[dict]], zones: Callable[[], dict[str, str]],
                 hub: Callable[[], Any], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval: float = INTERVAL) -> None:
        self.executable, self.config_path = executable, config_path
        self._spawn, self._parties, self._zones, self._hub = spawn, parties, zones, hub
        self._clock, self._sleep, self._interval = clock, sleep, interval
        self.receivers: dict[Target, Receiver] = {}
        self._poke = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._reconciling = asyncio.Lock()
        self._closed = False

    # -- what is wanted --------------------------------------------------------

    def _wanted(self) -> dict[Target, str]:
        try:
            parties = list(self._parties())
        except Exception:                                  # noqa: BLE001
            log.exception("airplay: reading the parties failed")
            parties = []
        try:
            zones = dict(self._zones())
        except Exception:                                  # noqa: BLE001
            log.exception("airplay: reading the rooms failed")
            zones = {}
        return receiver_names(parties, zones)

    def list(self) -> list[dict]:
        return [r.as_dict() for r in self.receivers.values()]

    def poke(self) -> None:
        """Reconcile soon (the party list changed)."""
        self._poke.set()

    async def reconcile(self) -> None:
        async with self._reconciling:
            if self._closed:
                return
            wanted = self._wanted()
            for target, rcv in list(self.receivers.items()):
                if wanted.get(target) != rcv.name:
                    del self.receivers[target]
                    await self._stop(rcv, "receiver_removed")
            used = {r.port for r in self.receivers.values()}
            for target, name in wanted.items():
                if target in self.receivers:
                    continue
                port = next((p for p in PORTS if p not in used), None)
                if port is None:
                    log.warning("airplay: no free port in %d-%d for %r", PORTS.start,
                                PORTS.stop - 1, name)
                    continue
                used.add(port)
                rcv = self.receivers[target] = Receiver(target, name, port)
                rcv.task = asyncio.create_task(self._run(rcv), name=f"airplay {name}")
                log.info("airplay: receiver %r on port %d", name, port)

    # -- the loop --------------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop(), name="airplay supervisor")

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await self.reconcile()
            except Exception:                              # noqa: BLE001
                log.exception("airplay: reconcile failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poke.wait(), self._interval)
            self._poke.clear()

    async def close(self) -> None:
        self._closed = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        async with self._reconciling:
            for rcv in list(self.receivers.values()):
                await self._stop(rcv, "shutdown")
            self.receivers.clear()

    # -- one process -----------------------------------------------------------

    def args(self, rcv: Receiver) -> list[str]:
        return [self.executable, "-c", str(self.config_path), "-a", rcv.name,
                "-p", str(rcv.port), "-o", "stdout"]

    async def _run(self, rcv: Receiver) -> None:
        while not rcv.stopping:
            began = self._clock()
            try:
                rcv.process = await self._spawn(self.args(rcv))
            except Exception:                              # noqa: BLE001
                log.exception("airplay: could not start %r", rcv.name)
                rcv.process = None
            if rcv.process is not None:
                rcv.offset = 0
                try:
                    await self._pump(rcv)
                finally:
                    await self._end_session(rcv, "disconnected")
                    await rcv.process.stop()
            if rcv.stopping:
                return
            if self._clock() - began >= HEALTHY_RUN:
                rcv.delay = BACKOFF_FIRST
            rcv.restarts += 1
            log.warning("airplay: %r stopped; restarting in %.0f s", rcv.name, rcv.delay)
            await self._sleep(rcv.delay)
            rcv.delay = min(rcv.delay * 2, BACKOFF_MAX)

    async def _pump(self, rcv: Receiver) -> None:
        while not rcv.stopping:
            chunk = await rcv.process.read()
            if not chunk:
                return
            at = rcv.offset
            rcv.offset += len(chunk)
            await self._feed(rcv, chunk, at)

    async def _feed(self, rcv: Receiver, chunk: bytes, at: int) -> None:
        if rcv.stopping:
            return
        session = rcv.session
        if session is None or session.ended:
            rcv.session = session = None
            # Only real sound starts a session: a sender left paused may keep
            # sending silence, which would restart it after every idle end.
            if not chunk.strip(b"\0") or self._clock() < rcv.hold_until:
                return
            chunk = chunk[(-at) % FRAME:]          # start on a whole frame
            try:
                session = await self._hub().start(target=rcv.target, source_name="AirPlay",
                                                  by="airplay", input_format=PCM)
            except NoTarget:
                log.info("airplay: %r has no party or room to play on", rcv.name)
                rcv.hold_until = self._clock() + RETRY_START
                return
            except Exception:                              # noqa: BLE001
                log.exception("airplay: starting live sound for %r failed", rcv.name)
                rcv.hold_until = self._clock() + RETRY_START
                return
            rcv.session = session
        await session.feed(chunk)

    async def _end_session(self, rcv: Receiver, reason: str) -> None:
        session, rcv.session = rcv.session, None
        if session is not None and not session.ended:
            try:
                await session.end(reason)
            except Exception:                              # noqa: BLE001
                log.exception("airplay: ending the session of %r failed", rcv.name)

    async def _stop(self, rcv: Receiver, reason: str) -> None:
        rcv.stopping = True
        await self._end_session(rcv, reason)
        if rcv.process is not None:
            await rcv.process.stop()
        if rcv.task is not None and rcv.task is not asyncio.current_task():
            rcv.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await rcv.task
        log.info("airplay: receiver %r removed", rcv.name)


# -- the real process -------------------------------------------------------------


class _Shairport:
    def __init__(self, proc: asyncio.subprocess.Process, name: str) -> None:
        self._proc, self._name = proc, name
        self._errors: collections.deque[str] = collections.deque(maxlen=8)
        self._stderr = asyncio.create_task(self._drain())
        self._stopped = False

    async def _drain(self) -> None:
        assert self._proc.stderr is not None
        async for line in self._proc.stderr:
            self._errors.append(line.decode("utf-8", "replace").rstrip())

    async def read(self) -> bytes:
        assert self._proc.stdout is not None
        data = await self._proc.stdout.read(READ_SIZE)
        if not data:
            code = await self._proc.wait()
            if not self._stopped:
                log.warning("airplay: shairport-sync for %r exited (%s): %s", self._name,
                            code, " | ".join(self._errors) or "no output")
        return data

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()
        self._stderr.cancel()


async def spawn_shairport(args: list[str]) -> Process:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    name = args[args.index("-a") + 1] if "-a" in args else "?"
    return _Shairport(proc, name)


# -- wiring -----------------------------------------------------------------------

#: The supervisor in use, if AirPlay is on.
_current: dict[str, AirPlaySupervisor] = {}


def current() -> AirPlaySupervisor | None:
    return _current.get("supervisor")


def _parties() -> list[dict]:
    from . import routes
    try:
        return routes.engine().list()
    except routes.Refused:
        return []


def _zones() -> dict[str, str]:
    from . import routes
    controller = routes._main().state.get("controller")
    zones = getattr(controller, "zones", None) or {}
    return {uuid: str(getattr(z, "name", "") or uuid) for uuid, z in list(zones.items())}


def _hub():
    from . import live_routes
    return live_routes.hub()


async def start_supervisor(*, which: Callable[[str], str | None] = shutil.which,
                           spawn: Spawn = spawn_shairport) -> AirPlaySupervisor | None:
    """Start the receivers, or say why not and leave AirPlay off."""
    executable = which("shairport-sync")
    if executable is None:
        log.info("airplay: shairport-sync is not installed; AirPlay receivers are off")
        return None
    config = Path(tempfile.gettempdir()) / "sonora-airplay.conf"
    try:
        config.write_text(CONFIG, encoding="utf-8")
    except OSError:
        log.exception("airplay: could not write %s; AirPlay receivers are off", config)
        return None
    supervisor = AirPlaySupervisor(executable=executable, config_path=config, spawn=spawn,
                                   parties=_parties, zones=_zones, hub=_hub)
    from . import routes
    routes.on_change(supervisor.poke)
    _current["supervisor"] = supervisor
    supervisor.start()
    log.info("airplay: receivers on, ports %d-%d", PORTS.start, PORTS.stop - 1)
    return supervisor


async def stop_supervisor() -> None:
    supervisor = _current.pop("supervisor", None)
    if supervisor is not None:
        from . import routes
        routes.off_change(supervisor.poke)
        await supervisor.close()


def attach(app: FastAPI) -> None:
    """Run the receivers for as long as the app runs.

    ``main.py`` hands FastAPI a lifespan, so router ``on_startup`` hooks would
    never run; this wraps that lifespan instead: the receivers start once the
    controller is up and stop (processes killed, sessions ended) before it
    stops."""
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        async with inner(app_) as value:
            try:
                await start_supervisor()
            except Exception:                              # noqa: BLE001
                log.exception("airplay: could not start the receivers")
            try:
                yield value
            finally:
                await stop_supervisor()

    app.router.lifespan_context = lifespan


@router.get("/api/airplay/receivers")
async def airplay_receivers():
    """The AirPlay names in use, for the help screen (empty when AirPlay is off)."""
    supervisor = current()
    return [{"name": r["name"], "target": r["target"]}
            for r in (supervisor.list() if supervisor is not None else [])]
