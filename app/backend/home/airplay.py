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
back to 1 s after a run of a minute; given up on after 5 exits in a row each
within 10 s of starting; moved to another port when its own is taken). Rooms
that are offline or in a running party get no receiver. It is off, with one
log line, when ``SONORA_AIRPLAY=0``, when shairport-sync is not installed, or
when no avahi daemon answers on the system D-Bus.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import shutil
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from fastapi import APIRouter, FastAPI

from .live import HOLD_REASONS, HOLD_SECONDS, NoTarget, Target, is_quiet

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
#: A receiver that exits this many times in a row, each within FAST_EXIT
#: seconds of starting, is given up on (logged once) until it is removed.
MAX_FAST_EXITS, FAST_EXIT = 5, 10.0
READ_SIZE = 16384
#: ``SONORA_AIRPLAY`` values that turn the receivers off.
OFF = {"0", "false", "no", "off"}

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


def held_rooms(parties: list[dict]) -> set[str]:
    """The rooms (zone uuids) in a running party."""
    return {str(z) for p in parties if p.get("speakers") for z in p["speakers"]}


def receiver_names(parties: list[dict], zones: dict[str, str]) -> dict[Target, str]:
    """What should be advertised: running parties (oldest first), then rooms
    (by name) that are not in a running party (the party's receiver covers
    them), each ``Sonora: <name>``, cut to fit and made unique. ``zones`` is
    the reachable rooms only."""
    wanted: list[tuple[Target, str]] = []
    for party in parties:
        if party.get("speakers"):
            wanted.append((("party", str(party["id"])), str(party.get("name") or "Party")))
    held = held_rooms(parties)
    for uuid, name in sorted(zones.items(), key=lambda kv: (str(kv[1]).casefold(), kv[0])):
        if str(uuid) not in held:
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
        self.fast_exits = 0
        #: Given up on after MAX_FAST_EXITS fast exits; kept (not restarted)
        #: until reconcile removes it.
        self.failed = False
        #: Bytes read from the current process, for frame alignment.
        self.offset = 0
        self.hold_until = 0.0
        #: Its session was taken over by a newer sender ("replaced"): sound
        #: is dropped until this receiver's sender has been quiet for
        #: HOLD_SECONDS (or a new process starts), so the newer one keeps
        #: the speakers instead of the two taking turns.
        self.held = False
        self.last_sound = 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "target": {"kind": self.target[0], "id": self.target[1]},
                "port": self.port}


