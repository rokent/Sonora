"""Parties and presets over HTTP: rights, conflicts, attribution, broadcasts.

Runs the real app and the real party engine against a fake controller and
fake speaker commands, with the scene machinery swapped out as in
test_home_parties.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import main
from backend.config import settings
from backend.home import routes
from backend.home.auth import SESSION_COOKIE, Principal, sign_session
from backend.sonos import scenes

from home_client import Client

USER = Principal("user", "sub-1", "Tudor")
OTHER_USER = Principal("user", "sub-2", "Ana")
GUEST = Principal("guest", "guest:1", "Mara")
OTHER_GUEST = Principal("guest", "guest:2", "Ion")
ITEM = {"kind": "item", "uri": "x-sonosapi-radio:1", "metadata": "<m/>", "title": "Radio"}


class _Page:
    def __init__(self, items, start, total):
        self.items, self.start, self.total = items, start, total

    def as_dict(self, host=""):
        return {"object_id": "Q:0", "start": self.start, "returned": len(self.items),
                "total": self.total, "has_more": self.start + len(self.items) < self.total,
                "items": [dict(i) for i in self.items]}


class _Controller:
    """Rooms A, B, C on household HH and X on HX; one queue per lead."""

    def __init__(self):
        self.homes = {"A": "HH", "B": "HH", "C": "HH", "X": "HX"}
        self.lead = {u: u for u in self.homes}
        self.zones = {u: SimpleNamespace(uuid=u, name=f"Room {u}", online=True, host=f"h{u}")
                      for u in self.homes}
        self.queues: dict[str, list[dict]] = {u: [] for u in self.homes}
        self.callbacks = []
        ctl = self

        class _Content:
            async def queue(self, host, start=0, count=200):
                items = ctl.queues[host[1:]]
                return _Page(items[start:start + count], start, len(items))
        self.content = _Content()

    def zone(self, uuid):
        return self.zones[uuid]

    def coordinator_of(self, uuid):
        return self.zones[self.lead[uuid]]

    def household_of(self, uuid):
        home = self.homes.get(uuid)
        return SimpleNamespace(id=home) if home else None

    def on_state(self, cb):
        self.callbacks.append(cb)


class _Commands:
    def __init__(self, ctl: _Controller):
        self.c, self.calls = ctl, []

    def _q(self, zone):
        return self.c.queues[self.c.lead[zone]]

    async def add_to_queue(self, zone, uri, metadata="", *, next_=False, position=0):
        self.calls.append(("add", zone, uri))
        self._q(zone).append({"id": f"Q:0/{len(self._q(zone)) + 1}", "uri": uri, "title": uri})
        return {"first_track": len(self._q(zone)), "added": 1}

    async def remove_from_queue(self, zone, index):
        self.calls.append(("remove", zone, index))
        del self._q(zone)[index - 1]

    async def join(self, zone, coordinator):
        self.c.lead[zone] = coordinator

    async def delegate(self, coordinator, heir):
        for u, lead in self.c.lead.items():
            if lead == coordinator:
                self.c.lead[u] = heir
        self.c.lead[coordinator] = coordinator

    async def set_volume(self, zone, level):
        self.calls.append(("volume", zone, level))

    async def set_group_volume(self, zone, level):
        self.calls.append(("group_volume", zone, level))

    async def play(self, zone):
        self.calls.append(("play", zone))

    async def pause(self, zone):
        self.calls.append(("pause", zone))

    async def next_track(self, zone):
        self.calls.append(("next", zone))

    async def previous_track(self, zone):
        self.calls.append(("previous", zone))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    ctl = _Controller()
    cmds = _Commands(ctl)
    monkeypatch.setitem(main.state, "controller", ctl)
    monkeypatch.setitem(main.state, "commands", cmds)
    sent: list[dict] = []

    async def fan_out(message):
        sent.append(message)
    monkeypatch.setattr(main, "_fan_out", fan_out)

    async def fake_apply(c, k, scene, metadata_for=None):
        before = {"name": scene["name"], "at": 1.0, "rooms": {}}
        for group in scene["groups"]:
            uuids = [r["uuid"] for r in group["rooms"]]
            for uuid in uuids:
                before["rooms"][uuid] = {"name": uuid, "coordinator": c.lead[uuid], "volume": 10}
            for uuid in uuids:
                c.lead[uuid] = uuids[0]
        return {"name": scene["name"], "set": [], "missing": [], "failed": [], "before": before}

    async def fake_revert(c, k, before):
        for uuid, room in before["rooms"].items():
            c.lead[uuid] = room["coordinator"]
        return {"name": "", "set": list(before["rooms"]), "missing": [], "failed": []}

    async def nothing(*a, **k):
        return []

    monkeypatch.setattr(scenes, "apply", fake_apply)
    monkeypatch.setattr(scenes, "revert", fake_revert)
    monkeypatch.setattr(scenes, "_settle_groups", nothing)
    monkeypatch.setattr(scenes, "_queue_items", nothing)
    routes.reset()
    yield SimpleNamespace(ctl=ctl, cmds=cmds, sent=sent)
    routes.reset()


def client(who: Principal) -> Client:
    c = Client(main.app, host="sonora.local")
    c.cookies[SESSION_COOKIE] = sign_session(who, 3600)
    return c


async def _start(who=USER, **body):
    return await client(who).request("POST", "/api/parties", body or {"speakers": ["A", "B"]})


async def test_start_and_broadcast(rig):
    r = await _start(speakers=["A", "B"])
    assert r.status == 200, r.body
    party = r.json()
    assert party["name"] == "Tudor's party" and party["speakers"] == ["A", "B"]
    assert party["owner"] == "sub-1"
    assert {"type": "parties"} in rig.sent
    listed = (await client(USER).request("GET", "/api/parties")).json()
    assert [p["id"] for p in listed["parties"]] == [party["id"]]
    assert listed["presets"] == []
    # the engine follows the controller
    assert len(rig.ctl.callbacks) == 1


async def test_conflict_is_409_then_take(rig):
    first = (await _start(speakers=["A"])).json()
    r = await _start(OTHER_USER, speakers=["A", "C"], name="Ana's")
    assert r.status == 409
    assert r.json() == {"error": "speaker_taken", "zone": "A", "room": "Room A",
                        "party_id": first["id"], "party_name": "Tudor's party"}
    r = await _start(OTHER_USER, speakers=["A", "C"], take=True)
    assert r.status == 200 and r.json()["speakers"] == ["A", "C"]


async def test_mixed_household_is_400(rig):
    r = await _start(speakers=["A", "X"])
    assert r.status == 400 and r.json() == {"error": "mixed_household"}


async def test_no_speakers_is_409(rig):
    rig.ctl.zones["A"].online = False
    r = await _start(speakers=["A"])
    assert r.status == 409 and r.json()["error"] == "no_speakers"


async def test_guest_cannot_start_end_or_touch_presets(rig):
    party = (await _start()).json()
    g = client(GUEST)
    assert (await g.request("POST", "/api/parties", {"speakers": ["C"]})).status == 403
    assert (await g.request("POST", f"/api/parties/{party['id']}/end")).status == 403
    assert (await g.request("POST", f"/api/parties/{party['id']}/speakers",
                            {"zone": "C"})).status == 403
    assert (await g.request("GET", "/api/presets")).status == 403
    assert (await g.request("POST", "/api/presets", {"name": "x"})).status == 403


async def test_guest_list_hides_presets(rig):
    await client(USER).request("POST", "/api/presets", {"name": "P", "speakers": ["A"]})
    await _start()
    listed = (await client(GUEST).request("GET", "/api/parties")).json()
    assert len(listed["parties"]) == 1 and listed["presets"] == []
    assert len((await client(USER).request("GET", "/api/parties")).json()["presets"]) == 1


async def test_display_sees_what_a_guest_sees(rig):
    """A TV screen is read-only and gets the guest's view: running parties,
    no presets, a party's queue."""
    from backend.home.auth import DisplayLinks
    link, _ = DisplayLinks().create("TV")
    tv = client(Principal("display", link["id"], "TV"))
    await client(USER).request("POST", "/api/presets", {"name": "P", "speakers": ["A"]})
    party = (await _start()).json()
    listed = (await tv.request("GET", "/api/parties")).json()
    assert [p["id"] for p in listed["parties"]] == [party["id"]]
    assert listed["presets"] == []
    await client(USER).request("POST", f"/api/parties/{party['id']}/queue",
                               {"uri": "x-file:1", "metadata": "<m/>"})
    q = await tv.request("GET", f"/api/parties/{party['id']}/queue")
    assert q.status == 200
    items = q.json()["items"]
    assert items and [i["can_remove"] for i in items] == [False] * len(items)
    assert (await tv.request("POST", f"/api/parties/{party['id']}/join")).status == 403
    assert (await tv.request("GET", "/api/presets")).status == 403


