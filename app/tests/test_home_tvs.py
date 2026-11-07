"""Samsung TV control: the TV list, pairing, showing a party, Wake-on-LAN,
the Home Assistant route and presets that show a party on a TV.

No real TV: an aiohttp test server plays the TV's remote-control socket. It
answers ``ms.channel.connect`` (with a token when told to) and keeps every
message it is sent."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import time
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from test_home_routes import GUEST, USER, client, rig  # noqa: F401
from test_home_ha import _token, call

from backend.home import routes, tvs
from backend.home.store import home_dir


class FakeTv:
    """The TV's socket. ``reply``: ``"connect"`` (default), ``"unauthorized"``,
    ``"silent"`` (never answers, as when the allow prompt is waiting)."""

    def __init__(self, token="tok-123", reply="connect"):
        self.token, self.reply = token, reply
        self.internet = False          # whether Samsung Internet is "installed"
        self.probes: list[str] = []
        self.requests: list[dict] = []
        self.received: list[dict] = []
        self.server: TestServer | None = None

    async def _ws(self, request):
        self.requests.append({"path": request.path, "query": dict(request.query)})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.reply == "connect":
            data = {"clients": [], "id": "x"}
            if self.token:
                data["token"] = self.token
            await ws.send_str(json.dumps({"event": "ms.channel.connect", "data": data}))
        elif self.reply == "unauthorized":
            await ws.send_str(json.dumps({"event": "ms.channel.unauthorized"}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                self.received.append(json.loads(msg.data))
        return ws

    async def _app(self, request):
        self.probes.append(request.match_info["app"])
        if self.internet and request.match_info["app"] == tvs.SAMSUNG_INTERNET:
            return web.json_response({"id": tvs.SAMSUNG_INTERNET, "running": False})
        return web.json_response({"error": "not found"}, status=404)

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v2/channels/samsung.remote.control", self._ws)
        app.router.add_get("/api/v2/applications/{app}", self._app)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return self.server.port

    async def close(self):
        if self.server is not None:
            await self.server.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fast(monkeypatch):
    """Short timeouts, no real broadcasts, a fixed base URL for the TV."""
    sent: list[tuple[bytes, tuple]] = []
    monkeypatch.setattr(tvs.config, "timeout", 0.5)
    monkeypatch.setattr(tvs.config, "pair_wait", 0.5)
    monkeypatch.setattr(tvs.config, "wake_window", 0.8)
    monkeypatch.setattr(tvs.config, "retry_every", 0.1)
    monkeypatch.setattr(tvs, "_send_udp", lambda packet, addr: sent.append((packet, addr)))
    async def base(host):
        return "http://10.0.0.2:50205"
    monkeypatch.setattr(tvs, "lan_base", base)
    return SimpleNamespace(wol=sent)


@pytest.fixture
async def tv(monkeypatch, fast):
    fake = FakeTv()
    port = await fake.start()
    # The test server speaks plain ws; the "secure" leg points at it.
    monkeypatch.setattr(tvs.config, "secure", ("ws", port))
    monkeypatch.setattr(tvs.config, "plain", ("ws", _free_port()))
    monkeypatch.setattr(tvs.config, "rest", ("http", port))
    yield fake
    await fake.close()


async def _add(body=None):
    r = await client(USER).request("POST", "/api/tvs",
                                   body or {"name": "Living room", "host": "127.0.0.1"})
    assert r.status == 200, r.body
    return r.json()


async def _party():
    r = await client(USER).request("POST", "/api/parties", {"speakers": ["A", "B"]})
    assert r.status == 200, r.body
    return r.json()


# -- the list -----------------------------------------------------------------


async def test_crud_and_token_never_listed(rig, fast):
    made = await _add({"name": "Bedroom", "host": "192.168.1.40", "mac": "AA-BB-CC-dd-ee-ff",
                       "mode_default": "visualizer"})
    assert made["name"] == "Bedroom" and made["mac"] == "aa:bb:cc:dd:ee:ff"
    assert made["mode_default"] == "visualizer" and made["paired"] is False and "token" not in made
    tvs.store().set_token(made["id"], "secret-token")
    listed = (await client(USER).request("GET", "/api/tvs")).json()["tvs"]
    assert [t["id"] for t in listed] == [made["id"]]
    assert listed[0]["paired"] is True and "token" not in listed[0]
    assert "secret-token" in (home_dir() / "tvs.json").read_text()
    r = await client(USER).request("PUT", f"/api/tvs/{made['id']}",
                                   {"name": "Bed", "host": "192.168.1.41", "mac": None})
    assert r.status == 200, r.body
    assert r.json()["name"] == "Bed" and r.json()["mac"] is None and r.json()["paired"] is True
    r = await client(USER).request("PUT", f"/api/tvs/{made['id']}", {"mode_default": None})
    assert r.status == 200 and r.json()["mode_default"] == "visualizer"
    assert (await client(USER).request("DELETE", f"/api/tvs/{made['id']}")).status == 200
    assert (await client(USER).request("DELETE", f"/api/tvs/{made['id']}")).status == 404
    assert (await client(USER).request("GET", "/api/tvs")).json()["tvs"] == []


@pytest.mark.parametrize("body", [
    {"name": "", "host": "1.2.3.4"}, {"name": "x", "host": ""},
    {"name": "x", "host": "1.2.3.4/evil"}, {"name": "x", "host": "1.2.3.4", "mac": "zz"},
    {"name": "x", "host": "1.2.3.4", "mode_default": "nope"},
    {"name": "x", "host": "1.2.3.4", "browser_app": "a b/c"}])
async def test_invalid_tv_is_400(rig, fast, body):
    r = await client(USER).request("POST", "/api/tvs", body)
    assert r.status == 400 and r.json()["error"] == "invalid"


async def test_guest_and_service_cannot_reach_tvs(rig, fast):
    made = await _add()
    assert (await client(GUEST).request("GET", "/api/tvs")).status == 403
    assert (await client(GUEST).request("POST", f"/api/tvs/{made['id']}/show",
                                        {"party_id": "p"})).status == 403
    t = (await _token())["token"]
    assert (await call(t, "GET", "/api/tvs")).status == 403


# -- pairing and showing ------------------------------------------------------


async def test_pair_stores_token_and_sends_name(rig, tv):
    made = await _add()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/pair")
    assert r.status == 200, r.body
    assert r.json() == {"ok": True, "paired": True}
    assert tvs.store().get(made["id"])["token"] == "tok-123"
    q = tv.requests[0]["query"]
    assert base64.b64decode(q["name"]).decode() == "Sonora Home" and "token" not in q


async def test_show_launches_browser_with_token(rig, tv):
    made = await _add({"name": "Living room", "host": "127.0.0.1", "mode_default": "cover"})
    tvs.store().set_token(made["id"], "tok-123")
    party = await _party()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show",
                                   {"party_id": party["id"], "mode": "visualizer"})
    assert r.status == 200, r.body
    assert r.json() == {"ok": True, "tv_id": made["id"], "party_id": party["id"],
                        "mode": "visualizer"}
    assert tv.requests[0]["query"]["token"] == "tok-123"
    await asyncio.sleep(0.05)
    assert tv.received == [{"method": "ms.channel.emit", "params": {
        "event": "ed.apps.launch", "to": "host",
        "data": {"appId": "org.tizen.browser", "action_type": "NATIVE_LAUNCH",
                 "metaTag": f"http://10.0.0.2:50205/tv?party={party['id']}&mode=visualizer"}}}]


async def test_show_uses_default_mode_and_injectable_url(rig, tv, monkeypatch):
    made = await _add({"name": "Living room", "host": "127.0.0.1", "mode_default": "cover"})
    party = await _party()
    seen = []

    def url_for(tv_, party_id, mode, base_url):
        seen.append((tv_["id"], party_id, mode, base_url))
        return "http://display/link"
    monkeypatch.setattr(tvs, "display_url_for", url_for)
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 200, r.body
    assert r.json()["mode"] == "cover"
    assert seen == [(made["id"], party["id"], "cover", "http://10.0.0.2:50205")]
    await asyncio.sleep(0.05)
    assert tv.received[0]["params"]["data"]["metaTag"] == "http://display/link"
    # The token the TV handed out on this connect is kept.
    assert tvs.store().get(made["id"])["token"] == "tok-123"


async def test_show_unknown_tv_party_or_mode(rig, tv):
    made = await _add()
    party = await _party()
    r = await client(USER).request("POST", "/api/tvs/nope/show", {"party_id": party["id"]})
    assert r.status == 404
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": "nope"})
    assert r.status == 404
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show",
                                   {"party_id": party["id"], "mode": "disco"})
    assert r.status == 400
    assert tv.received == []


async def test_falls_back_to_plain_port_without_token(rig, tv, monkeypatch):
    port = tv.server.port
    monkeypatch.setattr(tvs.config, "secure", ("ws", _free_port()))
    monkeypatch.setattr(tvs.config, "plain", ("ws", port))
    made = await _add()
    tvs.store().set_token(made["id"], "tok-old")
    party = await _party()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 200, r.body
    assert "token" not in tv.requests[0]["query"]
    await asyncio.sleep(0.05)
    assert tv.received[0]["params"]["event"] == "ed.apps.launch"


async def test_refused_prompt_is_needs_pairing(rig, tv):
    tv.reply = "unauthorized"
    made = await _add()
    party = await _party()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 409 and r.json()["error"] == "tv_needs_pairing"
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/pair")
    assert r.status == 409 and r.json()["error"] == "tv_needs_pairing"
    assert tv.received == []


async def test_waiting_prompt_is_needs_pairing_within_timeout(rig, tv):
    tv.reply = "silent"
    made = await _add()
    party = await _party()
    began = time.monotonic()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 409 and r.json()["error"] == "tv_needs_pairing"
    assert time.monotonic() - began < 2.0


# -- unreachable and Wake-on-LAN (Review Focus #4) -----------------------------


@pytest.fixture
async def black_hole(monkeypatch, fast):
    """A port that accepts TCP and never answers the websocket handshake,
    as a TV that is half asleep; the plain port refuses."""
    async def hold(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(tvs.config, "secure", ("ws", port))
    monkeypatch.setattr(tvs.config, "plain", ("ws", _free_port()))
    yield
    server.close()


async def test_unreachable_is_504_within_timeout(rig, black_hole, fast):
    made = await _add()
    party = await _party()
    began = time.monotonic()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    took = time.monotonic() - began
    assert r.status == 504 and r.json()["error"] == "tv_unreachable"
    assert took < 1.5          # timeout 0.5 s, injected
    assert fast.wol == []      # no MAC, no wake


async def test_unreachable_with_mac_wakes_first_then_gives_up(rig, black_hole, fast):
    made = await _add({"name": "TV", "host": "127.0.0.1", "mac": "01:23:45:67:89:ab"})
    party = await _party()
    began = time.monotonic()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    took = time.monotonic() - began
    assert r.status == 504 and r.json()["error"] == "tv_unreachable"
    assert took < 2.5          # wake window 0.8 s, injected
    addrs = [a for _, a in fast.wol]
    assert ("255.255.255.255", 9) in addrs and ("127.0.0.255", 9) in addrs
    # Sent again before every retry, not once.
    assert addrs.count(("255.255.255.255", 9)) >= 2


async def test_wake_then_connect(rig, tv, fast):
    made = await _add({"name": "TV", "host": "127.0.0.1", "mac": "01:23:45:67:89:ab"})
    party = await _party()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 200, r.body
    assert fast.wol      # sent first, even though the TV answered right away


def test_magic_packet():
    packet = tvs.magic_packet("01:23:45:67:89:AB")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("0123456789ab") * 16
    assert tvs.magic_packet("0123456789ab") == packet


def test_wake_targets(monkeypatch):
    sent = []
    monkeypatch.setattr(tvs, "_send_udp", lambda p, a: sent.append(a))
    tvs.wake("01:23:45:67:89:ab", "192.168.1.50")
    assert sent == [("255.255.255.255", 9), ("192.168.1.255", 9)]
    sent.clear()
    tvs.wake("01:23:45:67:89:ab", "tv.local")
    assert sent == [("255.255.255.255", 9)]


def test_real_tv_urls():
    tv_ = {"host": "192.168.1.50", "token": "tk"}
    secure, plain = tvs._urls(tv_)
    assert secure == ("wss://192.168.1.50:8002/api/v2/channels/samsung.remote.control"
                      "?name=U29ub3JhIEhvbWU=&token=tk")
    assert plain == "ws://192.168.1.50:8001/api/v2/channels/samsung.remote.control?name=U29ub3JhIEhvbWU="


def test_default_display_url():
    tv_ = {"id": "t1"}
    assert (tvs.display_url_for(tv_, "pa_1", "video", "http://10.0.0.2:50205")
            == "http://10.0.0.2:50205/tv?party=pa_1&mode=video")
    assert (tvs.display_url_for(tv_, "a&b=c d", "cover", "http://h")
            == "http://h/tv?party=a%26b%3Dc+d&mode=cover")


# -- which browser ------------------------------------------------------------


async def _show_once(made, party=None):
    party = party or await _party()
    r = await client(USER).request("POST", f"/api/tvs/{made['id']}/show", {"party_id": party["id"]})
    assert r.status == 200, r.body
    await asyncio.sleep(0.05)


async def test_probe_finds_samsung_internet_and_caches_it(rig, tv):
    tv.internet = True
    made = await _add()
    party = await _party()
    await _show_once(made, party)
    assert tv.probes == [tvs.SAMSUNG_INTERNET]
    data = tv.received[0]["params"]["data"]
    assert data["appId"] == tvs.SAMSUNG_INTERNET and data["action_type"] == "NATIVE_LAUNCH"
    assert tvs.store().get(made["id"])["browser_app"] == tvs.SAMSUNG_INTERNET
    await _show_once(made, party)
    assert len(tv.probes) == 1          # cached: not asked again
    assert tv.received[1]["params"]["data"]["appId"] == tvs.SAMSUNG_INTERNET


async def test_probe_without_samsung_internet_uses_tizen_browser(rig, tv):
    made = await _add()
    await _show_once(made)
    assert tv.probes == [tvs.SAMSUNG_INTERNET]
    assert tv.received[0]["params"]["data"]["appId"] == "org.tizen.browser"
    listed = (await client(USER).request("GET", "/api/tvs")).json()["tvs"][0]
    assert listed["browser_app"] == "org.tizen.browser"


async def test_set_browser_app_skips_probe(rig, tv):
    tv.internet = True
    made = await _add({"name": "TV", "host": "127.0.0.1", "browser_app": "com.example.web"})
    await _show_once(made)
    assert tv.probes == []
    assert tv.received[0]["params"]["data"]["appId"] == "com.example.web"


async def test_probe_failure_falls_back_uncached(rig, tv, monkeypatch):
    monkeypatch.setattr(tvs.config, "rest", ("http", _free_port()))
    made = await _add()
    await _show_once(made)
    assert tv.received[0]["params"]["data"]["appId"] == "org.tizen.browser"
    assert tvs.store().get(made["id"])["browser_app"] is None


# -- Home Assistant and presets ----------------------------------------------


async def test_ha_route_shows_with_service_token(rig, tv):
    made = await _add({"name": "TV", "host": "127.0.0.1", "mode_default": "video"})
    party = await _party()
    t = (await _token())["token"]
    r = await call(t, "POST", f"/api/ha/parties/{party['id']}/tv/{made['id']}")
    assert r.status == 200, r.body
    assert r.json()["mode"] == "video"
    r = await call(t, "POST", f"/api/ha/parties/{party['id']}/tv/{made['id']}", {"mode": "cover"})
    assert r.status == 200 and r.json()["mode"] == "cover"
    await asyncio.sleep(0.05)
    assert len(tv.received) == 2
    assert (await call(t, "POST", f"/api/ha/parties/{party['id']}/tv/nope")).status == 404


async def test_ha_route_unreachable_is_504(rig, black_hole):
    made = await _add()
    party = await _party()
    t = (await _token())["token"]
    r = await call(t, "POST", f"/api/ha/parties/{party['id']}/tv/{made['id']}")
    assert r.status == 504 and r.json()["error"] == "tv_unreachable"


async def _preset(tv_id, show="visualizer"):
    r = await client(USER).request("POST", "/api/presets", {
        "name": "Evening", "speakers": ["A", "B"], "tv": {"tv_id": tv_id, "show": show}})
    assert r.status == 200, r.body
    return r.json()


async def _settle():
    """Let the background TV shows finish."""
    while routes._tv_tasks:
        await asyncio.gather(*list(routes._tv_tasks))


def _results(rig):
    return [m for m in rig.sent if m.get("type") == "tv_result"]


async def test_preset_start_shows_on_tv_in_background(rig, tv):
    made = await _add()
    preset = await _preset(made["id"])
    r = await client(USER).request("POST", f"/api/presets/{preset['id']}/start")
    assert r.status == 200, r.body
    party = r.json()
    assert party["tv"] == "pending" and "tv_error" not in party
    await _settle()
    await asyncio.sleep(0.05)
    assert tv.received[0]["params"]["data"]["metaTag"].endswith(
        f"/tv?party={party['id']}&mode=visualizer")
    assert _results(rig) == [{"type": "tv_result", "party": party["id"], "tv_id": made["id"],
                              "ok": True, "error": None}]


async def test_preset_start_does_not_wait_for_unreachable_tv(rig, black_hole):
    made = await _add()
    preset = await _preset(made["id"])
    began = time.monotonic()
    r = await client(USER).request("POST", f"/api/presets/{preset['id']}/start")
    assert time.monotonic() - began < 0.3      # the TV timeout is 0.5 s
    assert r.status == 200, r.body
    party = r.json()
    assert party["tv"] == "pending" and party["speakers"] == ["A", "B"]
    assert _results(rig) == []
    await _settle()
    assert _results(rig) == [{"type": "tv_result", "party": party["id"], "tv_id": made["id"],
                              "ok": False, "error": "tv_unreachable"}]


async def test_ha_preset_start_in_background(rig, black_hole):
    made = await _add()
    preset = await _preset(made["id"])
    t = (await _token())["token"]
    began = time.monotonic()
    r = await call(t, "POST", f"/api/ha/presets/{preset['id']}/start")
    assert time.monotonic() - began < 0.3
    assert r.status == 200, r.body
    assert r.json()["tv"] == "pending"
    await _settle()
    assert [m["error"] for m in _results(rig)] == ["tv_unreachable"]


async def test_ha_preset_start_reports_missing_tv_at_once(rig, fast):
    preset = await _preset("gone")
    t = (await _token())["token"]
    r = await call(t, "POST", f"/api/ha/presets/{preset['id']}/start")
    assert r.status == 200, r.body
    assert r.json()["tv_error"] == "not_found" and "tv" not in r.json()
    assert not routes._tv_tasks
