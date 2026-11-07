"""AirPlay receivers: the supervisor with fake processes and a fake hub.

Nothing runs shairport-sync: a process is a queue of stdout chunks the test
fills, and its exit is an empty chunk. Sleeps are recorded and return at once
unless a test holds them.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from backend import main
from backend.config import settings
from backend.home import airplay, routes
from backend.home.auth import SESSION_COOKIE, Principal, sign_session
from backend.home.live import NoTarget

from home_client import Client

TONE = b"\x10\x00\x20\x00" * 64          # non-silent s16le stereo
SILENCE = b"\0" * 256


class FakeProcess:
    def __init__(self, args):
        self.args = args
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.stopped = False

    def name(self):
        return self.args[self.args.index("-a") + 1]

    def port(self):
        return int(self.args[self.args.index("-p") + 1])

    async def read(self) -> bytes:
        return await self.out.get()

    def crash(self):
        self.out.put_nowait(b"")

    async def stop(self):
        if not self.stopped:
            self.stopped = True
            self.out.put_nowait(b"")


class Spawner:
    def __init__(self):
        self.procs: list[FakeProcess] = []
        self.fail = 0

    async def __call__(self, args):
        if self.fail:
            self.fail -= 1
            raise OSError("cannot start")
        proc = FakeProcess(args)
        self.procs.append(proc)
        return proc

    def live(self, name):
        found = [p for p in self.procs if p.name() == name and not p.stopped]
        assert len(found) == 1, (name, [(p.name(), p.stopped) for p in self.procs])
        return found[0]


class FakeSession:
    def __init__(self, target, kwargs):
        self.target, self.kwargs = target, kwargs
        self.fed: list[bytes] = []
        self.ended = False
        self.reason = None

    async def feed(self, pcm):
        if not self.ended:
            self.fed.append(pcm)

    async def end(self, reason):
        if not self.ended:
            self.ended, self.reason = True, reason


class FakeHub:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.refuse: set = set()

    async def start(self, *, target, **kwargs):
        if target in self.refuse:
            raise NoTarget(target)
        session = FakeSession(target, kwargs)
        self.sessions.append(session)
        return session


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Sleeper:
    """Records each wait; returns at once unless ``hold`` is set."""

    def __init__(self):
        self.waits: list[float] = []
        self.hold: asyncio.Event | None = None

    async def __call__(self, seconds):
        self.waits.append(seconds)
        if self.hold is not None:
            await self.hold.wait()
        await asyncio.sleep(0)


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def make(parties=None, zones=None, interval=airplay.INTERVAL):
    state = SimpleNamespace(
        parties=parties if parties is not None else [
            {"id": "p1", "name": "Garden Party", "speakers": ["RINCON_A"]}],
        zones=zones if zones is not None else {"RINCON_A": "Kitchen", "RINCON_B": "Den"})
    spawner, hub, clock, sleeper = Spawner(), FakeHub(), Clock(), Sleeper()
    sup = airplay.AirPlaySupervisor(
        executable="/usr/bin/shairport-sync", config_path=Path("/tmp/x.conf"), spawn=spawner,
        parties=lambda: state.parties, zones=lambda: state.zones, hub=lambda: hub,
        clock=clock, sleep=sleeper, interval=interval)
    return SimpleNamespace(sup=sup, state=state, spawner=spawner, hub=hub, clock=clock,
                           sleeper=sleeper)


# -- names ---------------------------------------------------------------------------


def test_names_cover_running_parties_and_every_room():
    names = airplay.receiver_names(
        [{"id": "p1", "name": "Garden Party", "speakers": ["A"]},
         {"id": "p2", "name": "Ended", "speakers": []}],
        {"B": "Den", "A": "Kitchen"})
    assert names == {("party", "p1"): "Sonora: Garden Party",
                     ("zone", "B"): "Sonora: Den", ("zone", "A"): "Sonora: Kitchen"}


def test_same_names_are_made_unique():
    names = airplay.receiver_names([{"id": "p1", "name": "Kitchen", "speakers": ["A"]}],
                                   {"A": "Kitchen", "B": "kitchen"})
    assert names[("party", "p1")] == "Sonora: Kitchen"
    assert sorted(names.values()) == ["Sonora: Kitchen", "Sonora: Kitchen 2",
                                      "Sonora: kitchen 3"]


def test_long_names_are_cut_to_fifty_bytes_on_a_character():
    long = "Sufragerie și bucătărie, cu toată familia adunată"
    names = airplay.receiver_names([], {"A": long, "B": long})
    for name in names.values():
        assert len(name.encode()) <= airplay.NAME_LIMIT
        name.encode().decode()
    assert len(set(names.values())) == 2
    assert names[("zone", "B")].endswith(" 2")


# -- reconcile -----------------------------------------------------------------------


async def test_reconcile_starts_one_process_per_receiver_on_distinct_ports():
    t = make()
    await t.sup.reconcile()
    await settle()
    procs = {p.name(): p for p in t.spawner.procs}
    assert set(procs) == {"Sonora: Garden Party", "Sonora: Den", "Sonora: Kitchen"}
    assert len({p.port() for p in procs.values()}) == 3
    assert all(p.port() in airplay.PORTS for p in procs.values())
    args = procs["Sonora: Den"].args
    assert args[:3] == ["/usr/bin/shairport-sync", "-c", "/tmp/x.conf"]
    assert args[-2:] == ["-o", "stdout"]
    assert sorted(r["name"] for r in t.sup.list()) == sorted(procs)
    await t.sup.close()


async def test_reconcile_adds_and_removes_receivers_as_parties_change():
    t = make()
    await t.sup.reconcile()
    await settle()
    garden = t.spawner.live("Sonora: Garden Party")
    t.state.parties = [{"id": "p2", "name": "Late Night", "speakers": ["RINCON_B"]}]
    await t.sup.reconcile()
    await settle()
    assert garden.stopped
    t.spawner.live("Sonora: Late Night")
    assert ("party", "p1") not in t.sup.receivers
    assert garden.port() == t.sup.receivers[("party", "p2")].port   # port reused
    # nothing else restarted
    assert len(t.spawner.procs) == 4
    await t.sup.close()


async def test_a_renamed_room_gets_a_new_receiver():
    t = make(parties=[])
    await t.sup.reconcile()
    await settle()
    den = t.spawner.live("Sonora: Den")
    t.state.zones = {"RINCON_A": "Kitchen", "RINCON_B": "Study"}
    await t.sup.reconcile()
    await settle()
    assert den.stopped
    t.spawner.live("Sonora: Study")
    await t.sup.close()


async def test_a_party_change_reconciles_without_waiting_for_the_interval():
    t = make(interval=3600)
    t.sup.start()
    await settle()
    assert len(t.spawner.procs) == 3
    t.state.parties = []
    t.sup.poke()
    await settle()
    assert t.spawner.live("Sonora: Den")
    assert [p.name() for p in t.spawner.procs if p.stopped] == ["Sonora: Garden Party"]
    await t.sup.close()


async def test_the_loop_reconciles_every_interval():
    t = make(interval=0.01)
    t.sup.start()
    await settle()
    t.state.zones = {"RINCON_A": "Kitchen"}
    await asyncio.sleep(0.05)
    assert ("zone", "RINCON_B") not in t.sup.receivers
    await t.sup.close()
    assert airplay.INTERVAL == 5.0


async def test_routes_changed_pokes_listeners(monkeypatch):
    calls = []

    async def fan_out(message):
        calls.append(message)

    monkeypatch.setattr(main, "_fan_out", fan_out)
    poked = []
    routes.on_change(lambda: poked.append(1))
    try:
        await routes.changed()
    finally:
        routes._change_listeners.clear()
    assert calls == [{"type": "parties"}] and poked == [1]


# -- crashes ---------------------------------------------------------------------------


async def test_a_crashed_process_is_restarted_with_backoff():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    await settle()
    for _ in range(7):
        t.spawner.live("Sonora: Kitchen").crash()
        await settle()
    assert t.sleeper.waits == [1, 2, 4, 8, 16, 30, 30]
    assert len(t.spawner.procs) == 8
    assert t.sup.receivers[("zone", "RINCON_A")].restarts == 7
    await t.sup.close()


async def test_backoff_starts_over_after_a_healthy_run():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    await settle()
    for _ in range(3):
        t.spawner.live("Sonora: Kitchen").crash()
        await settle()
    t.clock.now += airplay.HEALTHY_RUN
    t.spawner.live("Sonora: Kitchen").crash()
    await settle()
    assert t.sleeper.waits == [1, 2, 4, 1]
    await t.sup.close()


async def test_a_process_that_will_not_start_is_retried_with_backoff():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    t.spawner.fail = 2
    await t.sup.reconcile()
    await settle()
    assert t.sleeper.waits == [1, 2]
    t.spawner.live("Sonora: Kitchen")
    await t.sup.close()


async def test_a_crash_while_sending_ends_the_session():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    await settle()
    proc = t.spawner.live("Sonora: Kitchen")
    proc.out.put_nowait(TONE)
    await settle()
    proc.crash()
    await settle()
    assert t.hub.sessions[0].reason == "disconnected"
    await t.sup.close()


async def test_a_receiver_removed_while_restarting_stays_down():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    t.sleeper.hold = asyncio.Event()
    await t.sup.reconcile()
    await settle()
    t.spawner.live("Sonora: Kitchen").crash()
    await settle()
    t.state.zones = {}
    await t.sup.reconcile()
    t.sleeper.hold.set()
    await settle()
    assert len(t.spawner.procs) == 1 and not t.sup.receivers
    await t.sup.close()


# -- sound -----------------------------------------------------------------------------


async def test_first_sound_starts_a_session_on_the_receivers_target():
    t = make()
    await t.sup.reconcile()
    await settle()
    t.spawner.live("Sonora: Garden Party").out.put_nowait(TONE)
    t.spawner.live("Sonora: Den").out.put_nowait(TONE)
    await settle()
    by_target = {s.target: s for s in t.hub.sessions}
    assert set(by_target) == {("party", "p1"), ("zone", "RINCON_B")}
    party = by_target[("party", "p1")]
    assert party.kwargs == {"source_name": "AirPlay", "by": "airplay",
                            "input_format": ("s16le", 44100, 2)}
    assert party.fed == [TONE]
    t.spawner.live("Sonora: Garden Party").out.put_nowait(SILENCE)
    await settle()
    assert party.fed == [TONE, SILENCE]                       # same session, fed on
    assert len(t.hub.sessions) == 2
    await t.sup.close()


async def test_silence_alone_does_not_start_a_session():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    await settle()
    t.spawner.live("Sonora: Kitchen").out.put_nowait(SILENCE)
    await settle()
    assert t.hub.sessions == []
    await t.sup.close()


async def test_sound_after_an_ended_session_starts_a_new_one_on_a_whole_frame():
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    await settle()
    proc = t.spawner.live("Sonora: Kitchen")
    proc.out.put_nowait(TONE + b"\x01\x02")         # 2 bytes into the next frame
    await settle()
    first = t.hub.sessions[0]
    await first.end("idle")                          # the hub's watchdog
    proc.out.put_nowait(b"\x03\x04" + TONE)
    await settle()
    second = t.hub.sessions[1]
    assert second.target == ("zone", "RINCON_A")
    assert second.fed == [TONE]                      # the partial frame dropped
    await t.sup.close()


async def test_a_refused_start_waits_before_trying_again():
    t = make()
    t.hub.refuse.add(("party", "p1"))
    await t.sup.reconcile()
    await settle()
    proc = t.spawner.live("Sonora: Garden Party")
    proc.out.put_nowait(TONE)
    proc.out.put_nowait(TONE)
    await settle()
    assert t.hub.sessions == []
    t.hub.refuse.clear()
    proc.out.put_nowait(TONE)
    await settle()
    assert t.hub.sessions == []
    t.clock.now += airplay.RETRY_START
    proc.out.put_nowait(TONE)
    await settle()
    assert [s.target for s in t.hub.sessions] == [("party", "p1")]
    await t.sup.close()


async def test_removing_a_receiver_ends_its_session_and_close_ends_the_rest():
    t = make()
    await t.sup.reconcile()
    await settle()
    t.spawner.live("Sonora: Den").out.put_nowait(TONE)
    t.spawner.live("Sonora: Kitchen").out.put_nowait(TONE)
    await settle()
    den, kitchen = sorted(t.hub.sessions, key=lambda s: s.target[1], reverse=True)
    t.state.zones = {"RINCON_A": "Kitchen"}
    await t.sup.reconcile()
    await settle()
    assert den.reason == "receiver_removed"
    await t.sup.close()
    assert kitchen.reason == "shutdown"
    assert all(p.stopped for p in t.spawner.procs) and not t.sup.receivers


# -- wiring -------------------------------------------------------------------------------


async def test_off_without_shairport_sync(caplog):
    caplog.set_level(logging.INFO, logger="backend.home.airplay")
    assert await airplay.start_supervisor(which=lambda _name: None) is None
    assert "shairport-sync is not installed" in caplog.text
    assert airplay.current() is None


async def test_start_and_stop_supervisor(monkeypatch, tmp_path):
    monkeypatch.setattr(airplay.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(airplay, "_parties", lambda: [])
    monkeypatch.setattr(airplay, "_zones", lambda: {"RINCON_A": "Kitchen"})
    spawner = Spawner()
    sup = await airplay.start_supervisor(which=lambda _n: "/usr/bin/shairport-sync",
                                         spawn=spawner)
    try:
        await settle()
        assert airplay.current() is sup
        assert sup.poke in routes._change_listeners
        conf = (tmp_path / "sonora-airplay.conf").read_text()
        assert 'output_backend = "stdout"' in conf and 'ignore_volume_control = "yes"' in conf
        assert "udp_port_base = 5100" in conf and "udp_port_range = 100" in conf
        assert spawner.live("Sonora: Kitchen")
    finally:
        await airplay.stop_supervisor()
    assert airplay.current() is None and sup.poke not in routes._change_listeners
    assert all(p.stopped for p in spawner.procs)


async def test_attach_runs_the_supervisor_inside_the_apps_lifespan(monkeypatch):
    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("app up")
        yield
        events.append("app down")

    async def fake_start():
        events.append("airplay up")

    async def fake_stop():
        events.append("airplay down")

    monkeypatch.setattr(airplay, "start_supervisor", fake_start)
    monkeypatch.setattr(airplay, "stop_supervisor", fake_stop)
    app = FastAPI(lifespan=lifespan)
    airplay.attach(app)
    async with app.router.lifespan_context(app):
        events.append("serving")
    assert events == ["app up", "airplay up", "serving", "airplay down", "app down"]


async def test_receivers_route_for_users_and_guests(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    guest = Client(main.app, host="sonora.local")
    guest.cookies[SESSION_COOKIE] = sign_session(Principal("guest", "guest:a", "Ana"), 3600)
    r = await guest.request("GET", "/api/airplay/receivers")
    assert r.status == 200 and r.json() == []
    t = make(parties=[], zones={"RINCON_A": "Kitchen"})
    await t.sup.reconcile()
    monkeypatch.setitem(airplay._current, "supervisor", t.sup)
    try:
        r = await guest.request("GET", "/api/airplay/receivers")
        assert r.json() == [{"name": "Sonora: Kitchen",
                              "target": {"kind": "zone", "id": "RINCON_A"}}]
        anon = await Client(main.app, host="sonora.local").request("GET", "/api/airplay/receivers")
        assert anon.status == 401
    finally:
        await t.sup.close()