async def test_guest_queue_attribution_and_removal(rig):
    party = (await _start()).json()
    pid = party["id"]
    g, other = client(GUEST), client(OTHER_GUEST)
    assert (await g.request("POST", f"/api/parties/{pid}/join")).json() == {"ok": True}
    r = await g.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1", "metadata": "<m/>"})
    assert r.status == 200 and r.json()["ok"] is True
    assert ("add", "A", "x-file:1") in rig.cmds.calls
    await other.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:2", "metadata": "<m/>"})
    q = (await g.request("GET", f"/api/parties/{pid}/queue")).json()
    assert [(i["uri"], i["added_by"], i["can_remove"]) for i in q["items"]] == [
        ("x-file:1", {"id": "guest:1", "name": "Mara"}, True),
        ("x-file:2", {"id": "guest:2", "name": "Ion"}, False)]
    # not theirs
    r = await g.request("DELETE", f"/api/parties/{pid}/queue/2")
    assert r.status == 403 and r.json() == {"error": "not_yours"}
    assert len(rig.ctl.queues["A"]) == 2
    # theirs
    r = await g.request("DELETE", f"/api/parties/{pid}/queue/1")
    assert r.status == 200
    assert [i["uri"] for i in rig.ctl.queues["A"]] == ["x-file:2"]
    listed = (await g.request("GET", "/api/parties")).json()["parties"][0]
    assert listed["queue_owners"] == {"x-file:2": [{"id": "guest:2", "name": "Ion"}]}
    assert [m["name"] for m in listed["members"]] == ["Mara"]
    # a user removes anything
    assert (await client(USER).request("DELETE", f"/api/parties/{pid}/queue/1")).status == 200


