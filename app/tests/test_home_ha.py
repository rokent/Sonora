"""Home Assistant's API: tokens, the service principal's reach, state, start, end."""

from __future__ import annotations

from types import SimpleNamespace

from test_home_routes import (OTHER_USER, USER, ITEM, client, rig, _start)  # noqa: F401

from backend import main
from backend.home import routes
from backend.home.store import home_dir
from home_client import Client


async def _token(label="HA"):
    r = await client(USER).request("POST", "/api/ha/tokens", {"label": label})
    assert r.status == 200, r.body
    return r.json()



async def call(token, method, path, body=None):
    c = Client(main.app, host="sonora.local")
    return await c.request(method, path, body, headers={"Authorization": f"Bearer {token}"})


async def test_token_create_list_delete(rig, tmp_path):
    made = await _token("Living room")
    assert made["token"] and made["id"]
    assert made["token"] not in (home_dir() / "ha_tokens.json").read_text()
    listed = (await client(USER).request("GET", "/api/ha/tokens")).json()["tokens"]
    assert [t["id"] for t in listed] == [made["id"]] and "token" not in listed[0] and "hash" not in listed[0]
    assert (await call(made["token"], "GET", "/api/ha/state")).status == 200
    r = await client(USER).request("DELETE", f"/api/ha/tokens/{made['id']}")
    assert r.status == 200
    assert (await call(made["token"], "GET", "/api/ha/state")).status == 401
    assert (await client(USER).request("DELETE", f"/api/ha/tokens/{made['id']}")).status == 404


async def test_wrong_token_and_no_token(rig):
    await _token()
    assert (await call("nope", "GET", "/api/ha/state")).status == 401
    assert (await Client(main.app, host="sonora.local").request("GET", "/api/ha/state")).status == 401


async def test_service_reach_is_ha_only(rig):
    t = (await _token())["token"]
    assert (await call(t, "POST", "/api/zones/A/group", {})).status == 403
    assert (await call(t, "GET", "/api/presets")).status == 403
    assert (await call(t, "GET", "/api/parties")).status == 403


async def test_service_token_cannot_manage_tokens(rig):
    t = (await _token())["token"]
    assert (await call(t, "GET", "/api/ha/tokens")).status == 403
    assert (await call(t, "POST", "/api/ha/tokens", {"label": "x"})).status == 403
    made = (await _token("other"))
    assert (await call(t, "DELETE", f"/api/ha/tokens/{made['id']}")).status == 403
    assert len((await client(USER).request("GET", "/api/ha/tokens")).json()["tokens"]) == 2


async def test_guest_cannot_manage_tokens(rig):
    from test_home_routes import GUEST
    assert (await client(GUEST).request("POST", "/api/ha/tokens", {})).status == 403


async def test_state_lists_presets_and_parties_with_track(rig):
    preset = (await client(USER).request("POST", "/api/presets",
              {"name": "Evening", "speakers": ["A", "B"]})).json()
    r = await _start(speakers=["A", "B"], name="Dinner")
    assert r.status == 200, r.body
    rig.ctl.zones["A"].transport = SimpleNamespace(title="Song", artist="Band", album="LP",
                                                   album_art_uri="http://h/a.jpg")
    t = (await _token())["token"]
    s = (await call(t, "GET", "/api/ha/state")).json()
    assert s["presets"] == [{"id": preset["id"], "name": "Evening"}]
    assert len(s["parties"]) == 1
    p = s["parties"][0]
    assert p["name"] == "Dinner" and p["state"] == "playing" and p["speakers"] == ["Room A", "Room B"]
    assert p["track"] == {"title": "Song", "artist": "Band", "album": "LP",
                          "art": "http://sonora.local/api/art?u=http%3A%2F%2Fh%2Fa.jpg"}


async def test_preset_start_takes_owned_speakers_and_end(rig):
    preset = (await client(USER).request("POST", "/api/presets",
              {"name": "Evening", "speakers": ["A", "B"]})).json()
    mine = (await _start(OTHER_USER, speakers=["A"], name="Ana's")).json()
    t = (await _token())["token"]
    r = await call(t, "POST", f"/api/ha/presets/{preset['id']}/start")
    assert r.status == 200, r.body
    party = r.json()
    assert party["speakers"] == ["A", "B"]
    assert routes.engine().owner_of("A") == party["id"] and mine["id"] != party["id"]
    assert (await call(t, "POST", "/api/ha/presets/none/start")).status == 404
    assert (await call(t, "POST", f"/api/ha/parties/{party['id']}/end")).status == 200
    assert routes.engine().owner_of("A") is None
    assert (await call(t, "POST", "/api/ha/parties/zzz/end")).status == 404


async def test_tv_route_reserved(rig):
    t = (await _token())["token"]
    r = await call(t, "POST", "/api/ha/parties/p/tv/tv1")
    assert r.status == 501 and r.json() == {"error": "phase3"}
