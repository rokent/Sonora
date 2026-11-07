"""Live sound hub: one encoder per live session, fanned out to speakers.

The encoder here is an identity pipe (what goes in comes out), the commands
are recorded, and the watchdog's clock is a number the test moves; nothing
runs ffmpeg or reaches a speaker.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend import main
from backend.config import settings
from backend.home import live, live_routes, permissions
from backend.home.parties import PartyEngine

from home_client import Client


class FakePipe:
    """Identity 'ffmpeg': bytes written come back out of ``read``."""

    def __init__(self, args):
        self.args = args
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    async def write(self, data: bytes) -> None:
        if not self.closed:
            await self.q.put(bytes(data))

    async def read(self) -> bytes:
        return await self.q.get()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            await self.q.put(b"")


class Spawner:
    def __init__(self):
        self.pipes: list[FakePipe] = []

    async def __call__(self, args):
        pipe = FakePipe(args)
        self.pipes.append(pipe)
        return pipe


class FakeCommands:
    def __init__(self):
        self.calls: list[tuple] = []

    async def play_now(self, zone, uri, metadata="", kind=""):
        self.calls.append(("play_now", zone, uri, metadata))

    async def seek_track(self, zone, index):
        self.calls.append(("seek_track", zone, index))

    async def seek_time(self, zone, position):
        self.calls.append(("seek_time", zone, position))

    async def play(self, zone):
        self.calls.append(("play", zone))

    async def set_source(self, zone, uri, metadata=""):
        self.calls.append(("set_source", zone, uri, metadata))

    def resumes(self):
        return [c for c in self.calls if c[0] != "play_now"]


def transport(track=3, rel="0:01:05", state="PLAYING", uri="x-rincon-queue:RINCON_A#0"):
    return SimpleNamespace(track_number=track, rel_time=rel, state=state,
                           media_uri=uri, position_read_at=0.0)


class FakeController:
    def __init__(self):
        self.zones = {"RINCON_A": SimpleNamespace(uuid="RINCON_A", host="10.0.0.5",
                                                  transport=transport())}

    def coordinator_of(self, uuid):
        return self.zones["RINCON_A"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def _never(_seconds):
    await asyncio.Event().wait()


def make_hub(parties=("p1",), analysis=None, members=None):
    """``members``: ``{room: party}`` for rooms in a running party."""
    clock, spawner, commands, ctl = Clock(), Spawner(), FakeCommands(), FakeController()
    running = set(parties)
    members = {} if members is None else members

    def resolve(target):
        kind, ident = target
        if kind == "party":
            return "RINCON_A" if ident in running else None
        return ident if ident in ctl.zones else None

    hub = live.LiveHub(controller=ctl, commands=commands, resolve=resolve,
                       base_url=lambda host: "http://10.0.0.2:8080", spawn=spawner,
                       clock=clock, sleep=_never, analysis=analysis,
                       party_of=lambda zone: members.get(zone))
    return SimpleNamespace(hub=hub, clock=clock, spawner=spawner, commands=commands,
                           ctl=ctl, running=running, members=members)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --- start ----------------------------------------------------------------


async def test_start_plays_the_live_url_as_radio():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="Ana's iPhone", by="airplay")
    [(name, zone, uri, didl)] = t.commands.calls
    assert (name, zone) == ("play_now", "RINCON_A")
    assert uri == f"x-rincon-mp3radio://10.0.0.2:8080/api/home/live/{s.key}.mp3"
    assert len(s.key) >= 22 and "/" not in s.key
    assert "<dc:title>Ana's iPhone</dc:title>" in didl
    assert "object.item.audioItem.audioBroadcast" in didl
    enc = t.spawner.pipes[0].args
    assert enc[enc.index("-i") - 6:enc.index("-i") + 2] == \
        ["-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0"]
    assert enc[-5:] == ["-f", "mp3", "-b:a", "320k", "pipe:1"]
    assert t.hub.session(s.key) is s
    await s.end("test")


async def test_start_converts_other_input_formats():
    t = make_hub()
    s = await t.hub.start(target=("zone", "RINCON_A"), source_name="PC", by="scream",
                          input_format=("s16le", 48000, 1))
    args = t.spawner.pipes[0].args
    i = args.index("-i")
    assert args[i - 6:i] == ["-f", "s16le", "-ar", "48000", "-ac", "1"]
    assert args[i + 1:].count("44100") == 1 and "-ac" in args[i + 1:]
    await s.end("test")


async def test_unknown_target_is_refused():
    t = make_hub()
    with pytest.raises(live.NoTarget):
        await t.hub.start(target=("party", "nope"), source_name="x", by="u")
    assert t.commands.calls == [] and t.spawner.pipes == []


# --- feeding and listening ------------------------------------------------


async def test_feed_reaches_every_listener():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    a, b = s.listen(), s.listen()
    first_a = asyncio.ensure_future(a.__anext__())
    first_b = asyncio.ensure_future(b.__anext__())
    await _settle()
    await s.feed(b"\x01\x02" * 100)
    assert await asyncio.wait_for(first_a, 1) == b"\x01\x02" * 100
    assert await asyncio.wait_for(first_b, 1) == b"\x01\x02" * 100
    await s.end("test")
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(a.__anext__(), 1)


async def test_a_late_listener_gets_at_most_64k_of_backlog():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    for _ in range(10):
        await s.feed(b"\x05" * 20000)
    await _settle()
    late = s.listen()
    got = await asyncio.wait_for(late.__anext__(), 1)
    assert 0 < len(got) <= 64 * 1024
    await s.end("test")


async def test_a_slow_listener_is_capped_not_unbounded():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    slow = s.listen()
    start = asyncio.ensure_future(slow.__anext__())
    await _settle()
    await s.feed(b"\x07")
    await asyncio.wait_for(start, 1)
    for _ in range(20):
        await s.feed(b"\x07" * 10000)
    await _settle()
    got = await asyncio.wait_for(slow.__anext__(), 1)
    assert len(got) <= 64 * 1024
    await s.end("test")


async def test_feed_after_end_is_ignored():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await s.end("test")
    await s.feed(b"\x01\x02")      # no error
    assert s.ended and t.hub.session(s.key) is None


# --- the watchdog and resuming --------------------------------------------


async def test_idle_8s_ends_and_resumes_the_queue_in_order():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.ctl.zones["RINCON_A"].transport = transport(uri=f"x-rincon-mp3radio://live/{s.key}")
    await s.feed(b"\x01\x00" * 50)
    t.clock.now += 7.9
    assert not await s.check_idle()
    t.clock.now += 0.2
    assert await s.check_idle()
    assert s.ended and s.reason == "idle"
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 3),
                                    ("seek_time", "RINCON_A", "0:01:05"),
                                    ("play", "RINCON_A")]
    assert t.hub.session(s.key) is None


async def test_digital_silence_counts_as_no_sound():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.clock.now += 5
    await s.feed(b"\x00" * 4096)
    t.clock.now += 3.5
    assert await s.check_idle()


async def test_near_silence_idles_out_and_resumes_the_queue():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.ctl.zones["RINCON_A"].transport = transport(uri=f"x-rincon-mp3radio://live/{s.key}")
    await s.feed(b"\x00\x10" * 64)                   # sound
    for _ in range(9):                                 # then +-2 LSB of noise
        t.clock.now += 1
        await s.feed(b"\x02\x00\xfe\xff\x01\x00\x00\x00" * 64)
    assert await s.check_idle()
    assert s.reason == "idle"
    assert [c[0] for c in t.commands.resumes()] == ["seek_track", "seek_time", "play"]


def test_is_quiet_thresholds_per_width():
    q = live.is_quiet
    assert q(b"", ("s16le", 44100, 2))
    assert q((16).to_bytes(2, "little", signed=True) * 8 + (-16).to_bytes(2, "little", signed=True) * 8,
             ("s16le", 44100, 2))
    assert not q((17).to_bytes(2, "little", signed=True) * 2, ("s16le", 44100, 2))
    assert not q((-17).to_bytes(2, "little", signed=True) * 2, ("s16le", 44100, 2))
    assert q((4096).to_bytes(3, "little", signed=True) * 4, ("s24le", 48000, 2))
    assert not q((4097).to_bytes(3, "little", signed=True) * 4, ("s24le", 48000, 2))
    assert not q((-4097).to_bytes(3, "little", signed=True) * 4, ("s24le", 48000, 2))
    assert q((16 << 16).to_bytes(4, "little", signed=True) * 2, ("s32le", 48000, 1))
    assert not q(((16 << 16) + 1).to_bytes(4, "little", signed=True) * 2, ("s32le", 48000, 1))
    assert not q(b"\x00\x00\x00\x10", ("s16le", 44100, 2))   # the right channel counts
    assert q(b"\x00" * 5, ("s16le", 44100, 2))                   # a partial frame is ignored
    assert q(b"\x00" * 8, ("f32le", 44100, 2)) and not q(b"\x01", ("f32le", 44100, 2))


def test_is_quiet_strides_through_long_chunks():
    loud = b"\x00\x40" * 2
    quiet = b"\x01\x00" * 2
    chunk = quiet * 90000 + loud * 10000      # a loud tenth at the end is seen
    assert not live.is_quiet(chunk, ("s16le", 44100, 2))
    assert live.is_quiet(quiet * 100000, ("s16le", 44100, 2))


async def test_the_watchdog_runs_by_itself():
    t = make_hub()
    t.hub._sleep = lambda _s: asyncio.sleep(0)
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.clock.now += 9
    for _ in range(50):
        if s.ended:
            break
        await asyncio.sleep(0)
    assert s.ended and s.reason == "idle"


async def test_disconnect_resumes_a_paused_queue_without_playing():
    t = make_hub()
    t.ctl.zones["RINCON_A"].transport = transport(track=7, rel="0:00:10", state="PAUSED_PLAYBACK")
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await s.end("disconnected")
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 7),
                                    ("seek_time", "RINCON_A", "0:00:10")]


async def test_position_of_a_playing_queue_is_advanced_to_now(monkeypatch):
    t = make_hub()
    tr = transport(rel="0:00:50")
    tr.position_read_at = 500.0
    t.ctl.zones["RINCON_A"].transport = tr
    monkeypatch.setattr(live.time, "monotonic", lambda: 512.4)
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await s.end("disconnected")
    assert ("seek_time", "RINCON_A", "0:01:02") in t.commands.calls


async def test_nothing_to_resume_when_nothing_was_playing():
    t = make_hub()
    t.ctl.zones["RINCON_A"].transport = transport(uri="x-sonosapi-stream:s1?sid=254",
                                                  state="PAUSED_PLAYBACK")
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await s.end("disconnected")
    assert t.commands.resumes() == []


@pytest.mark.parametrize("uri", ["x-sonosapi-stream:s1?sid=254", "x-rincon-stream:RINCON_B",
                                 "x-sonos-htastream:RINCON_A:spdif"])
async def test_a_playing_station_line_in_or_tv_comes_back_after_live(uri):
    t = make_hub()
    t.ctl.zones["RINCON_A"].transport = transport(uri=uri)
    t.ctl.zones["RINCON_A"].media_metadata = "<DIDL-Lite>station</DIDL-Lite>"
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.ctl.zones["RINCON_A"].transport = transport(uri=f"x-rincon-mp3radio://x/api/home/live/{s.key}.mp3")
    await s.end("idle")
    assert t.commands.resumes() == [("set_source", "RINCON_A", uri, "<DIDL-Lite>station</DIDL-Lite>"),
                                    ("play", "RINCON_A")]


async def test_live_sound_itself_or_a_group_member_is_not_a_resume_point():
    for uri in ("x-rincon-mp3radio://10.0.0.2:8080/api/home/live/abc.mp3", "x-rincon:RINCON_B"):
        t = make_hub()
        t.ctl.zones["RINCON_A"].transport = transport(uri=uri)
        s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
        await s.end("disconnected")
        assert t.commands.resumes() == [], uri


async def test_newer_session_replaces_older_and_resumes_once():
    t = make_hub()
    old = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    # now the coordinator plays the live stream: not what to come back to
    t.ctl.zones["RINCON_A"].transport = transport(track=1, rel="0:00:00",
                                                  uri=f"x-rincon-mp3radio://x/{old.key}")
    new = await t.hub.start(target=("party", "p1"), source_name="B", by="u")
    assert old.ended and old.reason == "replaced" and not new.ended
    assert t.hub.session(old.key) is None and t.hub.session(new.key) is new
    assert t.commands.resumes() == []
    await old.end("idle")           # a late end of the old one changes nothing
    assert t.commands.resumes() == []
    await new.end("idle")
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 3),
                                    ("seek_time", "RINCON_A", "0:01:05"),
                                    ("play", "RINCON_A")]
    await new.end("idle")
    assert len(t.commands.resumes()) == 3


async def test_party_end_ends_the_session_without_resume():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.running.discard("p1")
    await t.hub.end_for_party("p1")
    assert s.ended and s.reason == "party_ended"
    assert t.commands.resumes() == []
    t.clock.now += 20
    assert not await s.check_idle()
    assert t.commands.resumes() == []


async def test_a_party_gone_without_the_hook_is_not_resumed():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    t.running.discard("p1")
    await s.end("idle")
    assert t.commands.resumes() == []


async def test_engine_end_calls_its_listeners(tmp_path):
    ctl = SimpleNamespace(on_state=lambda f: None)
    engine = PartyEngine(tmp_path / "parties.json", ctl, object(), None, _async_noop)
    seen = []

    async def listener(party_id):
        seen.append(party_id)

    engine.on_end(listener)
    engine._parties["p9"] = _party("p9")
    await engine.end("p9")
    assert seen == ["p9"]


async def _async_noop(*_a):
    return None


def _party(pid):
    from backend.home.parties import Party
    return Party.from_dict({"id": pid, "name": "P", "speakers": [], "created": 1.0})


# --- one session per set of speakers ---------------------------------------


async def test_a_room_in_a_running_party_plays_on_the_party():
    t = make_hub(members={"RINCON_A": "p1"})
    party = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    room = await t.hub.start(target=("zone", "RINCON_A"), source_name="B", by="u")
    assert room.target == ("party", "p1")
    assert party.ended and party.reason == "replaced"
    assert t.hub.current(("zone", "RINCON_A")) is None
    assert t.hub.current(("party", "p1")) is room
    await room.end("idle")
    assert [c[0] for c in t.commands.resumes()] == ["seek_track", "seek_time", "play"]


async def test_a_session_on_the_same_coordinator_is_replaced_and_hands_over_its_point():
    t = make_hub()
    # RINCON_B is grouped with RINCON_A: its coordinator is RINCON_A.
    t.ctl.zones["RINCON_B"] = SimpleNamespace(uuid="RINCON_B", host="10.0.0.6",
                                              transport=transport(uri="x-rincon:RINCON_A"))
    first = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    t.ctl.zones["RINCON_A"].transport = transport(track=1, uri=f"x-rincon-mp3radio://x/{first.key}")
    second = await t.hub.start(target=("zone", "RINCON_B"), source_name="B", by="u")
    assert first.ended and first.reason == "replaced" and not second.ended
    assert t.hub.sessions() == [second]
    assert t.commands.resumes() == []
    await second.end("idle")
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 3),
                                    ("seek_time", "RINCON_A", "0:01:05"),
                                    ("play", "RINCON_A")]


async def test_a_room_joining_a_party_mid_live_ends_its_session_without_resume():
    t = make_hub()
    s = await t.hub.start(target=("zone", "RINCON_A"), source_name="A", by="u")
    t.hub.rooms_changed()                       # nothing changed for this room
    await _settle()
    assert not s.ended
    t.members["RINCON_A"] = "p1"
    t.hub.rooms_changed()
    await _settle()
    assert s.ended and s.reason == "joined_party"
    assert t.commands.resumes() == []
    assert t.hub.current(("zone", "RINCON_A")) is None


async def _grouped_hub():
    t = make_hub()
    t.ctl.zones["RINCON_B"] = SimpleNamespace(uuid="RINCON_B", host="10.0.0.6",
                                              transport=transport(uri="x-rincon:RINCON_A"))
    return t


async def test_a_pending_resume_does_not_cut_a_newer_session_on_the_same_coordinator():
    t = await _grouped_hub()
    a = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    t.ctl.zones["RINCON_A"].transport = transport(
        track=1, uri=f"x-rincon-mp3radio://10.0.0.2:8080/api/home/live/{a.key}.mp3")
    await t.hub._lock.acquire()                 # B's start gets the lock before A's resume
    b_start = asyncio.ensure_future(t.hub.start(target=("zone", "RINCON_B"), source_name="B", by="u"))
    await _settle()
    t.clock.now += 9
    a_end = asyncio.ensure_future(a.check_idle())   # A ends on silence: resume pending
    await _settle()
    assert a.ended and t.commands.resumes() == []
    t.hub._lock.release()
    b = await asyncio.wait_for(b_start, 1)
    await asyncio.wait_for(a_end, 1)
    await _settle()
    assert t.commands.resumes() == []           # nothing seeks under B's live sound
    assert not b.ended and t.hub.current(("zone", "RINCON_B")) is b
    assert t.hub._resume[("zone", "RINCON_B")]["track_no"] == 3
    await b.end("idle")
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 3),
                                    ("seek_time", "RINCON_A", "0:01:05"),
                                    ("play", "RINCON_A")]


async def test_a_pending_room_resume_is_skipped_when_the_room_joins_a_party():
    t = make_hub()
    s = await t.hub.start(target=("zone", "RINCON_A"), source_name="A", by="u")
    await t.hub._lock.acquire()
    t.clock.now += 9
    ending = asyncio.ensure_future(s.check_idle())
    await _settle()
    assert ("zone", "RINCON_A") in t.hub._resuming
    t.members["RINCON_A"] = "p1"
    t.hub.rooms_changed()
    t.hub._lock.release()
    await asyncio.wait_for(ending, 1)
    await _settle()
    assert t.commands.resumes() == []


async def test_live_routes_hub_follows_party_changes(monkeypatch):
    from backend.home import routes
    live_routes.reset()
    listeners = []
    monkeypatch.setattr(routes, "_change_listeners", listeners)
    monkeypatch.setattr(main, "state", {**main.state, "controller": FakeController(),
                                        "commands": FakeCommands()})
    monkeypatch.setattr(routes, "engine", lambda: (_ for _ in ()).throw(routes.Refused(503, "x")))
    found = live_routes.hub()
    assert listeners == [found.rooms_changed]
    assert live_routes.hub() is found and len(listeners) == 1
    live_routes.reset()
    assert listeners == []


def test_party_of_reads_the_running_party(monkeypatch):
    from backend.home import routes
    engine = SimpleNamespace(owner_of=lambda z: {"A": "p1", "B": "p2"}.get(z),
                             get=lambda p: {"p1": {"speakers": ["A"]}, "p2": {"speakers": []}}.get(p))
    monkeypatch.setattr(routes, "engine", lambda: engine)
    assert live_routes._party_of("A") == "p1"
    assert live_routes._party_of("B") is None      # a party with no speakers is not running
    assert live_routes._party_of("C") is None


# --- the analyzer ---------------------------------------------------------


async def test_party_sessions_feed_the_analyzer():
    got = {}

    def register(party_id, pcm):
        got["party"], got["pcm"] = party_id, pcm
        got["unregistered"] = False

        def unregister():
            got["unregistered"] = True
        return unregister

    t = make_hub(analysis=register)
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    assert got["party"] == "p1"
    args = t.spawner.pipes[1].args
    assert args[-7:] == ["-f", "s16le", "-ar", "22050", "-ac", "1", "pipe:1"]
    await s.feed(b"\x03\x04" * 10)
    chunk = await asyncio.wait_for(got["pcm"].__anext__(), 1)
    assert chunk == b"\x03\x04" * 10
    await s.end("test")
    assert got["unregistered"]


async def test_zone_sessions_do_not_feed_the_analyzer():
    calls = []
    t = make_hub(analysis=lambda p, pcm: calls.append(p) or (lambda: None))
    s = await t.hub.start(target=("zone", "RINCON_A"), source_name="x", by="u")
    assert calls == [] and len(t.spawner.pipes) == 1
    await s.end("test")


# --- the route --------------------------------------------------------------


@pytest.fixture
def app_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    t = make_hub()
    monkeypatch.setattr(live_routes, "hub", lambda: t.hub)
    return t


async def test_route_404_for_unknown_and_ended(app_hub):
    c = Client(main.app, host="sonora.local")
    assert (await c.request("GET", "/api/home/live/nope-nope-nope-nope-nope.mp3")).status == 404
    s = await app_hub.hub.start(target=("party", "p1"), source_name="x", by="u")
    await s.end("test")
    assert (await c.request("GET", f"/api/home/live/{s.key}.mp3")).status == 404


async def test_route_head_and_stream(app_hub):
    c = Client(main.app, host="sonora.local")
    s = await app_hub.hub.start(target=("party", "p1"), source_name="x", by="u")
    head = await c.request("HEAD", f"/api/home/live/{s.key}.mp3")
    assert head.status == 200
    hdrs = head.headers
    assert hdrs["content-type"] == "audio/mpeg"
    assert not any(k.startswith("icy-") for k in hdrs)

    async def later():
        await _settle()
        await s.feed(b"MP3DATA")
        await _settle()
        await s.end("test")

    task = asyncio.ensure_future(later())
    got = await asyncio.wait_for(c.request("GET", f"/api/home/live/{s.key}.mp3"), 2)
    await task
    assert got.status == 200 and got.body == b"MP3DATA"


async def test_at_most_16_listeners_per_session(app_hub):
    s = await app_hub.hub.start(target=("party", "p1"), source_name="x", by="u")
    streams = [s.listen() for _ in range(live.MAX_LISTENERS)]
    assert not s.has_room()
    c = Client(main.app, host="sonora.local")
    got = await asyncio.wait_for(c.request("GET", f"/api/home/live/{s.key}.mp3"), 2)
    assert got.status == 503
    assert (await c.request("HEAD", f"/api/home/live/{s.key}.mp3")).status == 200
    await s.end("test")
    del streams


def test_live_urls_are_public():
    assert permissions.allowed(None, "GET", "/api/home/live/abc.mp3")
    assert permissions.allowed(None, "HEAD", "/api/home/live/abc.mp3")
    assert not permissions.allowed(None, "POST", "/api/home/live/abc.mp3")


# --- review fixes -----------------------------------------------------------


async def test_a_listener_finishes_after_end_even_with_data_pending():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    gen = s.listen()
    await s.feed(b"\x09" * 100)
    await _settle()                  # the pump has pushed it; nobody read yet
    await s.end("test")
    got = []

    async def drain():
        async for chunk in gen:
            got.append(chunk)
    await asyncio.wait_for(drain(), 1)
    assert b"".join(got) == b"\x09" * 100


class Gate:
    def __init__(self):
        self.open = asyncio.Event()


async def test_a_start_waits_for_a_resume_under_way_and_reuses_its_point():
    t = make_hub()
    gate = Gate()
    plain_seek = t.commands.seek_track

    async def slow_seek(zone, index):
        await plain_seek(zone, index)
        await gate.open.wait()
    t.commands.seek_track = slow_seek

    s1 = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    # the transport model still shows the live stream (events lag)
    t.ctl.zones["RINCON_A"].transport = transport(track=1, uri=f"x-rincon-mp3radio://x/{s1.key}")
    t.clock.now += 9
    idle = asyncio.ensure_future(s1.check_idle())
    await _settle()
    assert [c[0] for c in t.commands.calls] == ["play_now", "seek_track"]
    second = asyncio.ensure_future(t.hub.start(target=("party", "p1"), source_name="B", by="u"))
    await _settle()
    assert [c[0] for c in t.commands.calls] == ["play_now", "seek_track"]   # start waits
    assert not second.done()
    gate.open.set()
    assert await asyncio.wait_for(idle, 1)
    s2 = await asyncio.wait_for(second, 1)
    assert [c[0] for c in t.commands.calls] == ["play_now", "seek_track", "seek_time", "play",
                                                "play_now"]
    point = t.hub._resume[("party", "p1")]
    assert (point["track_no"], point["position"], point["playing"]) == (3, "0:01:05", True)
    gate.open.set()
    await s2.end("disconnected")
    assert t.commands.calls[-3:] == [("seek_track", "RINCON_A", 3),
                                     ("seek_time", "RINCON_A", "0:01:05"),
                                     ("play", "RINCON_A")]


async def test_a_resume_queued_behind_a_start_hands_its_point_to_the_new_session():
    t = make_hub()
    s1 = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    point = t.hub._resume[("party", "p1")]
    async with t.hub._lock:                  # a start holds the lock ...
        t.hub._current[("party", "p1")] = s1  # (still current while it ends)
        del t.hub._resume[("party", "p1")]
        task = t.hub._schedule_resume(("party", "p1"), point)
        await _settle()
        assert t.commands.resumes() == []
    await asyncio.wait_for(task, 1)          # ... and a session is on: no seek
    assert t.commands.resumes() == []
    assert t.hub._resume[("party", "p1")] == point
    await s1.end("test")


async def test_a_failed_replacement_resumes_the_stranded_queue():
    t = make_hub()
    old = await t.hub.start(target=("party", "p1"), source_name="A", by="u")

    async def broken(args):
        raise RuntimeError("no ffmpeg")
    t.hub._spawn = broken
    with pytest.raises(RuntimeError):
        await t.hub.start(target=("party", "p1"), source_name="B", by="u")
    assert old.ended and old.reason == "replaced"
    for _ in range(20):
        if t.commands.resumes():
            break
        await asyncio.sleep(0)
    assert t.commands.resumes() == [("seek_track", "RINCON_A", 3),
                                    ("seek_time", "RINCON_A", "0:01:05"),
                                    ("play", "RINCON_A")]
    assert t.hub.current(("party", "p1")) is None


async def test_a_failed_play_now_resumes_after_the_start(monkeypatch):
    t = make_hub()
    old = await t.hub.start(target=("party", "p1"), source_name="A", by="u")

    async def refuse(*_a, **_k):
        raise OSError("speaker gone")
    t.commands.play_now = refuse
    with pytest.raises(OSError):
        await asyncio.wait_for(t.hub.start(target=("party", "p1"), source_name="B", by="u"), 1)
    for _ in range(20):
        if t.commands.resumes():
            break
        await asyncio.sleep(0)
    assert [c[0] for c in t.commands.resumes()] == ["seek_track", "seek_time", "play"]
    assert old.ended


async def test_an_encoder_that_will_not_stop_does_not_hold_end_up(monkeypatch):
    monkeypatch.setattr(live, "_CLOSE_TIMEOUT", 0.05)
    t = make_hub()

    class Stuck(FakePipe):
        async def close(self):
            await asyncio.Event().wait()

    async def spawn(args):
        return Stuck(args)
    t.hub._spawn = spawn
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await asyncio.wait_for(s.end("disconnected"), 1)
    assert s.ended


async def test_internal_ends_are_kept_referenced():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="x", by="u")
    await t.spawner.pipes[0].close()         # the encoder stops by itself
    await _settle()
    for _ in range(20):
        if s.ended:
            break
        await asyncio.sleep(0)
    assert s.ended and s.reason == "encoder_stopped"
    await _settle()                          # done callbacks run a tick later
    assert not s._ending                     # held while running, released after


async def test_party_end_during_a_pending_resume_with_a_concurrent_start():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    await t.hub._lock.acquire()          # the resume will have to wait for it
    ending = asyncio.ensure_future(s.end("disconnected"))
    await _settle()
    assert t.hub._resuming.get(("party", "p1")) is not None
    starting = asyncio.ensure_future(t.hub.start(target=("party", "p1"), source_name="B", by="u"))
    await _settle()
    assert not starting.done()
    t.running.discard("p1")
    await t.hub.end_for_party("p1")
    t.hub._lock.release()
    await asyncio.wait_for(ending, 1)    # returns cleanly, no CancelledError
    with pytest.raises(live.NoTarget):
        await asyncio.wait_for(starting, 1)
    assert t.commands.resumes() == []
    assert [c[0] for c in t.commands.calls] == ["play_now"]
    await _settle()
    assert not t.hub._resuming and not t.hub._skip


async def test_a_resume_skipped_by_party_end_skips_even_if_the_party_still_resolves():
    t = make_hub()
    s = await t.hub.start(target=("party", "p1"), source_name="A", by="u")
    await t.hub._lock.acquire()
    ending = asyncio.ensure_future(s.end("disconnected"))
    await _settle()
    await t.hub.end_for_party("p1")      # engine calls this before forgetting the party
    t.hub._lock.release()
    await asyncio.wait_for(ending, 1)
    assert t.commands.resumes() == []
