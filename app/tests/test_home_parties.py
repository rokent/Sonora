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
