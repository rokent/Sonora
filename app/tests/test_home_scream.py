"""Scream receiver: datagrams go through the protocol object to a fake hub."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend import main
from backend.config import settings
from backend.home import permissions, scream
from backend.home.auth import Principal

from home_client import Client


def packet(code=0x01, bits=16, channels=2, pcm=b"\x00\x10" * 576):
    return bytes([code, bits, channels, 3, 0]) + pcm


class FakeSession:
    def __init__(self, target, fmt):
        self.target, self.format, self.ended, self.fed, self.reason = target, fmt, False, [], None

    async def feed(self, pcm):
        self.fed.append(pcm)

    async def end(self, reason):
        self.ended, self.reason = True, reason


class FakeHub:
    def __init__(self):
        self.sessions, self.fail = [], None

    async def start(self, *, target, source_name, by, input_format):
        if self.fail:
            raise self.fail
        s = FakeSession(target, input_format)
        s.source_name, s.by = source_name, by
        self.sessions.append(s)
        return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    hub = FakeHub()
    now = {"t": 100.0, "m": 0.0}
    rx = scream.ScreamReceiver(lambda: hub, clock=lambda: now["t"], mono=lambda: now["m"])
    monkeypatch.setattr(scream, "_receiver", rx)
    rx.hub, rx.now = hub, now
    return rx


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


# --- header ---------------------------------------------------------------


def test_header_48k_base():
    assert scream.parse(packet(0x01, 16, 2))[0] == ("s16le", 48000, 2)
    assert scream.parse(packet(0x02, 24, 2, b"\x00" * 6))[0] == ("s24le", 96000, 2)


def test_header_44k_base():
    assert scream.parse(packet(0x81, 16, 2))[0] == ("s16le", 44100, 2)
    assert scream.parse(packet(0x82, 32, 1, b"\x00" * 4))[0] == ("s32le", 88200, 1)


def test_header_rejects_junk_and_trims_partial_frames():
    assert scream.parse(b"\x01\x10\x02") is None
    assert scream.parse(packet(0x01, 12, 2)) is None
    assert scream.parse(packet(0x00, 16, 2)) is None
    assert scream.parse(packet(0x01, 16, 0)) is None
    assert scream.parse(packet(pcm=b"")) is None
    fmt, pcm = scream.parse(packet(pcm=b"\x01" * 10))
    assert len(pcm) == 8


def test_header_rejects_rates_over_384k():
    assert scream.parse(packet(0x08, 16, 2))[0][1] == 384000
    assert scream.parse(packet(0x09, 16, 2)) is None
    assert scream.parse(packet(0xFF, 16, 2)) is None


# --- mapping --------------------------------------------------------------


async def test_unmapped_is_listed_not_played(env):
    env.datagram_received(packet(), ("10.0.0.5", 5000))
    await settle()
    assert env.hub.sessions == []
    rows = env.listing()
    assert rows == [{"ip": "10.0.0.5", "last_seen": 100.0,
                     "format": {"format": "s16le", "rate": 48000, "channels": 2}, "target": None}]


async def test_mapped_source_feeds_a_session(env):
    env.set_target("10.0.0.5", {"kind": "party", "id": "p1"})
    p = packet()
    env.datagram_received(p, ("10.0.0.5", 5000))
    env.datagram_received(packet(0x81), ("10.0.0.5", 5000))   # format change
    await settle()
    first, second = env.hub.sessions
    assert first.target == ("party", "p1") and first.format == ("s16le", 48000, 2)
    # Same target, new format: a plain start (the hub hands over), no end
    # from here that would cut the queue in between.
    assert first.fed == [p[5:]] and not first.ended
    assert second.format == ("s16le", 44100, 2) and len(second.fed) == 1
    assert first.by == "scream" and "10.0.0.5" in first.source_name


async def test_other_sender_stays_ignored(env):
    env.set_target("10.0.0.5", {"kind": "zone", "id": "RINCON_1"})
    env.datagram_received(packet(), ("10.0.0.6", 5000))
    await settle()
    assert env.hub.sessions == []


async def test_new_session_when_the_old_one_ended(env):
    env.set_target("10.0.0.5", {"kind": "zone", "id": "RINCON_1"})
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    env.hub.sessions[0].ended = True            # idle watchdog ended it
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    assert len(env.hub.sessions) == 2 and len(env.hub.sessions[1].fed) == 1


async def test_silence_does_not_restart_an_ended_session(env):
    env.set_target("10.0.0.5", {"kind": "zone", "id": "RINCON_1"})
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    env.hub.sessions[0].ended = True
    env.datagram_received(packet(pcm=b"\x00" * 1152), ("10.0.0.5", 1))
    await settle()
    assert len(env.hub.sessions) == 1


async def test_refused_start_backs_off(env):
    env.set_target("10.0.0.5", {"kind": "party", "id": "gone"})
    env.hub.fail = RuntimeError("no target")
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    env.hub.fail = None
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    assert env.hub.sessions == []              # inside the back-off
    env.now["m"] = 10.0
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    assert len(env.hub.sessions) == 1


async def test_a_target_change_ends_the_old_session(env):
    env.set_target("10.0.0.5", {"kind": "party", "id": "p1"})
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    env.set_target("10.0.0.5", {"kind": "zone", "id": "RINCON_1"})
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    first, second = env.hub.sessions
    assert first.ended and first.reason == "source_changed"
    assert second.target == ("zone", "RINCON_1")


async def test_with_the_real_hub_a_format_change_hands_over_without_a_resume():
    from test_home_live import make_hub
    t = make_hub()
    rx = scream.ScreamReceiver(lambda: t.hub, path_fn=lambda: None)
    rx._map = {"10.0.0.5": {"kind": "party", "id": "p1"}}
    rx.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    rx.datagram_received(packet(0x81), ("10.0.0.5", 1))
    await settle()
    first, second = rx.sources["10.0.0.5"].session, None
    assert t.commands.resumes() == []           # no queue blip between the two
    assert [c[0] for c in t.commands.calls] == ["play_now", "play_now"]
    sessions = t.hub.sessions()
    assert len(sessions) == 1 and sessions[0] is first and not first.ended
    await first.end("disconnected")
    assert [c[0] for c in t.commands.resumes()] == ["seek_track", "seek_time", "play"]


# --- newer sender wins ----------------------------------------------------


async def test_a_replaced_source_holds_until_quiet_for_2s(env):
    env.set_target("10.0.0.5", {"kind": "party", "id": "p1"})
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    first = env.hub.sessions[0]
    first.ended, first.reason = True, "replaced"     # a tab took the party over
    for step in range(10):                           # it keeps sending sound
        env.now["m"] += 0.5
        env.datagram_received(packet(), ("10.0.0.5", 1))
        await settle()
    assert len(env.hub.sessions) == 1
    env.now["m"] += 1.0                              # quiet packets only: not sound
    env.datagram_received(packet(pcm=b"\x01\x00" * 576), ("10.0.0.5", 1))
    await settle()
    env.now["m"] += 1.1
    env.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    assert len(env.hub.sessions) == 2 and env.hub.sessions[1].fed


async def test_with_the_real_hub_a_tab_keeps_the_party_from_scream():
    from test_home_live import make_hub
    t = make_hub()
    now = {"m": 0.0}
    rx = scream.ScreamReceiver(lambda: t.hub, path_fn=lambda: None, mono=lambda: now["m"])
    rx._map = {"10.0.0.5": {"kind": "party", "id": "p1"}}
    rx.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    tab = await t.hub.start(target=("party", "p1"), source_name="tab", by="tab")
    for _ in range(6):
        now["m"] += 0.3
        rx.datagram_received(packet(), ("10.0.0.5", 1))
        await settle()
    assert t.hub.current(("party", "p1")) is tab and not tab.ended
    now["m"] += 2.5
    rx.datagram_received(packet(), ("10.0.0.5", 1))
    await settle()
    assert tab.ended and tab.reason == "replaced"
    await rx.close()


# --- the source list ------------------------------------------------------


async def test_a_full_list_evicts_stale_unmapped_sources(env):
    for i in range(scream._MAX_SOURCES):
        env.datagram_received(packet(), (f"10.1.0.{i + 1}", 1))
    env.now["t"] += 60
    env.datagram_received(packet(), ("10.2.0.1", 1))
    assert "10.2.0.1" not in env.sources          # all still fresh
    env.now["t"] += scream._EVICT_AFTER
    env.datagram_received(packet(), ("10.1.0.1", 1))   # one stays fresh
    env.datagram_received(packet(), ("10.2.0.1", 1))
    assert "10.2.0.1" in env.sources and "10.1.0.1" in env.sources
    assert len(env.sources) == 2


async def test_a_mapped_ip_is_always_admitted(env):
    for i in range(scream._MAX_SOURCES):
        env.datagram_received(packet(), (f"10.1.0.{i + 1}", 1))
    env.set_target("10.3.0.1", {"kind": "zone", "id": "RINCON_1"})
    env.datagram_received(packet(), ("10.3.0.1", 1))
    await settle()
    assert "10.3.0.1" in env.sources and len(env.sources) == scream._MAX_SOURCES
    assert "10.1.0.1" not in env.sources       # the longest unheard made way
    assert env.hub.sessions and env.hub.sessions[0].target == ("zone", "RINCON_1")
    await env.close()


async def test_party_end_unmaps_its_sources_once(env, tmp_path, caplog):
    env.set_target("10.0.0.5", {"kind": "party", "id": "p1"})
    env.set_target("10.0.0.6", {"kind": "zone", "id": "RINCON_1"})
    with caplog.at_level("INFO"):
        env.party_ended("p1")
        env.party_ended("p1")
    assert env.mapping() == {"10.0.0.6": {"kind": "zone", "id": "RINCON_1"}}
    saved = json.loads((tmp_path / "home" / "scream.json").read_text())
    assert saved == {"10.0.0.6": {"kind": "zone", "id": "RINCON_1"}}
    assert caplog.text.count("party p1 ended") == 1


def test_mapping_is_persisted(env, tmp_path):
    env.set_target("10.0.0.5", {"kind": "party", "id": "p1"})
    saved = json.loads((tmp_path / "home" / "scream.json").read_text())
    assert saved == {"10.0.0.5": {"kind": "party", "id": "p1"}}
    again = scream.ScreamReceiver(lambda: None)
    assert again.mapping() == saved
    assert again.listing()[0]["last_seen"] is None      # mapped, never heard
    env.set_target("10.0.0.5", None)
    assert json.loads((tmp_path / "home" / "scream.json").read_text()) == {}


# --- routes ---------------------------------------------------------------


async def test_routes_list_and_map(env, tmp_path):
    c = Client(main.app, host="sonora.local")
    env.datagram_received(packet(), ("10.0.0.5", 1))
    got = (await c.request("GET", "/api/scream/sources")).json()
    assert got[0]["ip"] == "10.0.0.5" and got[0]["target"] is None
    put = await c.request("PUT", "/api/scream/sources/10.0.0.5",
                          {"target": {"kind": "zone", "id": "RINCON_1"}})
    assert put.status == 200
    assert (await c.request("GET", "/api/scream/sources")).json()[0]["target"] == {"kind": "zone", "id": "RINCON_1"}
    assert (await c.request("PUT", "/api/scream/sources/10.0.0.5", {"target": None})).status == 200
    assert (await c.request("GET", "/api/scream/sources")).json()[0]["target"] is None


@pytest.mark.parametrize("ip,body", [
    ("nope", {"target": None}),
    ("::1", {"target": None}),
    ("10.0.0.5", {}),
    ("10.0.0.5", {"target": {"kind": "room", "id": "x"}}),
    ("10.0.0.5", {"target": {"kind": "party", "id": ""}}),
    ("10.0.0.5", {"target": {"kind": "party"}}),
    ("10.0.0.5", {"target": "p1"}),
])
async def test_routes_validate(env, ip, body):
    c = Client(main.app, host="sonora.local")
    assert (await c.request("PUT", f"/api/scream/sources/{ip}", body)).status == 400
    assert env.mapping() == {}


def test_routes_are_for_users_only():
    guest = Principal("guest", "g", "G")
    display = Principal("display", "d", "D")
    user = Principal("user", "u", "U")
    for path, method in (("/api/scream/sources", "GET"), ("/api/scream/sources/10.0.0.5", "PUT")):
        assert not permissions.allowed(None, method, path)
        assert not permissions.allowed(guest, method, path)
        assert not permissions.allowed(display, method, path)
        assert permissions.allowed(user, method, path)


# --- socket ---------------------------------------------------------------


async def test_start_binds_and_receives_unicast(env):
    sock = scream.open_socket(0)
    port = sock.getsockname()[1]
    sock.close()
    env.set_target("127.0.0.1", {"kind": "party", "id": "p1"})
    assert await scream.start(port=port, proto=env) is env
    import socket
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.sendto(packet(), ("127.0.0.1", port))
    tx.close()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if env.hub.sessions and env.hub.sessions[0].fed:
            break
    assert env.hub.sessions[0].target == ("party", "p1")
    await env.close()


async def test_start_disabled_or_unbindable_is_not_fatal(env, monkeypatch, caplog):
    assert await scream.start(port=0) is None
    monkeypatch.setattr(scream, "open_socket", lambda port: (_ for _ in ()).throw(OSError("in use")))
    with caplog.at_level("WARNING"):
        assert await scream.start(port=4010) is None
    assert "cannot be bound" in caplog.text


def test_configured_port(monkeypatch):
    monkeypatch.delenv("SONORA_SCREAM_PORT", raising=False)
    assert scream.configured_port() == 4010
    monkeypatch.setenv("SONORA_SCREAM_PORT", "0")
    assert scream.configured_port() == 0
    monkeypatch.setenv("SONORA_SCREAM_PORT", "abc")
    assert scream.configured_port() == 4010
