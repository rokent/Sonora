"""Server-side audio analysis for TV mode's visualizer (``home/analysis.py``).

No real ffmpeg: every test hands the analyzer a fake process factory whose
stdout the test feeds by hand, a fake speaker probe, and a fake clock, so the
seek / skip / idle-stop rules run in milliseconds."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest

from backend import main
from backend.config import settings
from backend.home import analysis
from backend.home.analysis import (FRAME_SAMPLES, HOP_SAMPLES, RATE, Analyzer,
                                   Playing)
from backend.home.auth import SESSION_COOKIE, Principal, sign_session

from home_client import Client

URI = "http://192.168.1.5:8000/api/home/media/AAAAAAAAAAAAAAAAAAAAAA.m4a"
URI2 = "http://192.168.1.5:8000/api/home/media/BBBBBBBBBBBBBBBBBBBBBB.m4a"


# --- fakes ---------------------------------------------------------------

class FakeStdout:
    def __init__(self):
        self.buf = bytearray()
        self.eof = False
        self.changed = asyncio.Event()

    def feed(self, data: bytes) -> None:
        self.buf += data
        self.changed.set()

    def end(self) -> None:
        self.eof = True
        self.changed.set()

    async def readexactly(self, n: int) -> bytes:
        while len(self.buf) < n and not self.eof:
            self.changed.clear()
            await self.changed.wait()
        if len(self.buf) < n:
            partial = bytes(self.buf)
            self.buf.clear()
            raise asyncio.IncompleteReadError(partial, n)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


class FakeProc:
    def __init__(self, args):
        self.args = list(args)
        self.stdout = FakeStdout()
        self.killed = False
        self.returncode = None

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.end()

    async def wait(self):
        return self.returncode


class Spawner:
    def __init__(self):
        self.procs: list[FakeProc] = []

    async def __call__(self, args):
        proc = FakeProc(args)
        self.procs.append(proc)
        return proc


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class World:
    """The speaker side: what each party plays, where, and what resolves."""

    def __init__(self):
        self.playing: dict[str, Playing] = {}
        self.resolved: list[str] = []

    def probe(self, party_id):
        return self.playing.get(party_id)

    async def resolve(self, uri):
        self.resolved.append(uri)
        if analysis.media_key(uri) is None:
            return None
        return f"https://upstream.test/{analysis.media_key(uri)}", {"User-Agent": "UA"}


def make(world=None, **kw):
    world = world or World()
    spawn, clock = Spawner(), Clock()
    an = Analyzer(ffmpeg="/usr/bin/ffmpeg", probe=world.probe, resolve=world.resolve,
                  spawn=spawn, clock=clock, tick=0.001, **kw)
    return an, world, spawn, clock


async def until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0.001)


def hop(fill: int) -> bytes:
    """One hop of u8 samples, its last 512 tagged so a frame can be told apart."""
    return bytes([fill]) * (HOP_SAMPLES - FRAME_SAMPLES) + bytes([fill + 1]) * FRAME_SAMPLES


def ss_of(args) -> float:
    return float(args[args.index("-ss") + 1])


# --- pure helpers ----------------------------------------------------------

def test_ffmpeg_args_follow_the_brief():
    args = analysis.ffmpeg_args("/usr/bin/ffmpeg", "https://u.test/a",
                                {"User-Agent": "UA", "Cookie": "c=1"}, 12.5)
    assert args[:2] == ["/usr/bin/ffmpeg", "-nostdin"]
    assert args[args.index("-headers") + 1] == "User-Agent: UA\r\nCookie: c=1\r\n"
    tail = args[args.index("-ss"):]
    assert tail == ["-ss", "12.500", "-re", "-i", "https://u.test/a",
                    "-f", "u8", "-ac", "1", "-ar", "22050", "pipe:1"]
    # input options must come before -i
    assert args.index("-headers") < args.index("-i")
    assert args[args.index("-protocol_whitelist") + 1] == "https,tls,tcp,http"
    assert args.index("-protocol_whitelist") < args.index("-i")
    no_headers = analysis.ffmpeg_args("ffmpeg", "https://u.test/a", {}, 0)
    assert "-headers" not in no_headers


def test_frame_budget():
    assert RATE == 22050 and FRAME_SAMPLES == 512
    # <= 30 frames per second, 512 bytes each
    assert RATE / HOP_SAMPLES <= 30
    assert HOP_SAMPLES >= FRAME_SAMPLES


def test_media_key():
    assert analysis.media_key(URI) == "AAAAAAAAAAAAAAAAAAAAAA"
    assert analysis.media_key("x-sonos-spotify:spotify%3atrack%3a1?sid=9") is None
    assert analysis.media_key("http://h/api/home/media/short.m4a") is None
    assert analysis.media_key("") is None


def test_s16le_to_u8():
    import struct
    pcm = struct.pack("<4h", 0, -32768, 32767, 256)
    assert analysis.u8_from_s16le(pcm) == bytes([128, 0, 255, 129])


def test_visuals_mode(monkeypatch):
    monkeypatch.setattr(analysis.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert analysis.visuals_mode() == "exact"
    monkeypatch.setattr(analysis.shutil, "which", lambda name: None)
    assert analysis.visuals_mode() == "approximate"


# --- frames ------------------------------------------------------------------

async def test_frames_sliced_and_stamped():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 30.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    proc = spawn.procs[0]
    assert ss_of(proc.args) == 30.0
    assert proc.args[proc.args.index("-i") + 1] == "https://upstream.test/AAAAAAAAAAAAAAAAAAAAAA"
    proc.stdout.feed(hop(10) + hop(20))
    f1 = await asyncio.wait_for(sub.get(), 1)
    f2 = await asyncio.wait_for(sub.get(), 1)
    assert set(f1) == {"t", "w"}
    assert f1["t"] == round(30.0 + (HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3)
    assert f2["t"] == round(30.0 + (2 * HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3)
    assert base64.b64decode(f1["w"]) == bytes([11]) * FRAME_SAMPLES
    assert base64.b64decode(f2["w"]) == bytes([21]) * FRAME_SAMPLES
    an.unsubscribe(sub)
    await an.close()
    assert proc.killed


async def test_every_subscriber_gets_frames():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 0.0, True)
    a, b = an.subscribe("p1"), an.subscribe("p1")
    await until(lambda: spawn.procs)
    assert len(spawn.procs) == 1          # one ffmpeg per party, not per viewer
    spawn.procs[0].stdout.feed(hop(1))
    assert (await asyncio.wait_for(a.get(), 1)) == (await asyncio.wait_for(b.get(), 1))
    await an.close()


async def test_slow_subscriber_drops_oldest():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 0.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    for i in range(40):
        spawn.procs[0].stdout.feed(hop(i * 2))
    await until(lambda: sub.queue.full())
    await settle()
    frames = []
    while not sub.queue.empty():
        frames.append(sub.queue.get_nowait())
    assert len(frames) == analysis.QUEUE_FRAMES
    assert base64.b64decode(frames[-1]["w"])[0] == 79   # the newest kept
    await an.close()


# --- seek / skip (Review Focus #2) --------------------------------------------

async def test_small_drift_does_not_restart():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(1))
    await asyncio.wait_for(sub.get(), 1)
    world.playing["p1"] = Playing(URI, 11.5, True)
    await settle(50)
    assert len(spawn.procs) == 1 and not spawn.procs[0].killed
    await an.close()


async def test_restart_on_seek():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(1))
    await asyncio.wait_for(sub.get(), 1)
    world.playing["p1"] = Playing(URI, 95.0, True)      # the speaker seeked
    await until(lambda: len(spawn.procs) == 2)
    assert spawn.procs[0].killed
    assert ss_of(spawn.procs[1].args) == 95.0
    spawn.procs[1].stdout.feed(hop(5))
    frame = await asyncio.wait_for(sub.get(), 1)
    assert frame["t"] == round(95.0 + (HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3)
    await an.close()


async def test_seek_backwards_restarts_too():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 60.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(1))
    await asyncio.wait_for(sub.get(), 1)
    world.playing["p1"] = Playing(URI, 5.0, True)
    await until(lambda: len(spawn.procs) == 2)
    assert ss_of(spawn.procs[1].args) == 5.0
    await an.close()


async def test_skip_restarts_and_no_stale_frames():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 100.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    old = spawn.procs[0]
    old.stdout.feed(hop(1))
    await until(lambda: not sub.queue.empty())          # queued, not yet sent
    world.playing["p1"] = Playing(URI2, 0.0, True)      # skip
    await until(lambda: len(spawn.procs) == 2)
    assert old.killed
    assert sub.queue.empty()                            # the old track's frame is gone
    old.stdout.buf.clear()
    old.stdout.eof = False
    old.stdout.feed(hop(50))                            # a late write from the dead ffmpeg
    await settle()
    assert sub.queue.empty()
    new = spawn.procs[1]
    assert new.args[new.args.index("-i") + 1].endswith("BBBBBBBBBBBBBBBBBBBBBB")
    assert ss_of(new.args) == 0.0
    new.stdout.feed(hop(7))
    frame = await asyncio.wait_for(sub.get(), 1)
    assert base64.b64decode(frame["w"])[0] == 8
    assert frame["t"] < 1.0
    await an.close()


async def test_measured_start_latency_is_led_on_restart():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    clock.now += 1.5                        # ffmpeg took 1.5 s to the first frame
    spawn.procs[0].stdout.feed(hop(1))
    await asyncio.wait_for(sub.get(), 1)
    world.playing["p1"] = Playing(URI, 40.0, True)
    await until(lambda: len(spawn.procs) == 2)
    assert ss_of(spawn.procs[1].args) == 41.5
    await an.close()


async def test_pause_stops_ffmpeg_and_resume_restarts():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    world.playing["p1"] = Playing(URI, 12.0, False)
    await until(lambda: spawn.procs[0].killed)
    await settle()
    assert len(spawn.procs) == 1
    world.playing["p1"] = Playing(URI, 12.0, True)
    await until(lambda: len(spawn.procs) == 2)
    assert ss_of(spawn.procs[1].args) == 12.0
    await an.close()


async def test_not_relayed_track_spawns_nothing():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing("x-sonos-spotify:spotify%3atrack%3a1", 10.0, True)
    an.subscribe("p1")
    await until(lambda: world.resolved)
    await settle(50)
    assert spawn.procs == []
    # not asked again every tick
    assert len(world.resolved) == 1
    await an.close()


async def test_failing_ffmpeg_backs_off():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    an.subscribe("p1")
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.end()             # died with no output
    await settle(50)
    assert len(spawn.procs) == 1            # not respawned in a hot loop
    clock.now += 60
    await until(lambda: len(spawn.procs) == 2)
    await an.close()


# --- subscribers -------------------------------------------------------------

async def test_stops_ten_seconds_after_last_subscriber():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    a, b = an.subscribe("p1"), an.subscribe("p1")
    await until(lambda: spawn.procs)
    an.unsubscribe(a)
    clock.now += 30
    await settle()
    assert an.running("p1") and not spawn.procs[0].killed
    an.unsubscribe(b)
    clock.now += 9.9
    await settle()
    assert an.running("p1") and not spawn.procs[0].killed
    clock.now += 0.2
    await until(lambda: not an.running("p1"))
    assert spawn.procs[0].killed
    # a new viewer brings it back
    world.playing["p1"] = Playing(URI, 50.0, True)
    an.subscribe("p1")
    await until(lambda: len(spawn.procs) == 2)
    await an.close()


async def test_returning_viewer_within_grace_keeps_ffmpeg():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    a = an.subscribe("p1")
    await until(lambda: spawn.procs)
    an.unsubscribe(a)
    clock.now += 5
    await settle()
    an.subscribe("p1")
    clock.now += 20
    await settle()
    assert an.running("p1") and len(spawn.procs) == 1 and not spawn.procs[0].killed
    await an.close()


async def test_disabled_without_ffmpeg():
    world = World()
    world.playing["p1"] = Playing(URI, 10.0, True)
    spawn = Spawner()
    an = Analyzer(ffmpeg=None, probe=world.probe, resolve=world.resolve,
                  spawn=spawn, clock=Clock(), tick=0.001)
    assert not an.enabled
    an.subscribe("p1")
    await settle(30)
    assert spawn.procs == [] and not an.running("p1")
    await an.close()


# --- Phase 4 live sources ----------------------------------------------------

async def test_registered_pcm_source():
    import struct
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    feed: asyncio.Queue = asyncio.Queue()

    async def pcm():
        while True:
            chunk = await feed.get()
            if chunk is None:
                return
            yield chunk

    unregister = an.register_source("p1", pcm())
    sub = an.subscribe("p1")
    samples = [0] * (HOP_SAMPLES - FRAME_SAMPLES) + [256] * FRAME_SAMPLES
    raw = struct.pack(f"<{len(samples)}h", *samples)
    await until(lambda: an._parties["p1"].on_source)
    await feed.put(raw[:1001])               # odd split across chunks
    await feed.put(raw[1001:])
    frame = await asyncio.wait_for(sub.get(), 1)
    assert base64.b64decode(frame["w"]) == bytes([129]) * FRAME_SAMPLES
    assert frame["t"] == round((HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3)
    assert spawn.procs == []                 # the live source wins over the track
    unregister()
    await feed.put(None)
    await until(lambda: spawn.procs)         # back to the track
    await an.close()


# --- default probe / resolve ----------------------------------------------------

def test_default_probe_extrapolates_position(monkeypatch):
    from backend.home import routes
    transport = types.SimpleNamespace(track_uri=URI, state="PLAYING", rel_time="0:01:05",
                                      position_read_at=500.0)
    coordinator = types.SimpleNamespace(transport=transport)
    controller = types.SimpleNamespace(coordinator_of=lambda uuid: coordinator)
    monkeypatch.setitem(main.state, "controller", controller)
    parties = {"p1": {"id": "p1", "speakers": ["RINCON_1"]},
               "p2": {"id": "p2", "speakers": []}}
    monkeypatch.setattr(routes, "engine", lambda: types.SimpleNamespace(get=parties.get))
    monkeypatch.setattr(analysis.time, "monotonic", lambda: 503.25)
    got = analysis.default_probe("p1")
    assert got == Playing(URI, 68.25, True)
    transport.state = "PAUSED_PLAYBACK"
    assert analysis.default_probe("p1") == Playing(URI, 65.0, False)
    assert analysis.default_probe("p2") is None
    assert analysis.default_probe("nope") is None


async def test_default_resolve(monkeypatch):
    from backend.home import youtube
    calls = []
    monkeypatch.setattr(analysis, "_video_of", lambda key: "vid123" if key == "AAAAAAAAAAAAAAAAAAAAAA" else None)

    async def audio_source(video_id):
        calls.append(video_id)
        return youtube.AudioSource("https://rr1.googlevideo.com/x", {"User-Agent": "UA"},
                                   "audio/mp4", None, 0)

    monkeypatch.setattr(youtube, "audio_source", audio_source)
    monkeypatch.setattr(main.fetchguard, "safe_to_fetch", lambda url: True)
    assert await analysis.default_resolve(URI) == ("https://rr1.googlevideo.com/x", {"User-Agent": "UA"})
    assert calls == ["vid123"]
    assert await analysis.default_resolve(URI2) is None
    assert await analysis.default_resolve("x-sonos-spotify:abc") is None
    monkeypatch.setattr(main.fetchguard, "safe_to_fetch", lambda url: False)
    assert await analysis.default_resolve(URI) is None

    async def broken(video_id):
        raise youtube.YouTubeError("nope")

    monkeypatch.setattr(youtube, "audio_source", broken)
    monkeypatch.setattr(main.fetchguard, "safe_to_fetch", lambda url: True)
    assert await analysis.default_resolve(URI) is None


# --- /ws/visual ------------------------------------------------------------

GUEST_P = Principal("guest", "guest:abc", "Ana")
USER_P = Principal("user", "sub-1", "Tudor")
SERVICE_P = Principal("service", "ha", "Home Assistant")
DISPLAY_P = Principal("display", "display:d1", "Living room TV")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(analysis, "party_known", lambda party_id, principal: party_id == "p1")
    return tmp_path


def client(principal=None) -> Client:
    c = Client(main.app, host="sonora.local")
    if principal is not None:
        c.cookies[SESSION_COOKIE] = sign_session(principal, 3600)
    return c


@pytest.fixture
async def fake_analyzer(monkeypatch):
    an, world, spawn, clock = make()
    monkeypatch.setattr(analysis, "analyzer", lambda: an)
    return an, world, spawn, clock


async def test_ws_visual_principals(home, monkeypatch, fake_analyzer):
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    sent = await client().websocket("/ws/visual?party=p1")
    assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008
    sent = await client(SERVICE_P).websocket("/ws/visual?party=p1")
    assert sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008
    for who in (GUEST_P, USER_P):
        sent = await client(who).websocket("/ws/visual?party=p1")
        assert sent[0]["type"] == "websocket.accept", who
    # display sessions come from a parallel task; any kind but service may watch
    monkeypatch.setattr(analysis, "_principal", lambda ws: DISPLAY_P)
    sent = await client().websocket("/ws/visual?party=p1")
    assert sent[0]["type"] == "websocket.accept"
    await fake_analyzer[0].close()


async def test_ws_visual_unknown_party(home, fake_analyzer):
    sent = await client().websocket("/ws/visual?party=zzz")
    assert sent[0]["type"] == "websocket.accept"
    assert sent[1]["type"] == "websocket.close" and sent[1]["code"] == analysis.CLOSE_UNKNOWN_PARTY
    await fake_analyzer[0].close()


async def test_ws_visual_disabled_without_ffmpeg(home, monkeypatch):
    monkeypatch.setattr(analysis.shutil, "which", lambda name: None)
    monkeypatch.setattr(analysis, "_singleton", None)
    assert analysis.analyzer() is None
    sent = await client().websocket("/ws/visual?party=p1")
    assert sent[0]["type"] == "websocket.accept"
    assert sent[1]["type"] == "websocket.close" and sent[1]["code"] == analysis.CLOSE_DISABLED


async def test_ws_visual_sends_frames_then_unsubscribes(home, fake_analyzer):
    an, world, spawn, clock = fake_analyzer
    world.playing["p1"] = Playing(URI, 3.0, True)
    incoming: asyncio.Queue = asyncio.Queue()
    sent: list[dict] = []
    await incoming.put({"type": "websocket.connect"})

    async def receive():
        return await incoming.get()

    async def send(msg):
        sent.append(msg)

    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "scheme": "ws", "path": "/ws/visual", "raw_path": b"/ws/visual",
             "query_string": b"party=p1", "headers": [(b"host", b"sonora.local")],
             "server": ("sonora.local", 80), "client": ("1.2.3.4", 1), "subprotocols": []}
    task = asyncio.create_task(main.app(scope, receive, send))
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(3))
    await until(lambda: any(m["type"] == "websocket.send" for m in sent))
    msg = next(m for m in sent if m["type"] == "websocket.send")
    frame = json.loads(msg["text"])
    assert frame == {"t": round(3.0 + (HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3),
                     "w": base64.b64encode(bytes([4]) * FRAME_SAMPLES).decode()}
    assert len(base64.b64decode(frame["w"])) <= 512
    await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 1)
    clock.now += 11
    await until(lambda: not an.running("p1"))
    await an.close()


def test_router_is_installed():
    def walk(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            else:
                yield route
    paths = {getattr(r, "path", "") for r in walk(main.app.routes)}
    assert "/ws/visual" in paths


async def test_hop_read_just_before_a_skip_is_not_published():
    """A hop ffmpeg handed over in the same instant the track changed (read,
    not yet published, when the reader is cancelled) is dropped by the
    generation check."""
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    party = an._parties["p1"]
    old = party.run
    await an._stop(party)                   # the skip
    assert an._emit(party, old, hop(1)) is False
    assert sub.queue.empty()
    await an.close()


# --- review fixes ----------------------------------------------------------------

def test_ffmpeg_headers_are_sanitised():
    args = analysis.ffmpeg_args("ffmpeg", "https://u.test/a", {
        "User-Agent": "UA",
        "X-Evil": "a\r\nCookie: stolen",
        "Bad\nKey": "v",
        "Has:Colon": "v",
        "Referer": "https://www.youtube.com/",
    }, 0)
    assert args[args.index("-headers") + 1] == "User-Agent: UA\r\nReferer: https://www.youtube.com/\r\n"
    only_bad = analysis.ffmpeg_args("ffmpeg", "https://u.test/a", {"X": "a\rb"}, 0)
    assert "-headers" not in only_bad


async def test_live_source_survives_idle_stop():
    """I-1: the party's idle stop never ends a registered source; it keeps
    being drained and frames come back with the next viewer."""
    import struct
    an, world, spawn, clock = make()
    feed: asyncio.Queue = asyncio.Queue()
    pulled = []

    async def pcm():
        while True:
            chunk = await feed.get()
            pulled.append(len(chunk))
            yield chunk

    an.register_source("p1", pcm())
    hop_bytes = struct.pack(f"<{HOP_SAMPLES}h", *([256] * HOP_SAMPLES))
    sub = an.subscribe("p1")
    await until(lambda: an._parties["p1"].on_source)
    await feed.put(hop_bytes)
    await asyncio.wait_for(sub.get(), 1)
    an.unsubscribe(sub)
    clock.now += 10
    await until(lambda: not an.running("p1"))
    # nobody watching: still drained, no frames made
    await feed.put(hop_bytes)
    await feed.put(b"\x00")                 # an odd byte stays for alignment
    await until(lambda: len(pulled) == 3)
    assert "p1" in an._sources and not an._readers["p1"].done()
    sub2 = an.subscribe("p1")
    await until(lambda: an._parties["p1"].on_source)
    await feed.put(b"\x01" + hop_bytes[:-1])  # completes the split sample
    frame = await asyncio.wait_for(sub2.get(), 1)
    # stream time kept counting while nobody watched: third hop of the stream
    assert frame["t"] == round((2 * HOP_SAMPLES + HOP_SAMPLES - FRAME_SAMPLES) / RATE, 3)
    assert spawn.procs == []
    await an.close()


async def test_live_source_unregisters_when_it_ends():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)

    async def pcm():
        yield b"\x00\x00" * 10

    an.register_source("p1", pcm())
    await until(lambda: "p1" not in an._sources and "p1" not in an._readers)
    an.subscribe("p1")
    await until(lambda: spawn.procs)         # back on the track
    await an.close()


async def test_explicit_unregister_source():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    gate = asyncio.Event()

    async def pcm():
        await gate.wait()
        yield b""

    an.register_source("p1", pcm())
    reader = an._readers["p1"]
    an.subscribe("p1")
    await until(lambda: an._parties["p1"].on_source)
    an.unregister_source("p1")
    await until(lambda: reader.done())
    await until(lambda: spawn.procs)
    await an.close()


async def test_mid_track_death_backs_off():
    an, world, spawn, clock = make()
    world.playing["p1"] = Playing(URI, 10.0, True)
    sub = an.subscribe("p1")
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(1))
    await asyncio.wait_for(sub.get(), 1)
    spawn.procs[0].stdout.end()             # died mid-track, after frames
    world.playing["p1"] = Playing(URI, 30.0, True)   # far apart: would restart at once
    await settle(50)
    assert len(spawn.procs) == 1
    clock.now += analysis.BACKOFF_MIN
    await until(lambda: len(spawn.procs) == 2)
    assert ss_of(spawn.procs[1].args) == 30.0
    await an.close()


async def test_ws_visual_closes_a_stuck_socket(home, fake_analyzer, monkeypatch):
    an, world, spawn, clock = fake_analyzer
    monkeypatch.setattr(analysis, "SEND_TIMEOUT", 0.05)
    world.playing["p1"] = Playing(URI, 3.0, True)
    incoming: asyncio.Queue = asyncio.Queue()
    sent: list[dict] = []
    await incoming.put({"type": "websocket.connect"})
    never = asyncio.Event()

    async def receive():
        return await incoming.get()

    async def send(msg):
        if msg["type"] == "websocket.send":
            await never.wait()               # the viewer stopped reading
        sent.append(msg)

    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "scheme": "ws", "path": "/ws/visual", "raw_path": b"/ws/visual",
             "query_string": b"party=p1", "headers": [(b"host", b"sonora.local")],
             "server": ("sonora.local", 80), "client": ("1.2.3.4", 1), "subprotocols": []}
    task = asyncio.create_task(main.app(scope, receive, send))
    await until(lambda: spawn.procs)
    spawn.procs[0].stdout.feed(hop(3))
    await asyncio.wait_for(task, 2)          # the endpoint gave up on its own
    closes = [m for m in sent if m["type"] == "websocket.close"]
    assert closes and closes[0]["code"] == analysis.CLOSE_TOO_SLOW
    assert not an._parties["p1"].subs        # unsubscribed
    await an.close()