class AirPlaySupervisor:
    """Keeps one shairport-sync process per wanted receiver."""

    def __init__(self, *, executable: str, config_path: Path, spawn: Spawn,
                 parties: Callable[[], list[dict]], zones: Callable[[], dict[str, str]],
                 hub: Callable[[], Any], clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval: float = INTERVAL,
                 port_free: Callable[[int], bool] | None = None) -> None:
        self.executable, self.config_path = executable, config_path
        self._port_free = port_free or port_is_free
        self._spawn, self._parties, self._zones, self._hub = spawn, parties, zones, hub
        self._clock, self._sleep, self._interval = clock, sleep, interval
        self.receivers: dict[Target, Receiver] = {}
        self._poke = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._reconciling = asyncio.Lock()
        self._closed = False

    # -- what is wanted --------------------------------------------------------

    def _wanted(self) -> tuple[dict[Target, str], set[str]]:
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
        return receiver_names(parties, zones), held_rooms(parties)

    def list(self) -> list[dict]:
        """The receivers that are up (not given up on)."""
        return [r.as_dict() for r in self.receivers.values() if not r.failed]

    def _free_port(self, rcv: Receiver | None = None) -> int | None:
        """The lowest port in the range no other receiver holds and nothing
        else on this machine is listening on."""
        used = {r.port for r in self.receivers.values() if r is not rcv}
        return next((p for p in PORTS if p not in used and self._port_free(p)), None)

    def poke(self) -> None:
        """Reconcile soon (the party list changed)."""
        self._poke.set()

    async def reconcile(self) -> None:
        async with self._reconciling:
            if self._closed:
                return
            wanted, held = self._wanted()
            for target, rcv in list(self.receivers.items()):
                if wanted.get(target) != rcv.name:
                    del self.receivers[target]
                    # A room taken into a party is the party's now: its live
                    # sound ends without seeking the room's old queue.
                    taken = target[0] == "zone" and target[1] in held
                    await self._stop(rcv, "shutdown" if taken else "receiver_removed")
            for target, name in wanted.items():
                if target in self.receivers:
                    continue
                port = self._free_port()
                if port is None:
                    log.warning("airplay: no free port in %d-%d for %r", PORTS.start,
                                PORTS.stop - 1, name)
                    continue
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
            await asyncio.gather(*(self._stop(rcv, "shutdown")
                                   for rcv in list(self.receivers.values())))
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
                rcv.held = False
                try:
                    await self._pump(rcv)
                except Exception:                          # noqa: BLE001
                    log.exception("airplay: reading from %r failed", rcv.name)
                finally:
                    await self._end_session(rcv, "disconnected")
                    try:
                        await rcv.process.stop()
                    except Exception:                      # noqa: BLE001
                        log.exception("airplay: stopping %r failed", rcv.name)
            if rcv.stopping:
                return
            ran = self._clock() - began
            if ran >= HEALTHY_RUN:
                rcv.delay = BACKOFF_FIRST
            rcv.fast_exits = rcv.fast_exits + 1 if ran < FAST_EXIT else 0
            if rcv.fast_exits >= MAX_FAST_EXITS:
                rcv.failed = True
                log.error("airplay: %r exited %d times in a row within %.0f s of starting; "
                          "not restarting it", rcv.name, rcv.fast_exits, FAST_EXIT)
                return
            rcv.restarts += 1
            log.warning("airplay: %r stopped; restarting in %.0f s", rcv.name, rcv.delay)
            await self._sleep(rcv.delay)
            rcv.delay = min(rcv.delay * 2, BACKOFF_MAX)
            if not rcv.stopping and not self._port_free(rcv.port):
                port = self._free_port(rcv)
                if port is not None:
                    log.warning("airplay: port %d is taken; %r moves to %d",
                                rcv.port, rcv.name, port)
                    rcv.port = port

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
            if session is not None and session.reason in HOLD_REASONS and not rcv.held:
                rcv.held, rcv.last_sound = True, self._clock()
            rcv.session = session = None
            # Only real sound starts a session: a sender left paused may keep
            # sending silence, which would restart it after every idle end.
            if is_quiet(chunk[(-at) % FRAME:], PCM) or self._clock() < rcv.hold_until:
                return
            if rcv.held:
                now, quiet_for = self._clock(), self._clock() - rcv.last_sound
                rcv.last_sound = now
                if quiet_for < HOLD_SECONDS:
                    return
                rcv.held = False
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
            try:
                await rcv.process.stop()
            except Exception:                              # noqa: BLE001
                log.exception("airplay: stopping %r failed", rcv.name)
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


def port_is_free(port: int) -> bool:
    """Whether nothing listens on TCP ``port`` (shairport-sync's RTSP)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError:
            return False
    return True


BUS_SOCKETS = (Path("/run/dbus/system_bus_socket"), Path("/var/run/dbus/system_bus_socket"))


async def avahi_available(*, tries: int = 3, pause: float = 1.0, timeout: float = 5.0) -> bool:
    """Whether an avahi daemon answers on the system D-Bus. shairport-sync
    cannot announce (and exits at once) without one."""
    if not os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") and not any(p.exists() for p in BUS_SOCKETS):
        return False
    send = shutil.which("dbus-send")
    if send is None:
        return True                    # a bus is there; nothing to ask avahi with
    for attempt in range(tries):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                send, "--system", "--print-reply", "--reply-timeout=2000",
                "--dest=org.freedesktop.Avahi", "/",
                "org.freedesktop.Avahi.Server.GetVersionString",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(proc.wait(), timeout) == 0:
                return True
        except TimeoutError:          # before OSError: it is one
            # A dbus-send that hangs is killed, not left behind.
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        except OSError:
            pass
        if attempt + 1 < tries:
            await asyncio.sleep(pause)
    return False


def enabled() -> bool:
    return os.environ.get("SONORA_AIRPLAY", "").strip().lower() not in OFF


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
    # Unreachable rooms get no receiver: a sender would pick a dead name.
    return {uuid: str(getattr(z, "name", "") or uuid) for uuid, z in list(zones.items())
            if getattr(z, "online", True)}


def _hub():
    from . import live_routes
    return live_routes.hub()


async def start_supervisor(*, which: Callable[[str], str | None] = shutil.which,
                           spawn: Spawn = spawn_shairport,
                           probe: Callable[[], Awaitable[bool]] = avahi_available,
                           ) -> AirPlaySupervisor | None:
    """Start the receivers, or say why not (once) and leave AirPlay off."""
    if not enabled():
        log.info("airplay: AirPlay off (SONORA_AIRPLAY=%s)", os.environ.get("SONORA_AIRPLAY"))
        return None
    executable = which("shairport-sync")
    if executable is None:
        log.info("airplay: shairport-sync is not installed; AirPlay receivers are off")
        return None
    if not await probe():
        log.warning("airplay: AirPlay off: no mDNS (avahi) available")
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
