"""Parties: who owns which speaker, the way back, attribution, outside changes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from backend.home import parties
from backend.home.parties import MixedHousehold, NoSpeakers, PartyEngine, SpeakerTaken
from backend.sonos import scenes


@dataclass(frozen=True)
class _Who:
    kind: str
    id: str
    name: str

    @property
    def is_guest(self) -> bool:
        return self.kind == "guest"

    @property
    def is_user(self) -> bool:
        return self.kind == "user"


ANA = _Who("user", "u_ana", "Ana")
GUEST = _Who("guest", "g_1", "Guest")
OTHER_GUEST = _Who("guest", "g_2", "Other")


class _Controller:
    """Rooms A, B, C on household HH and X on HX, each leading itself."""

    def __init__(self) -> None:
        self.homes = {"A": "HH", "B": "HH", "C": "HH", "X": "HX"}
        self.lead = {u: u for u in self.homes}
        self.zones = {u: SimpleNamespace(uuid=u, name=f"Room {u}", online=True) for u in self.homes}
        self.callbacks: list = []

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
    def __init__(self, controller: _Controller) -> None:
        self.c = controller
        self.calls: list[tuple] = []
        #: When set, a join is heard back from the speakers only this many
        #: seconds later, with a stale topology event halfway there.
        self.late: float | None = None

    def _fire(self):
        for cb in self.c.callbacks:
            asyncio.ensure_future(cb({"type": "snapshot"}))

    async def join(self, zone, coordinator):
        self.calls.append(("join", zone, coordinator))
        if self.late is None:
            self.c.lead[zone] = coordinator
            return
        loop = asyncio.get_running_loop()
        loop.call_later(self.late / 2, self._fire)

        def heard():
            self.c.lead[zone] = coordinator
            self._fire()
        loop.call_later(self.late, heard)

    async def delegate(self, coordinator, heir):
        self.calls.append(("delegate", coordinator, heir))
        for u, lead in self.c.lead.items():
            if lead == coordinator:
                self.c.lead[u] = heir
        self.c.lead[coordinator] = coordinator

    async def set_source(self, zone, uri, metadata=""):
        self.calls.append(("set_source", zone, uri))

    async def seek_track(self, zone, index):
        self.calls.append(("seek_track", zone, index))

    async def play(self, zone):
        self.calls.append(("play", zone))


@pytest.fixture
def rig(monkeypatch, tmp_path):
    controller = _Controller()
    commands = _Commands(controller)
    applied: list[dict] = []
    reverted: list[dict] = []

    async def fake_apply(ctrl, cmds, scene, metadata_for=None):
        applied.append(scene)
        before = {"name": scene["name"], "at": 100.0, "rooms": {}}
        for group in scene["groups"]:
            uuids = [r["uuid"] for r in group["rooms"]]
            for uuid in uuids:
                before["rooms"][uuid] = {"name": f"Room {uuid}", "coordinator": ctrl.lead[uuid],
                                         "volume": 10, "marker": f"{uuid}@{len(applied)}"}
            for uuid in uuids:
                ctrl.lead[uuid] = uuids[0]
        return {"name": scene["name"], "set": [], "missing": [], "failed": [], "before": before}

    async def fake_revert(ctrl, cmds, before):
        reverted.append(before)
        for uuid, room in before["rooms"].items():
            ctrl.lead[uuid] = room["coordinator"]
        return {"name": before.get("name", ""), "set": list(before["rooms"]), "missing": [], "failed": []}

    monkeypatch.setattr(scenes, "apply", fake_apply)
    monkeypatch.setattr(scenes, "revert", fake_revert)
    notices: list[dict] = []

    async def notify(message):
        notices.append(message)

    def engine():
        return PartyEngine(tmp_path / "parties.json", controller, commands, None, notify)

    return SimpleNamespace(controller=controller, commands=commands, applied=applied,
                           reverted=reverted, notices=notices, engine=engine, path=tmp_path / "parties.json")


def run(coro):
    return asyncio.run(coro)


def test_start_groups_and_snapshots(rig):
    engine = rig.engine()
    party = run(engine.start(name="Dinner", speakers=["A", "B"], by=ANA, volumes={"A": 30}))
    assert len(rig.applied) == 1
    group = rig.applied[0]["groups"]
    assert len(group) == 1 and [r["uuid"] for r in group[0]["rooms"]] == ["A", "B"]
    assert group[0]["rooms"][0]["volume"] == 30 and group[0]["rooms"][1]["volume"] is None
    assert group[0]["play"]["kind"] == "silence" and group[0]["household"] == "HH"
    assert engine.owner_of("A") == party["id"] and engine.owner_of("B") == party["id"]
    assert engine.owner_of("C") is None
    assert party["owner"] == "u_ana" and party["owner_name"] == "Ana" and party["household"] == "HH"
    assert party["speakers"] == ["A", "B"] and party["state"] == "playing"
    listed = engine.list()
    assert [p["id"] for p in listed] == [party["id"]] and listed[0]["queue_owners"] == {}


def test_taken_speaker_needs_confirmation(rig):
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A"], by=ANA))
    with pytest.raises(SpeakerTaken) as err:
        run(engine.start(name="Two", speakers=["A"], by=GUEST))
    assert (err.value.zone, err.value.party_id, err.value.party_name) == ("A", one["id"], "One")
    assert len(rig.applied) == 1
    two = run(engine.start(name="Two", speakers=["A"], by=GUEST, take=True))
    assert engine.owner_of("A") == two["id"]
    first = next(p for p in engine.list() if p["id"] == one["id"])
    assert first["speakers"] == [] and first["state"] == "paused"


def test_taking_the_lead_hands_the_group_to_the_next_speaker(rig):
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    run(engine.start(name="Two", speakers=["A"], by=ANA, take=True))
    assert ("delegate", "A", "B") in rig.commands.calls
    first = next(p for p in engine.list() if p["id"] == one["id"])
    assert first["speakers"] == ["B"] and first["state"] == "playing"


def test_end_returns_moved_speaker_to_running_party(rig):
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    two = run(engine.start(name="Two", speakers=["B"], by=ANA, take=True))
    rig.commands.calls.clear()
    run(engine.end(two["id"]))
    assert ("join", "B", "A") in rig.commands.calls
    assert rig.reverted == []
    assert engine.owner_of("B") == one["id"]
    assert [p["id"] for p in engine.list()] == [one["id"]]
    assert engine.list()[0]["speakers"] == ["A", "B"]
    # Ending the first party now puts B back as it was before any party.
    run(engine.end(one["id"]))
    assert rig.reverted[-1]["rooms"]["B"]["marker"] == "B@1"
    assert rig.reverted[-1]["rooms"]["A"]["marker"] == "A@1"


def test_end_restores_pre_party_state(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    run(engine.end(party["id"]))
    assert len(rig.reverted) == 1
    rooms = rig.reverted[0]["rooms"]
    assert set(rooms) == {"A", "B"} and rooms["A"]["marker"] == "A@1" and rooms["B"]["marker"] == "B@1"
    assert engine.list() == [] and engine.owner_of("A") is None


def test_end_after_underlying_party_ended_goes_back_to_before_any_party(rig):
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    two = run(engine.start(name="Two", speakers=["B"], by=ANA, take=True))
    run(engine.end(one["id"]))
    assert set(rig.reverted[-1]["rooms"]) == {"A"}
    run(engine.end(two["id"]))
    assert rig.reverted[-1]["rooms"]["B"]["marker"] == "B@1"


def test_add_and_remove_speaker(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    run(engine.add_speaker(party["id"], "C", ANA))
    assert ("join", "C", "A") in rig.commands.calls
    assert engine.owner_of("C") == party["id"] and engine.list()[0]["speakers"] == ["A", "C"]
    run(engine.remove_speaker(party["id"], "C"))
    assert engine.owner_of("C") is None and rig.reverted[-1]["rooms"]["C"]["marker"] == "C@2"
    other = run(engine.start(name="Two", speakers=["B"], by=ANA))
    with pytest.raises(SpeakerTaken):
        run(engine.add_speaker(party["id"], "B", ANA))
    with pytest.raises(MixedHousehold):
        run(engine.add_speaker(party["id"], "X", ANA))
    run(engine.add_speaker(party["id"], "B", ANA, take=True))
    assert engine.owner_of("B") == party["id"]
    assert next(p for p in engine.list() if p["id"] == other["id"])["state"] == "paused"


def test_outside_change_drops_speaker(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    rig.commands.calls.clear()                       # the start pointed A at its queue
    rig.controller.lead["B"] = "B"                   # another app took B out of the group
    run(engine.on_state({"type": "snapshot"}))
    assert rig.notices == [{"type": "party_notice", "party": party["id"], "zone": "B", "event": "left"}]
    assert engine.owner_of("B") is None and engine.list()[0]["speakers"] == ["A"]
    assert engine.list()[0]["state"] == "playing"
    assert rig.commands.calls == [] and rig.reverted == []   # never regroups
    # Nothing changed: no further notice.
    run(engine.on_state({"type": "snapshot"}))
    assert len(rig.notices) == 1


def test_outside_change_ignores_other_messages(rig):
    engine = rig.engine()
    run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    rig.controller.lead["B"] = "B"
    run(engine.on_state({"type": "library"}))
    assert rig.notices == []


def test_guest_can_remove_only_own(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    engine.record_add(party["id"], "x-file:1", GUEST)
    engine.record_add(party["id"], "x-file:1", OTHER_GUEST)
    engine.record_add(party["id"], "x-file:2", OTHER_GUEST)
    assert engine.added_by(party["id"], "x-file:1") == ["g_1", "g_2"]
    assert engine.can_remove(party["id"], "x-file:1", GUEST)
    assert not engine.can_remove(party["id"], "x-file:1", OTHER_GUEST)
    assert not engine.can_remove(party["id"], "x-file:2", GUEST)
    assert not engine.can_remove(party["id"], "x-file:9", GUEST)
    assert engine.can_remove(party["id"], "x-file:2", ANA)
    # anything that is not a user (a display, a future kind) is never one
    for kind in ("display", "robot"):
        assert not engine.can_remove(party["id"], "x-file:2", _Who(kind, "d_1", "TV"))
    engine.record_remove(party["id"], "x-file:1")
    assert engine.added_by(party["id"], "x-file:1") == ["g_2"]
    assert engine.can_remove(party["id"], "x-file:1", OTHER_GUEST)
    owners = engine.list()[0]["queue_owners"]
    assert owners["x-file:1"] == [{"id": "g_2", "name": "Other"}]


def test_reload_after_restart(rig):
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    run(engine.start(name="Two", speakers=["B"], by=ANA, take=True))
    engine.record_add(one["id"], "x-file:1", GUEST)
    again = rig.engine()
    assert again.list() == engine.list()
    assert again.owner_of("A") == one["id"] and again.added_by(one["id"], "x-file:1") == ["g_1"]
    run(again.end(one["id"]))
    assert rig.reverted[-1]["rooms"]["A"]["marker"] == "A@1"
    assert again.added_by(one["id"], "x-file:1") == []
    third = rig.engine()
    assert [p["name"] for p in third.list()] == ["Two"]
    run(third.end(third.list()[0]["id"]))
    assert rig.reverted[-1]["rooms"]["B"]["marker"] == "B@1"
    assert rig.engine().list() == []


def test_junk_file_starts_empty(rig):
    rig.path.write_text("not json")
    assert rig.engine().list() == []


def test_mixed_household_rejected(rig):
    engine = rig.engine()
    with pytest.raises(MixedHousehold):
        run(engine.start(name="Both", speakers=["A", "X"], by=ANA))
    assert rig.applied == [] and engine.list() == []


def test_lead_moved_away_lets_the_lead_go(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B", "C"], by=ANA))
    # Another app took A out; the speakers handed the group to B.
    rig.controller.lead.update({"A": "A", "B": "B", "C": "B"})
    run(engine.on_state({"type": "snapshot"}))
    assert rig.notices == [{"type": "party_notice", "party": party["id"], "zone": "A", "event": "left"}]
    assert engine.list()[0]["speakers"] == ["B", "C"] and engine.owner_of("A") is None
    assert rig.engine().list()[0]["speakers"] == ["B", "C"]


def test_late_topology_does_not_drop_a_speaker_just_placed(rig):
    engine = rig.engine()
    rig.controller.on_state(engine.on_state)
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    rig.commands.late = 0.2

    async def go():
        await engine.add_speaker(party["id"], "C", ANA)
        await asyncio.sleep(0.3)
    run(go())
    assert rig.notices == []
    assert engine.owner_of("C") == party["id"] and engine.list()[0]["speakers"] == ["A", "C"]
    # The way back is still there.
    run(engine.remove_speaker(party["id"], "C"))
    assert rig.reverted[-1]["rooms"]["C"]["marker"] == "C@2"


def test_changes_run_one_at_a_time(rig, monkeypatch):
    fake = scenes.apply

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return await fake(*args, **kwargs)
    engine = rig.engine()
    other = run(engine.start(name="Other", speakers=["B", "C"], by=ANA))
    monkeypatch.setattr(scenes, "apply", slow)

    async def go():
        first = asyncio.ensure_future(engine.start(name="One", speakers=["A"], by=ANA))
        second = asyncio.ensure_future(engine.start(name="Two", speakers=["A"], by=ANA))
        await asyncio.sleep(0.01)
        # Read while a change holds the engine: not acted on.
        rig.controller.lead["C"] = "C"
        await engine.on_state({"type": "snapshot"})
        assert rig.notices == []
        return await asyncio.gather(first, second, return_exceptions=True)
    results = run(go())
    assert sum(isinstance(r, SpeakerTaken) for r in results) == 1
    assert sum(isinstance(r, dict) for r in results) == 1
    assert len([p for p in engine.list() if "A" in p["speakers"]]) == 1
    run(engine.on_state({"type": "snapshot"}))
    assert rig.notices == [{"type": "party_notice", "party": other["id"], "zone": "C", "event": "left"}]


def test_start_checks_the_scene_before_touching_other_parties(rig):
    engine = rig.engine()
    run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    rig.commands.calls.clear()
    with pytest.raises(ValueError):
        run(engine.start(name="  ", speakers=["A"], by=ANA, take=True))
    assert rig.commands.calls == [] and engine.list()[0]["speakers"] == ["A", "B"]


def test_no_way_back_refuses_the_party(rig, monkeypatch):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["B"], by=ANA))
    rig.controller.zones["C"].online = False
    with pytest.raises(NoSpeakers):
        run(engine.add_speaker(party["id"], "C", ANA))
    rig.controller.zones["A"].online = False
    with pytest.raises(NoSpeakers):
        run(engine.start(name="Off", speakers=["A", "C"], by=ANA))
    assert len(rig.applied) == 1
    rig.controller.zones["A"].online = True

    async def no_record(ctrl, cmds, scene, metadata_for=None):
        return {"name": scene["name"], "set": [], "missing": [], "failed": [], "before": None}
    monkeypatch.setattr(scenes, "apply", no_record)
    with pytest.raises(NoSpeakers):
        run(engine.start(name="Blind", speakers=["A"], by=ANA))
    assert [p["name"] for p in engine.list()] == ["One"] and engine.owner_of("A") is None


# -- the queue the party found, through the real scene machinery --------------


class _Transport(SimpleNamespace):
    is_home_theater = False

    @property
    def source(self):
        return "queue" if self.media_uri.startswith("x-rincon-queue:") else ""


class _SceneController:
    """Room A alone, playing its own queue at track 2."""

    def __init__(self):
        self.zones = {"A": SimpleNamespace(
            uuid="A", name="Kitchen", host="10.0.0.1", online=True, muted=False, volume=20, bass=0, treble=0,
            loudness=False, media_metadata="", model_number="S1",
            transport=_Transport(media_uri="x-rincon-queue:A#0", track_uri="", state="PLAYING", play_mode="NORMAL",
                                 container_title="", title="", album_art_uri="", track_number=2, rel_time="0:00:10"))}
        group = SimpleNamespace(coordinator_uuid="A", zone_uuids=["A"])
        self.home = SimpleNamespace(id="HH", groups={"GA": group}, group_for_zone=lambda u: group)
        self.households = {"HH": self.home}

    def zone(self, uuid):
        return self.zones[uuid]

    def coordinator_of(self, uuid):
        return self.zones[uuid]

    def household_of(self, uuid):
        return self.home

    def account_udn(self, household_id, sid, serial=""):
        return ""


class _SceneSoap:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    async def call(self, host, service, action, args=None, **kwargs):
        self.calls.append((action, dict(args or {})))
        values = {}
        if action == "Browse":
            start = args["StartingIndex"]
            page = self.queue[start:start + args["RequestedCount"]]
            values = {"Result": "".join(f'<item id="Q:0/{i}"><res protocolInfo="x">{u}</res></item>'
                                        for i, u in enumerate(page, start + 1)),
                      "TotalMatches": str(len(self.queue))}
        return SimpleNamespace(get=lambda k, d="": values.get(k, d))


class _SceneCommands:
    def __init__(self, soap):
        self.soap = soap
        self.calls = []

    def _queueable(self, uri):
        return True

    @staticmethod
    def _has_speech_switch(state):
        return False

    async def sleep_timer_remaining(self, uuid):
        return ""

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, *args))
        return call


def test_end_puts_back_the_queue_the_party_found(tmp_path, monkeypatch):
    monkeypatch.setattr(scenes, "GROUP_SETTLE", 0)
    controller = _SceneController()
    soap = _SceneSoap(["x-file:1", "x-file:2"])
    commands = _SceneCommands(soap)

    async def notify(message):
        pass
    engine = PartyEngine(tmp_path / "parties.json", controller, commands, None, notify)
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    assert ("pause", "A") in commands.calls                # silence: the party starts quiet
    # A guest fills the queue during the party.
    engine.record_add(party["id"], "x-file:guest", GUEST)
    soap.queue.append("x-file:guest")
    commands.calls.clear()
    soap.calls.clear()
    run(engine.end(party["id"]))
    assert ("clear_queue", "A") in commands.calls
    adds = [args for action, args in soap.calls if action == "AddMultipleURIsToQueue"]
    assert [a["EnqueuedURIs"] for a in adds] == ["x-file:1 x-file:2"]
    assert ("set_source", "A", "x-rincon-queue:A#0", "") in commands.calls
    seeks = [args for action, args in soap.calls if action == "Seek"]
    assert seeks and seeks[0]["Target"] == 2


def test_a_quiet_party_on_airplay_takes_the_speaker_to_its_queue_and_back(tmp_path, monkeypatch):
    """The hardware case through the real scene machinery: the Roam plays the
    iPhone over AirPlay; the party points it at its queue once what it was
    is written down, and ending the party hands AirPlay back."""
    monkeypatch.setattr(scenes, "GROUP_SETTLE", 0)
    controller = _SceneController()
    airplay = "x-sonos-vli:RINCON_C43875C3DCEC01400:1,airplay:04470cd8"
    controller.zones["A"].transport.media_uri = airplay
    controller.zones["A"].transport.track_number = 1
    soap = _SceneSoap(["x-file:1"])

    class _Following(_SceneCommands):
        async def set_source(self, zone, uri, metadata=""):
            self.calls.append(("set_source", zone, uri, metadata))
            controller.zones["A"].transport.media_uri = uri
    commands = _Following(soap)

    async def notify(message):
        pass
    engine = PartyEngine(tmp_path / "parties.json", controller, commands, None, notify)
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    assert commands.calls.index(("pause", "A")) < commands.calls.index(("set_source", "A", "x-rincon-queue:A#0", ""))
    assert engine._parties[party["id"]].own_source is None
    commands.calls.clear()
    run(engine.end(party["id"]))
    assert ("set_source", "A", airplay, "") in commands.calls
    assert ("play", "A") in commands.calls


# -- last action wins: something else played on the party's speakers ----------


def _plays(rig, zone, uri):
    rig.controller.zones[zone].transport = SimpleNamespace(media_uri=uri)


def test_something_else_played_lets_the_whole_party_go(rig):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    _plays(rig, "A", "x-rincon-queue:A#0")            # the party's own queue
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [] and engine.list()[0]["speakers"] == ["A", "B"]
    rig.commands.calls.clear()
    _plays(rig, "A", "x-sonosapi-stream:s1234")       # the Sonos app played a station
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [{"type": "party_notice", "party": party["id"], "zone": z, "event": "left"}
                           for z in ("A", "B")]
    listed = engine.list()[0]
    assert listed["speakers"] == [] and listed["state"] == "paused"
    assert engine.owner_of("A") is None and engine.owner_of("B") is None
    assert rig.commands.calls == [] and rig.reverted == []   # never fights back
    run(engine.on_state({"type": "zone"}))
    assert len(rig.notices) == 2
    # Ending it later owes the speakers nothing.
    run(engine.end(party["id"]))
    assert rig.reverted == []


def test_a_report_from_before_the_party_played_is_not_read_as_a_change(rig):
    engine = rig.engine()
    _plays(rig, "A", "x-sonosapi-stream:old")         # what A played before the party
    run(engine.start(name="One", speakers=["A"], by=ANA))
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [] and engine.list()[0]["speakers"] == ["A"]
    _plays(rig, "A", "x-rincon-queue:A#0")
    run(engine.on_state({"type": "zone"}))
    _plays(rig, "A", "x-sonosapi-stream:new")
    run(engine.on_state({"type": "zone"}))
    assert [n["zone"] for n in rig.notices] == ["A"]


def test_the_party_own_sources_are_not_outside_changes(rig):
    rig.commands._queueable = lambda uri: not uri.startswith("x-sonosapi-stream:")
    engine = rig.engine()
    run(engine.start(name="TV", speakers=["A"], by=ANA, music={"kind": "tv", "source": "A"}))
    run(engine.start(name="Line", speakers=["B"], by=ANA, music={"kind": "line_in"}))
    run(engine.start(name="Radio", speakers=["C"], by=ANA,
                     music={"kind": "item", "uri": "x-sonosapi-stream:s1", "title": "R"}))
    _plays(rig, "A", "x-sonos-htastream:A:spdif")
    _plays(rig, "B", "x-rincon-stream:B")
    _plays(rig, "C", "x-sonosapi-stream:s1")
    run(engine.on_state({"type": "zone"}))
    # live sound from Sonora is the party's own too
    _plays(rig, "A", "x-rincon-mp3radio://10.0.0.5:8000/api/home/live/k1.mp3")
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == []
    assert [p["speakers"] for p in engine.list()] == [["A"], ["B"], ["C"]]
    _plays(rig, "B", "x-sonosapi-stream:s9")
    run(engine.on_state({"type": "zone"}))
    assert [n["zone"] for n in rig.notices] == ["B"]


AIRPLAY = "x-sonos-vli:RINCON_C43875C3DCEC01400:1,airplay:04470cd8"


def _loaded(monkeypatch, uri):
    """Every room the scene writes down had ``uri`` loaded, as a lead does."""
    fake = scenes.apply

    async def with_transport(ctrl, cmds, scene, metadata_for=None):
        report = await fake(ctrl, cmds, scene, metadata_for)
        for room in report["before"]["rooms"].values():
            room["transport"] = {"media_uri": uri}
        return report
    monkeypatch.setattr(scenes, "apply", with_transport)


def test_a_quiet_party_points_its_lead_at_its_own_queue(rig, monkeypatch):
    # Real hardware: a Roam on the owner's iPhone over AirPlay, party started
    # with no music. The party's own source is its queue, never the AirPlay.
    _loaded(monkeypatch, AIRPLAY)
    engine = rig.engine()
    party = run(engine.start(name="Quiet", speakers=["A", "B"], by=ANA))
    assert rig.commands.calls[-1] == ("set_source", "A", "x-rincon-queue:A#0")
    assert ("play", "A") not in rig.commands.calls           # left quiet
    assert engine._parties[party["id"]].own_source is None
    # A report from before the switch is not read as someone else's doing.
    _plays(rig, "A", AIRPLAY)
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [] and engine.list()[0]["speakers"] == ["A", "B"]
    _plays(rig, "A", "x-rincon-queue:A#0")
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == []
    # seen on its queue, so the iPhone taking the speaker back is someone else's
    _plays(rig, "A", AIRPLAY)
    run(engine.on_state({"type": "zone"}))
    assert [n["zone"] for n in rig.notices] == ["A", "B"]
    # and ending it restores the way back the scene wrote down
    rig.reverted.clear()
    run(engine.end(party["id"]))
    assert rig.reverted == []                                # let go: nothing owed


def test_ending_a_quiet_party_reverts_to_what_the_lead_played(rig, monkeypatch):
    _loaded(monkeypatch, AIRPLAY)
    engine = rig.engine()
    party = run(engine.start(name="Quiet", speakers=["A"], by=ANA))
    run(engine.end(party["id"]))
    assert rig.reverted[-1]["rooms"]["A"]["transport"]["media_uri"] == AIRPLAY


def test_a_quiet_party_already_on_its_queue_is_left_there(rig, monkeypatch):
    _loaded(monkeypatch, "x-rincon-queue:A#0")
    engine = rig.engine()
    run(engine.start(name="Quiet", speakers=["A"], by=ANA))
    assert not any(c[0] == "set_source" for c in rig.commands.calls)


def test_starting_music_is_not_pointed_at_the_queue(rig, monkeypatch):
    _loaded(monkeypatch, AIRPLAY)
    rig.commands._queueable = lambda uri: not uri.startswith("x-sonosapi-stream:")
    engine = rig.engine()
    run(engine.start(name="TV", speakers=["A"], by=ANA, music={"kind": "tv", "source": "A"}))
    run(engine.start(name="Line", speakers=["B"], by=ANA, music={"kind": "line_in"}))
    run(engine.start(name="Radio", speakers=["C"], by=ANA,
                     music={"kind": "item", "uri": "x-sonosapi-stream:s1", "title": "R"}))
    assert not any(c[0] == "set_source" for c in rig.commands.calls)
    assert [p["own_source"] for p in engine.list()] == [
        "x-sonos-htastream:A:spdif", "x-rincon-stream:B", "x-sonosapi-stream:s1"]


# -- a song added while the lead plays something else -------------------------


def _party_on_airplay(rig):
    """A party whose lead is on AirPlay and seen so: one from before the fix,
    which took what its lead had loaded as its own."""
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    held = engine._parties[party["id"]]
    held.own_source, held.seen = AIRPLAY, True
    _plays(rig, "A", AIRPLAY)
    rig.commands.calls.clear()
    return engine, party["id"]


def test_a_song_added_off_the_queue_switches_the_lead_to_it(rig):
    engine, pid = _party_on_airplay(rig)
    assert run(engine.play_added(pid, "x-file:9", {"FirstTrackNumberEnqueued": "3"})) is True
    assert rig.commands.calls == [("set_source", "A", "x-rincon-queue:A#0"),
                                  ("seek_track", "A", 3), ("play", "A")]
    held = engine._parties[pid]
    assert held.own_source is None and held.seen is False and held.state == "playing"
    # the report still on its way from before the switch drops nobody
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [] and engine.list()[0]["speakers"] == ["A", "B"]
    _plays(rig, "A", "x-rincon-queue:A#0")
    run(engine.on_state({"type": "zone"}))
    assert rig.notices == [] and engine._parties[pid].seen is True


def test_a_song_added_off_the_queue_is_found_in_the_queue(rig, monkeypatch):
    _with_queues(rig, monkeypatch, {"A": [{"uri": "x-file:9", "metadata": ""}, {"uri": "x-file:1", "metadata": ""},
                                          {"uri": "x-file:9", "metadata": ""}]})
    engine, pid = _party_on_airplay(rig)
    run(engine.play_added(pid, "x-file:9"))
    assert ("seek_track", "A", 3) in rig.commands.calls     # the copy just added: the last
    run(engine.play_added(pid, "x-file:1", position=2))
    assert ("seek_track", "A", 2) in rig.commands.calls


def test_a_song_added_on_the_queue_only_queues(rig):
    engine, pid = _party_on_airplay(rig)
    _plays(rig, "A", "x-rincon-queue:A#0")
    assert run(engine.play_added(pid, "x-file:9", {"FirstTrackNumberEnqueued": "3"})) is False
    assert rig.commands.calls == []


def test_a_party_seen_before_a_restart_follows_reality(rig):
    import json
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    _plays(rig, "A", "x-rincon-queue:A#0")
    run(engine.on_state({"type": "zone"}))
    assert json.loads(rig.path.read_text())["parties"][0]["seen"] is True
    _plays(rig, "A", "x-sonosapi-stream:s1")          # played while Sonora was down
    again = rig.engine()
    run(again.on_state({"type": "snapshot"}))
    assert rig.notices == [{"type": "party_notice", "party": party["id"], "zone": "A", "event": "left"}]


def test_a_party_never_seen_is_not_dropped_after_a_restart(rig):
    import json
    engine = rig.engine()
    run(engine.start(name="One", speakers=["A"], by=ANA))
    data = json.loads(rig.path.read_text())
    assert data["parties"][0]["seen"] is False
    # an older file has no flag at all: not seen either
    del data["parties"][0]["seen"]
    rig.path.write_text(json.dumps(data))
    _plays(rig, "A", "x-sonosapi-stream:s1")          # its own source was never matched
    again = rig.engine()
    run(again.on_state({"type": "snapshot"}))
    assert rig.notices == [] and again.list()[0]["speakers"] == ["A"]


# -- a party with no speakers keeps its queue ---------------------------------


def _with_queues(rig, monkeypatch, queues):
    for zone in rig.controller.zones.values():
        zone.host = f"h{zone.uuid}"
    put: list[tuple] = []

    async def items(commands, host):
        return [dict(i) for i in queues.get(host[1:], [])]

    async def put_queue(commands, uuid, host, queue):
        put.append((uuid, host, [dict(q) for q in queue]))
    monkeypatch.setattr(scenes, "_queue_items", items)
    monkeypatch.setattr(scenes, "put_queue", put_queue)
    return put


def test_party_left_by_everyone_keeps_its_queue_and_puts_it_back(rig, monkeypatch):
    queue = [{"uri": "x-file:host", "metadata": "<a/>"}, {"uri": "x-file:guest", "metadata": "<b/>"}]
    put = _with_queues(rig, monkeypatch, {"A": queue, "C": [{"uri": "x-file:c-own", "metadata": "<c/>"}]})
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    engine.record_add(party["id"], "x-file:guest", GUEST)
    _plays(rig, "A", "x-rincon-queue:A#0")
    run(engine.on_state({"type": "zone"}))
    _plays(rig, "A", "x-sonosapi-stream:s1")
    run(engine.on_state({"type": "zone"}))
    assert engine.list()[0]["speakers"] == [] and engine.list()[0]["kept_queue"] == 2
    # kept in the file, so a restart keeps it
    import json
    saved = json.loads(rig.path.read_text())["parties"][0]["saved_queue"]
    assert saved["items"] == queue and saved["added"] == {"x-file:guest": [{"id": "g_1", "name": "Guest"}]}
    again = rig.engine()
    rig.commands.calls.clear()
    run(again.add_speaker(party["id"], "C", ANA))
    assert put == [("C", "hC", queue)]
    assert ("set_source", "C", "x-rincon-queue:C#0") in rig.commands.calls
    assert ("play", "C") in rig.commands.calls
    listed = again.list()[0]
    assert listed["speakers"] == ["C"] and listed["state"] == "playing" and listed["kept_queue"] == 0
    assert again.added_by(party["id"], "x-file:guest") == ["g_1"]
    # C's own queue went into its way back before the party's went on it
    run(again.end(party["id"]))
    assert rig.reverted[-1]["rooms"]["C"]["queue"] == [{"uri": "x-file:c-own", "metadata": "<c/>"}]


def test_party_whose_last_speaker_is_taken_keeps_its_queue(rig, monkeypatch):
    queue = [{"uri": "x-file:1", "metadata": "<a/>"}]
    _with_queues(rig, monkeypatch, {"A": queue})
    engine = rig.engine()
    one = run(engine.start(name="One", speakers=["A"], by=ANA))
    run(engine.start(name="Two", speakers=["A"], by=ANA, take=True))
    first = next(p for p in engine.list() if p["id"] == one["id"])
    assert first["speakers"] == [] and first["kept_queue"] == 1
    run(engine.start(name="Three", speakers=["B"], by=ANA))
    run(engine.remove_speaker(engine.list()[-1]["id"], "B"))
    assert engine.list()[-1]["kept_queue"] == 0       # B's queue was empty


# -- the engine reads the speakers again after its own changes ----------------


def test_a_speaker_that_left_during_a_change_is_let_go_after_it(rig, monkeypatch):
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A", "B"], by=ANA))
    fake = scenes.apply

    async def meanwhile(ctrl, cmds, scene, metadata_for=None):
        report = await fake(ctrl, cmds, scene, metadata_for)
        ctrl.lead["B"] = "B"                          # another app took B out just now
        await engine.on_state({"type": "snapshot"})   # heard while the engine is busy: ignored
        return report
    monkeypatch.setattr(scenes, "apply", meanwhile)
    run(engine.add_speaker(party["id"], "C", ANA))
    assert rig.notices == [{"type": "party_notice", "party": party["id"], "zone": "B", "event": "left"}]
    assert engine.list()[0]["speakers"] == ["A", "C"]


# -- the file ------------------------------------------------------------------


def test_corrupt_file_is_set_aside_and_writes_are_private(rig):
    import os
    rig.path.write_text("{not json")
    engine = rig.engine()
    assert engine.list() == []
    aside = list(rig.path.parent.glob("parties.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == "{not json"
    run(engine.start(name="One", speakers=["A"], by=ANA))
    assert os.stat(rig.path).st_mode & 0o777 == 0o600
    assert not list(rig.path.parent.glob("*.tmp"))


def test_kept_queue_is_not_put_on_a_speaker_whose_queue_could_not_be_written_down(rig, monkeypatch):
    queue = [{"uri": "x-file:guest", "metadata": "<b/>"}]
    put = _with_queues(rig, monkeypatch, {"A": queue})
    engine = rig.engine()
    party = run(engine.start(name="One", speakers=["A"], by=ANA))
    run(engine.start(name="Two", speakers=["A"], by=ANA, take=True))
    assert engine.get(party["id"])["kept_queue"] == 1

    async def broken(commands, host):
        raise OSError("speaker did not answer")
    monkeypatch.setattr(scenes, "_queue_items", broken)
    rig.commands.calls.clear()
    run(engine.add_speaker(party["id"], "C", ANA))
    assert put == [] and not any(c[0] in ("set_source", "play") for c in rig.commands.calls)
    listed = engine.get(party["id"])
    assert listed["speakers"] == ["C"] and listed["kept_queue"] == 1   # kept for later