async def test_duplicate_uris_match_their_own_adder(rig):
    pid = (await _start()).json()["id"]
    g, other = client(GUEST), client(OTHER_GUEST)
    await other.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1", "metadata": "m"})
    await g.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1", "metadata": "m"})
    q = (await g.request("GET", f"/api/parties/{pid}/queue")).json()["items"]
    assert [i["can_remove"] for i in q] == [False, True]
    assert (await g.request("DELETE", f"/api/parties/{pid}/queue/1")).status == 403
    assert (await g.request("DELETE", f"/api/parties/{pid}/queue/2")).status == 200
    q = (await other.request("GET", f"/api/parties/{pid}/queue")).json()["items"]
    assert [(i["added_by"]["name"], i["can_remove"]) for i in q] == [("Ion", True)]


async def test_transport_and_volume(rig):
    pid = (await _start()).json()["id"]
    g = client(GUEST)
    for action in ("play", "pause", "next", "previous"):
        assert (await g.request("POST", f"/api/parties/{pid}/transport/{action}")).status == 200
    assert [c for c in rig.cmds.calls if c[0] in ("play", "pause", "next", "previous")] == [
        ("play", "A"), ("pause", "A"), ("next", "A"), ("previous", "A")]
    r = await g.request("POST", f"/api/parties/{pid}/volume", {"zone": "C", "level": 20})
    assert r.status == 400 and r.json() == {"error": "not_in_party", "zone": "C"}
    assert (await g.request("POST", f"/api/parties/{pid}/volume", {"zone": "B", "level": 20})).status == 200
    assert (await g.request("POST", f"/api/parties/{pid}/volume", {"level": 30})).status == 200
    assert ("volume", "B", 20) in rig.cmds.calls and ("group_volume", "A", 30) in rig.cmds.calls
    assert (await g.request("POST", f"/api/parties/{pid}/volume", {"level": 101})).status == 422


async def test_unknown_party_is_404(rig):
    g = client(GUEST)
    assert (await g.request("POST", "/api/parties/nope/join")).status == 404
    assert (await g.request("GET", "/api/parties/nope/queue")).status == 404
    assert (await client(USER).request("POST", "/api/parties/nope/end")).status == 404


