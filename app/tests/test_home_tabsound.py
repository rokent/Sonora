"""Send this tab's sound: ``/ws/tabsound`` decodes WebM chunks and feeds live
sessions. The decoder is an identity pipe (what the page sends comes out as
"PCM"), so nothing runs ffmpeg; the hub is a fake except where the real one
shows the replacement rule end to end."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import main
from backend.config import settings
from backend.home import live, live_routes, routes, tabsound
from backend.home.auth import SESSION_COOKIE, Principal, sign_session
from backend.home.permissions import allowed

from test_home_live import FakePipe, Spawner, make_hub

USER_P = Principal("user", "sub-1", "Tudor")
GUEST_P = Principal("guest", "guest:abc", "Ana")
DISPLAY_P = Principal("display", "d1", "TV")
SERVICE_P = Principal("service", "ha", "Home Assistant")

SOUND = b"\x01\x02" * 64
SILENCE = b"\x00" * 128


class FakeSession:
    def __init__(self, target, fmt, source_name, by):
        self.target, self.input_format, self.source_name, self.by = target, fmt, source_name, by
        self.ended, self.reason, self.fed = False, None, []

    async def feed(self, pcm):
        self.fed.append(pcm)

    async def end(self, reason):
        self.ended, self.reason = True, reason


class FakeHub:
    def __init__(self):
        self.sessions: list[FakeSession] = []
        self.fail: Exception | None = None

    async def start(self, *, target, source_name, by, input_format):
        if self.fail:
            raise self.fail
        s = FakeSession(target, input_format, source_name, by)
        self.sessions.append(s)
        return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    hub, spawner = FakeHub(), Spawner()
    running = {"p1"}
    zones = {"RINCON_A"}

    def resolve(target):
        kind, ident = target
        if kind == "party":
            return "RINCON_A" if ident in running else None
        return ident if ident in zones else None

    monkeypatch.setattr(live_routes, "hub", lambda: hub)
    monkeypatch.setattr(live_routes, "_resolve", resolve)
    monkeypatch.setattr(tabsound, "spawn", spawner)
    return type("Env", (), {"hub": hub, "spawner": spawner, "running": running,
                            "monkeypatch": monkeypatch})


class Socket:
    """Drive ``/ws/tabsound`` on the real app through ASGI."""

    def __init__(self, query: str, principal: Principal | None = USER_P):
        self.query, self.principal = query, principal
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.task: asyncio.Task | None = None

    async def open(self):
        headers = [(b"host", b"sonora.local")]
        if self.principal is not None:
            headers.append((b"cookie", f"{SESSION_COOKIE}={sign_session(self.principal, 3600)}"
                            .encode()))
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "scheme": "ws", "path": "/ws/tabsound", "raw_path": b"/ws/tabsound",
                 "query_string": self.query.encode(), "headers": headers,
                 "server": ("sonora.local", 80), "client": ("1.2.3.4", 1), "subprotocols": []}
        await self.incoming.put({"type": "websocket.connect"})

        async def send(msg):
            self.sent.append(msg)

        self.task = asyncio.ensure_future(main.app(scope, self.incoming.get, send))
        await settle()
        return self

    async def send_bytes(self, data: bytes):
        await self.incoming.put({"type": "websocket.receive", "bytes": data})
        await settle()

    async def hang_up(self):
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)

    def accepted(self) -> bool:
        return bool(self.sent) and self.sent[0]["type"] == "websocket.accept"

    def closed(self):
        for msg in self.sent:
            if msg["type"] == "websocket.close":
                return msg.get("code"), msg.get("reason", "")
        return None

    def states(self) -> list[str]:
        return [json.loads(m["text"])["state"] for m in self.sent
                if m["type"] == "websocket.send" and m.get("text")]


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def opened(query, principal=USER_P):
    return await Socket(query, principal).open()


# --- who may send where ---------------------------------------------------

def test_tabsound_rules_users_and_guests_only():
    for who in (USER_P, GUEST_P):
        assert allowed(who, "GET", "/ws/tabsound"), who
    for who in (DISPLAY_P, SERVICE_P, None):
        assert not allowed(who, "GET", "/ws/tabsound"), who


@pytest.mark.parametrize("who", [None, DISPLAY_P, SERVICE_P])
async def test_refused_before_the_handshake(env, who):
    sock = await opened("target=party:p1", who)
    assert sock.sent[0]["type"] == "websocket.close" and sock.sent[0]["code"] == 1008
    assert env.spawner.pipes == []


async def test_guest_may_not_send_to_a_room(env):
    sock = await opened("target=zone:RINCON_A", GUEST_P)
    assert sock.accepted() and sock.closed() == (4403, "forbidden")
    assert env.spawner.pipes == [] and env.hub.sessions == []


async def test_guest_only_to_a_running_party(env):
    sock = await opened("target=party:p2", GUEST_P)
    assert sock.closed() == (4404, "unknown_target")
    sock = await opened("target=party:p1", GUEST_P)
    assert sock.closed() is None
    await sock.send_bytes(SOUND)
    assert [s.target for s in env.hub.sessions] == [("party", "p1")]
    await sock.hang_up()


async def test_user_may_send_to_a_room(env):
    sock = await opened("target=zone:RINCON_A")
    await sock.send_bytes(SOUND)
    (session,) = env.hub.sessions
    assert session.target == ("zone", "RINCON_A")
    assert session.source_name == "Tudor (tab)" and session.by == "tab"
    assert session.input_format == ("s16le", 44100, 2)
    await sock.hang_up()


@pytest.mark.parametrize("query", ["", "target=", "target=room:x", "target=party:",
                                   "target=p1", "target=party:" + "x" * 200])
async def test_bad_target(env, query):
    sock = await opened(query)
    assert sock.accepted() and sock.closed() == (4400, "bad_target")


async def test_unknown_room_for_a_user(env):
    sock = await opened("target=zone:RINCON_Z")
    assert sock.closed() == (4404, "unknown_target")


async def test_guest_whose_party_ends_before_sound_is_refused(env):
    sock = await opened("target=party:p1", GUEST_P)
    env.running.clear()
    await sock.send_bytes(SOUND)
    assert sock.closed() == (4404, "unknown_target") and env.hub.sessions == []
    await asyncio.wait_for(sock.task, 2)


# --- the stream -----------------------------------------------------------

async def test_decoder_args_and_feeding(env):
    sock = await opened("target=party:p1")
    assert env.spawner.pipes == []              # no decoder before the first chunk
    await sock.send_bytes(SOUND)
    (pipe,) = env.spawner.pipes
    assert pipe.args[pipe.args.index("-f") + 1] == "webm"
    assert pipe.args[pipe.args.index("-i") + 1] == "pipe:0"
    assert pipe.args[-7:] == ["-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"]
    assert "-nostdin" in pipe.args
    await sock.send_bytes(SOUND + b"\x03")
    (session,) = env.hub.sessions
    assert session.fed == [SOUND, SOUND + b"\x03"]
    assert sock.states() == ["waiting", "live"]
    await sock.hang_up()
    assert session.ended and session.reason == "disconnected"
    assert pipe.closed


async def test_silence_does_not_start_a_session(env):
    sock = await opened("target=party:p1")
    await sock.send_bytes(SILENCE)
    assert env.hub.sessions == []
    await sock.send_bytes(SOUND)
    await sock.send_bytes(SILENCE)          # silence inside a session is fed
    assert env.hub.sessions[0].fed == [SOUND, SILENCE]
    await sock.hang_up()


async def test_after_an_idle_end_the_next_sound_starts_again(env):
    sock = await opened("target=party:p1")
    await sock.send_bytes(SOUND)
    first = env.hub.sessions[0]
    first.ended, first.reason = True, "idle"
    await sock.send_bytes(SILENCE)
    assert len(env.hub.sessions) == 1 and sock.states() == ["waiting", "live", "waiting"]
    await sock.send_bytes(SOUND)
    assert len(env.hub.sessions) == 2 and env.hub.sessions[1].fed == [SOUND]
    assert sock.states() == ["waiting", "live", "waiting", "live"]
    assert sock.closed() is None
    await sock.hang_up()
    assert env.hub.sessions[1].reason == "disconnected"


@pytest.mark.parametrize("reason,closed", [("replaced", (4409, "replaced")),
                                           ("joined_party", (4409, "replaced")),
                                           ("party_ended", (4410, "party_ended")),
                                           ("shutdown", (4503, "shutdown"))])
async def test_final_ends_close_the_socket(env, reason, closed):
    sock = await opened("target=party:p1")
    await sock.send_bytes(SOUND)
    env.hub.sessions[0].ended, env.hub.sessions[0].reason = True, reason
    await sock.send_bytes(SOUND)
    assert sock.closed() == closed and len(env.hub.sessions) == 1
    await asyncio.wait_for(sock.task, 2)
    assert env.spawner.pipes[0].closed


async def test_start_refused_by_the_hub(env):
    env.hub.fail = live.NoTarget("gone")
    sock = await opened("target=party:p1")
    await sock.send_bytes(SOUND)
    assert sock.closed() == (4404, "unknown_target")
    await asyncio.wait_for(sock.task, 2)


async def test_oversize_message(env):
    sock = await opened("target=party:p1")
    await sock.send_bytes(b"\x01" * (tabsound.MAX_MESSAGE + 1))
    assert sock.closed() == (4413, "too_large")
    await asyncio.wait_for(sock.task, 2)
    assert env.hub.sessions == []


async def test_decoder_exit_closes(env):
    sock = await opened("target=party:p1")
    await sock.send_bytes(SOUND)
    await env.spawner.pipes[0].q.put(b"")       # ffmpeg exited
    await settle()
    assert sock.closed() == (4500, "decoder_stopped")
    await asyncio.wait_for(sock.task, 2)
    assert env.hub.sessions[0].reason == "disconnected"


async def test_stalled_decoder_closes(env):
    env.monkeypatch.setattr(tabsound, "WRITE_TIMEOUT", 0.01)

    class Stuck(FakePipe):
        async def write(self, data):
            await asyncio.Event().wait()

    async def spawn(args):
        return Stuck(args)

    env.monkeypatch.setattr(tabsound, "spawn", spawn)
    sock = await opened("target=party:p1")
    await sock.send_bytes(SOUND)
    await asyncio.wait_for(sock.task, 2)
    assert sock.closed() == (4500, "decoder_stopped")


async def test_not_ready_and_no_decoder(env):
    def refuse():
        raise routes.Refused(503, "not_ready")

    env.monkeypatch.setattr(live_routes, "hub", refuse)
    sock = await opened("target=party:p1")
    assert sock.closed() == (4503, "not_ready") and env.spawner.pipes == []

    env.monkeypatch.setattr(live_routes, "hub", lambda: env.hub)

    async def missing(args):
        raise RuntimeError("ffmpeg is not installed")

    env.monkeypatch.setattr(tabsound, "spawn", missing)
    sock = await opened("target=party:p1")
    assert sock.closed() is None
    await sock.send_bytes(SOUND)
    assert sock.closed() == (4503, "no_decoder")
    await asyncio.wait_for(sock.task, 2)
    assert tabsound._running == {}


async def test_decoder_caps_per_principal_and_in_all(env):
    env.monkeypatch.setattr(tabsound, "_running", {})
    a1, a2, a3 = [await opened("target=party:p1") for _ in range(3)]
    await a1.send_bytes(SOUND)
    await a2.send_bytes(SOUND)
    assert a3.closed() is None                  # connecting alone costs nothing
    await a3.send_bytes(SOUND)
    assert a3.closed() == (4429, "too_many")
    await asyncio.wait_for(a3.task, 2)
    assert len(env.spawner.pipes) == 2
    others = [await opened("target=party:p1", Principal("user", f"sub-{i + 10}", "U"))
              for i in range(6)]
    for sock in others:
        await sock.send_bytes(SOUND)
    assert [s.closed() for s in others] == [None] * 6
    late = await opened("target=party:p1", Principal("user", "sub-99", "V"))
    await late.send_bytes(SOUND)
    assert late.closed() == (4429, "too_many")
    await a1.hang_up()                          # a slot comes free
    again = await opened("target=party:p1", Principal("user", "sub-99", "V"))
    await again.send_bytes(SOUND)
    assert again.closed() is None
    for sock in (a2, *others, again):
        await sock.hang_up()
    assert tabsound._running == {}


async def test_near_silence_does_not_start_a_session(env):
    sock = await opened("target=party:p1")
    await sock.send_bytes(b"\x02\x00\xfe\xff" * 64)       # +-2 LSB
    assert env.hub.sessions == [] and sock.states() == ["waiting"]
    await sock.hang_up()


async def test_with_the_real_hub_a_newer_tab_replaces_the_older(env):
    real = make_hub()
    env.monkeypatch.setattr(live_routes, "hub", lambda: real.hub)
    first = await opened("target=party:p1")
    await first.send_bytes(SOUND)
    (play,) = [c for c in real.commands.calls if c[0] == "play_now"]
    assert play[2].startswith("x-rincon-mp3radio://10.0.0.2:8080/api/home/live/")
    second = await opened("target=party:p1", GUEST_P)
    await second.send_bytes(SOUND)
    await first.send_bytes(SOUND)               # the first notices on its next chunk
    assert first.closed() == (4409, "replaced")
    await asyncio.wait_for(first.task, 2)
    assert second.closed() is None
    assert real.commands.resumes() == []        # nothing resumed while the newer one plays
    await second.hang_up()
    await settle()
    assert [c[0] for c in real.commands.resumes()] == ["seek_track", "seek_time", "play"]