async def test_speakers_and_end(rig):
    pid = (await _start(speakers=["A"])).json()["id"]
    u = client(USER)
    r = await u.request("POST", f"/api/parties/{pid}/speakers", {"zone": "B"})
    assert r.status == 200 and r.json()["speakers"] == ["A", "B"]
    r = await u.request("DELETE", f"/api/parties/{pid}/speakers/B")
    assert r.status == 200 and r.json()["speakers"] == ["A"]
    other = (await _start(OTHER_USER, speakers=["C"])).json()
    r = await u.request("POST", f"/api/parties/{pid}/speakers", {"zone": "C"})
    assert r.status == 409 and r.json()["party_id"] == other["id"]
    r = await u.request("POST", f"/api/parties/{pid}/end")
    assert r.status == 200 and r.json()["id"] == pid
    assert [p["id"] for p in (await u.request("GET", "/api/parties")).json()["parties"]] == [other["id"]]


async def test_presets_crud_and_start(rig):
    u = client(USER)
    r = await u.request("POST", "/api/presets", {"name": "Dinner", "speakers": ["A", "B"],
                                                  "volumes": {"A": 30}, "music": ITEM})
    assert r.status == 200, r.body
    preset = r.json()
    r = await u.request("POST", "/api/presets", {"name": "Bad", "speakers": ["A", "nope"]})
    assert r.status == 400 and r.json()["error"] == "invalid"
    r = await u.request("PUT", f"/api/presets/{preset['id']}", {**preset, "name": "Supper"})
    assert r.status == 200 and r.json()["name"] == "Supper"
    assert (await u.request("PUT", "/api/presets/nope", {"name": "x", "speakers": ["A"]})).status == 404
    assert [p["name"] for p in (await u.request("GET", "/api/presets")).json()["presets"]] == ["Supper"]
    # conflict, then take
    blocker = (await _start(OTHER_USER, speakers=["B"])).json()
    r = await u.request("POST", f"/api/presets/{preset['id']}/start", {})
    assert r.status == 409 and r.json()["party_id"] == blocker["id"]
    r = await u.request("POST", f"/api/presets/{preset['id']}/start", {"take": True})
    assert r.status == 200
    party = r.json()
    assert party["name"] == "Supper" and party["preset_id"] == preset["id"]
    assert party["speakers"] == ["A", "B"] and party["volumes"] == {"A": 30}
    assert (await u.request("DELETE", f"/api/presets/{preset['id']}")).status == 200
    assert (await u.request("DELETE", f"/api/presets/{preset['id']}")).status == 404


async def test_start_from_preset_id(rig):
    u = client(USER)
    preset = (await u.request("POST", "/api/presets", {"name": "P", "speakers": ["C"]})).json()
    r = await _start(preset_id=preset["id"])
    assert r.status == 200 and r.json()["speakers"] == ["C"] and r.json()["name"] == "P"


async def test_sonora_enqueue_on_a_party_speaker_is_attributed(rig, monkeypatch):
    pid = (await _start()).json()["id"]
    u = client(USER)
    # B follows A: its queue is the party's
    r = await u.request("POST", "/api/zones/B/source",
                        {"uri": "x-file:9", "metadata": "<m/>", "enqueue": True})
    assert r.status == 200, r.body
    q = (await u.request("GET", f"/api/parties/{pid}/queue")).json()["items"]
    assert [(i["uri"], i["added_by"]) for i in q] == [("x-file:9", {"id": "sub-1", "name": "Tudor"})]
    # not a party speaker: nothing recorded
    await u.request("POST", "/api/zones/C/source", {"uri": "x-file:8", "metadata": "", "enqueue": True})
    assert list((await u.request("GET", "/api/parties")).json()["parties"][0]["queue_owners"]) == ["x-file:9"]
    # Sonora's own remove drops the adder
    assert (await u.request("DELETE", "/api/queue/A/1")).status == 200
    assert (await u.request("GET", "/api/parties")).json()["parties"][0]["queue_owners"] == {}


async def test_party_notice_is_broadcast_as_is(rig):
    pid = (await _start()).json()["id"]
    rig.sent.clear()
    rig.ctl.lead["B"] = "B"          # another app took B away
    await rig.ctl.callbacks[0]({"type": "snapshot"})
    assert rig.sent == [{"type": "party_notice", "party": pid, "zone": "B", "event": "left"},
                        {"type": "parties"}]


async def test_adders_belong_to_the_last_copies(rig):
    pid = (await _start()).json()["id"]
    rig.ctl.queues["A"].append({"id": "Q:0/1", "uri": "x-file:1", "title": "before"})
    g = client(GUEST)
    await g.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1", "metadata": "m"})
    q = (await g.request("GET", f"/api/parties/{pid}/queue")).json()["items"]
    assert [(i["added_by"], i["can_remove"]) for i in q] == [
        (None, False), ({"id": "guest:1", "name": "Mara"}, True)]
    r = await g.request("DELETE", f"/api/parties/{pid}/queue/1")
    assert r.status == 403 and r.json() == {"error": "not_yours"}
    assert (await g.request("DELETE", f"/api/parties/{pid}/queue/2")).status == 200
    assert [i["title"] for i in rig.ctl.queues["A"]] == ["before"]
    assert (await g.request("GET", "/api/parties")).json()["parties"][0]["queue_owners"] == {}


async def test_mid_queue_inserts_are_not_attributed(rig):
    await _start()
    u = client(USER)
    for extra in ({"next": True}, {"position": 1}):
        r = await u.request("POST", "/api/zones/A/source",
                            {"uri": "x-file:5", "metadata": "m", "enqueue": True, **extra})
        assert r.status == 200
    assert (await u.request("GET", "/api/parties")).json()["parties"][0]["queue_owners"] == {}


async def test_queue_start_is_capped(rig):
    pid = (await _start()).json()["id"]
    g = client(GUEST)
    assert (await g.request("GET", f"/api/parties/{pid}/queue?start=10001")).status == 422
    await g.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1", "metadata": "m"})
    r = (await g.request("GET", f"/api/parties/{pid}/queue?start=5")).json()
    assert r["items"] == [] and r["total"] == 1 and r["has_more"] is False


async def test_engine_failure_is_logged_once(rig, monkeypatch, caplog):
    made = []

    def broken(*a, **k):
        made.append(1)
        raise TypeError("corrupt parties.json")
    monkeypatch.setattr(routes, "PartyEngine", broken)
    u = client(USER)
    for _ in range(3):
        r = await u.request("GET", "/api/parties")
        assert r.status == 503 and r.json() == {"error": "parties_unavailable"}
    assert len(made) == 1
    assert sum("could not be made" in rec.getMessage() for rec in caplog.records) == 1


async def test_queue_body_is_capped(rig):
    pid = (await _start()).json()["id"]
    g = client(GUEST)
    r = await g.request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:" + "a" * routes.URI_MAX})
    assert r.status == 422
    r = await g.request("POST", f"/api/parties/{pid}/queue",
                        {"uri": "x-file:1", "metadata": "m" * (routes.METADATA_MAX + 1)})
    assert r.status == 422
    ok = await g.request("POST", f"/api/parties/{pid}/queue",
                         {"uri": "x-file:" + "a" * (routes.URI_MAX - 7), "metadata": "m" * routes.METADATA_MAX})
    assert ok.status == 200, ok.body


async def test_engine_reads_the_speakers_once_it_is_made(rig, tmp_path):
    import asyncio
    pid = (await _start(speakers=["A", "B"])).json()["id"]
    routes.reset()                                    # Sonora restarts ...
    rig.ctl.callbacks.clear()
    rig.ctl.lead["B"] = "B"                           # ... and B left while it was down
    rig.sent.clear()
    await client(USER).request("GET", "/api/parties")  # the first call makes the engine
    await asyncio.gather(*list(routes._tasks))
    assert {"type": "party_notice", "party": pid, "zone": "B", "event": "left"} in rig.sent
    assert routes.engine().get(pid)["speakers"] == ["A"]


async def test_engine_first_read_failure_is_logged(rig, monkeypatch, caplog):
    import asyncio
    from backend.home.parties import PartyEngine

    async def broken(self, message):
        raise RuntimeError("controller hiccup")
    monkeypatch.setattr(PartyEngine, "on_state", broken)
    r = await client(USER).request("GET", "/api/parties")
    assert r.status == 200
    await asyncio.gather(*list(routes._tasks))
    assert any("first read of the speakers failed" in rec.getMessage() for rec in caplog.records)
