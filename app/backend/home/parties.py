"""Parties: a named group of speakers someone has started, and the way back.

A party is one Sonos group started by a person (a user or a guest) under a
name. Each speaker belongs to at most one party at a time; starting a party
on a speaker another party holds needs the starter to say so (``take``), and
the speaker then moves, leaving the old party one speaker shorter.

Every time a party takes a speaker, what that speaker was just before is
pushed onto the speaker's own snapshot stack. Ending a party walks each of
its speakers down that stack: a speaker that was taken from a party still
going goes back to it (it rejoins that party's lead); otherwise it is put
back as it was before any party, with the scene machinery's ``revert``.
The stacks are kept per speaker, not per party, because a speaker can pass
through several parties and each must land in the right place whatever
order they end in.

Sonora never fights the household: when another app moves a party speaker
out of the party's group, the party lets it go and says so (a
``party_notice``); it never regroups it. The same when something else is
played on the party's speakers: once the party's lead has been seen playing
what the party put on (its own queue, the starting music the party chose,
or live sound from Sonora), a lead that turns to any other source takes the
whole party's speakers out of it.

A party left with no speakers pauses and keeps its queue: the lead's queue
and who added what are written into the party, and put back on the next
speaker added to it.

A Sonos queue has no stable item ids, so who added what is kept by track
URI, a first-in first-out list of adders per URI, for showing names and for
letting a guest remove only what they added.

Parties, stacks and attribution live in one JSON file, rewritten after every
change, so a restart of Sonora keeps every way back. After every change, and
once when the engine is made, the engine reads the speakers once more, so a
speaker that left while it was not listening is let go.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import secrets
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol

from ..sonos import scenes
from .store import load_json, write_json

log = logging.getLogger(__name__)

#: Controller messages after which group membership or what a group plays
#: may have changed.
_TOPOLOGY_MESSAGES = ("snapshot", "zone")

#: Live sound from Sonora (``home/live.py``) reaches a party's speakers as a
#: stream URL under this path; the party put it there itself.
_LIVE_PATH = "/api/home/live/"


class Actor(Protocol):
    """Whoever acts on a party: anything with an id, a name and whether it
    is a guest (``home.auth.Principal`` fits)."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def is_guest(self) -> bool: ...

    @property
    def is_user(self) -> bool: ...


class SpeakerTaken(Exception):
    """A speaker asked for belongs to another party, and taking it was not
    confirmed."""

    def __init__(self, zone: str, party_id: str, party_name: str) -> None:
        super().__init__(f"{zone} belongs to party {party_name!r}")
        self.zone = zone
        self.party_id = party_id
        self.party_name = party_name


class NoSpeakers(Exception):
    """None of the speakers asked for can be reached, or what they were could
    not be written down: a party there would have no way back."""


class MixedHousehold(Exception):
    """The speakers asked for are on more than one Sonos system: one group
    cannot span two."""


@dataclass
class Party:
    id: str
    name: str
    owner: str
    owner_name: str
    household: str
    speakers: list[str] = field(default_factory=list)
    volumes: dict[str, int] = field(default_factory=dict)
    state: Literal["playing", "paused"] = "playing"
    preset_id: str | None = None
    created: float = 0.0
    #: The source the party itself put on its lead when that is not the
    #: lead's queue: a television, a line-in, a station from the starting
    #: music, or (for a party that started quiet) what the lead had loaded.
    own_source: str | None = None
    #: The queue and attribution kept while the party has no speakers:
    #: ``{"items": [{uri, metadata}], "added": {uri: [adders]}}``.
    saved_queue: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Party":
        return cls(id=str(raw["id"]), name=str(raw.get("name") or ""), owner=str(raw.get("owner") or ""),
                   owner_name=str(raw.get("owner_name") or ""), household=str(raw.get("household") or ""),
                   speakers=[str(s) for s in raw.get("speakers") or []],
                   volumes={str(k): int(v) for k, v in (raw.get("volumes") or {}).items()},
                   state="paused" if raw.get("state") == "paused" else "playing",
                   preset_id=raw.get("preset_id"), created=float(raw.get("created") or 0.0),
                   own_source=str(raw["own_source"]) if raw.get("own_source") else None,
                   saved_queue=raw["saved_queue"] if isinstance(raw.get("saved_queue"), dict) else None)


def _public(party: Party) -> dict:
    """A party as it is shown: the kept queue is counted, not listed."""
    out = asdict(party)
    kept = out.pop("saved_queue", None)
    out["kept_queue"] = len((kept or {}).get("items") or [])
    return out


def _split(before: dict | None, speakers: list[str]) -> dict[str, dict | None]:
    """One ``before`` record per speaker, out of ``scenes.apply``'s record
    for them all. A record is ``{name, at, rooms: {uuid: room}}`` and each
    room stands on its own, so each speaker gets its own room. A room the
    scene moved that is not a party speaker (one that followed a speaker
    and was sent off on its own) goes with the speaker it followed, so it
    is put back with it; failing that, with the first speaker."""
    if not before:
        return {s: None for s in speakers}
    out: dict[str, dict | None] = {}
    rooms = before.get("rooms") or {}
    for speaker in speakers:
        if speaker in rooms:
            out[speaker] = {"name": before.get("name", ""), "at": before.get("at"), "rooms": {speaker: rooms[speaker]}}
    for uuid, room in rooms.items():
        if uuid in speakers:
            continue
        home = room.get("coordinator") if room.get("coordinator") in out else next(iter(out), None)
        if home is not None and out[home] is not None:
            out[home]["rooms"][uuid] = room
    return out


class PartyEngine:
    """The parties now on, the speakers' snapshot stacks, and who added which
    track, in one JSON file."""

    def __init__(self, path: Path, controller, commands,
                 metadata_fn: Callable[[str, str, str], Awaitable[str]] | None,
                 notify: Callable[[dict], Awaitable[None]]) -> None:
        self._path = path
        self._controller = controller
        self._commands = commands
        self._metadata_fn = metadata_fn
        self._notify = notify
        self._parties: dict[str, Party] = {}
        #: ``{zone: [{"party": id, "before": record | None}, ...]}``, oldest
        #: first; the first entry's record is the speaker before any party.
        self._stacks: dict[str, list[dict]] = {}
        #: ``{party: {uri: [{"id", "name"}, ...]}}``, oldest adder first.
        self._added: dict[str, dict[str, list[dict]]] = {}
        #: Called with a party's id as it ends; see ``on_end`` for when.
        self._end_listeners: list[Callable[[str], Awaitable[None] | None]] = []
        #: Held while a party changes, so changes run one at a time and the
        #: topology events Sonora's own regrouping brings are not read as
        #: another app's doing.
        self._lock = asyncio.Lock()
        #: Parties whose lead has been seen playing what the party put on.
        #: Only those are let go when it turns to something else, so a
        #: report still on its way from before the party's own change is not
        #: read as someone else's. Parties read back from the file are taken
        #: as seen: nothing of the engine's is on its way then.
        self._seen: set[str] = set()
        self._load()
        self._seen = set(self._parties)

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        data = load_json(self._path)
        if not isinstance(data, dict):
            return
        for raw in data.get("parties") or []:
            try:
                party = Party.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            self._parties[party.id] = party
        stacks = data.get("stacks")
        if isinstance(stacks, dict):
            self._stacks = {str(z): [e for e in s if isinstance(e, dict) and "party" in e]
                            for z, s in stacks.items() if isinstance(s, list)}
            self._stacks = {z: s for z, s in self._stacks.items() if s}
        added = data.get("added")
        if isinstance(added, dict):
            self._added = {p: {u: list(who) for u, who in uris.items() if isinstance(who, list)}
                           for p, uris in added.items() if isinstance(uris, dict) and p in self._parties}

    def _save(self) -> None:
        write_json(self._path, {"parties": [asdict(p) for p in self._parties.values()],
                                 "stacks": self._stacks, "added": self._added})

    # -- reading ---------------------------------------------------------------

    def list(self) -> list[dict]:
        """Every party, oldest first, each with ``queue_owners``: who added
        each track URI still attributed, oldest adder first."""
        return [{**_public(p), "queue_owners": {u: [dict(w) for w in who]
                                               for u, who in self._added.get(p.id, {}).items() if who}}
                for p in sorted(self._parties.values(), key=lambda p: p.created)]

    def get(self, party_id: str) -> dict | None:
        return next((p for p in self.list() if p["id"] == party_id), None)

    def owner_of(self, zone: str) -> str | None:
        """The id of the party holding ``zone``, if any."""
        return next((p.id for p in self._parties.values() if zone in p.speakers), None)

    def _running(self, party_id: str) -> Party | None:
        party = self._parties.get(party_id)
        return party if party is not None and party.speakers else None

    def _party(self, party_id: str) -> Party:
        party = self._parties.get(party_id)
        if party is None:
            raise KeyError(party_id)
        return party

    # -- checks ----------------------------------------------------------------

    def _household(self, speakers: list[str]) -> str:
        homes = set()
        for zone in speakers:
            home = self._controller.household_of(zone)
            if home is None:
                raise KeyError(zone)
            homes.add(home.id)
        if len(homes) > 1:
            raise MixedHousehold(sorted(homes))
        return homes.pop() if homes else ""

    def _check_taken(self, speakers: list[str], take: bool, mine: str | None = None) -> list[tuple[str, Party]]:
        taken = []
        for zone in speakers:
            holder = self.owner_of(zone)
            if holder is None or holder == mine:
                continue
            party = self._parties[holder]
            if not take:
                raise SpeakerTaken(zone, party.id, party.name)
            taken.append((zone, party))
        return taken

    # -- the stacks ------------------------------------------------------------

    def _push(self, zone: str, party_id: str, before: dict | None) -> None:
        self._stacks.setdefault(zone, []).append({"party": party_id, "before": before})

    def _drop_entries(self, zone: str, party_id: str) -> None:
        """Forget ``party_id``'s place in ``zone``'s stack. The bottom entry's
        record (the speaker before any party) is handed to the entry above
        it, so the way all the way back is never lost."""
        stack = self._stacks.get(zone)
        if not stack:
            return
        base = stack[0]["before"]
        kept = [e for e in stack if e["party"] != party_id]
        if kept and stack[0]["party"] == party_id:
            kept[0] = {**kept[0], "before": base}
        if kept:
            self._stacks[zone] = kept
        else:
            del self._stacks[zone]

    def _leave_plan(self, zone: str, party_id: str) -> tuple[str, Any]:
        """Where ``zone`` goes when it leaves ``party_id``: ``("join", party)``
        for a party still going lower in its stack, else ``("revert",
        record)`` with its record from before any party (None if it has
        none). The stack is cut to match."""
        stack = self._stacks.get(zone) or []
        top = max((i for i, e in enumerate(stack) if e["party"] == party_id), default=len(stack))
        for j in range(top - 1, -1, -1):
            under = self._running(stack[j]["party"])
            if under is not None and under.id != party_id:
                self._stacks[zone] = stack[:j + 1]
                return "join", under
        base = stack[0]["before"] if stack else None
        self._stacks.pop(zone, None)
        return "revert", base

    # -- regrouping ------------------------------------------------------------

    async def _settle(self, expected: dict[str, str]) -> None:
        """Wait, as the scene machinery does, until the speakers report the
        regrouping just sent, so the lock is not let go while its topology
        events are still on their way: one arriving late would otherwise read
        as another app moving a speaker the engine has just placed."""
        if expected:
            await scenes._settle_groups(self._controller, expected)

    def _online(self, zone: str) -> bool:
        try:
            return bool(self._controller.zone(zone).online)
        except KeyError:
            return False

    async def _keep_queue(self, record: dict | None, lead: str) -> None:
        """Write the party lead's own queue into its record when the scene
        did not (``scenes.apply`` reads a queue only when it replaces it).
        Guests fill the lead's queue during the party; ending it must hand
        back the queue that was there, and ``revert`` restores a queue held
        in the record even when the source is still that queue."""
        room = ((record or {}).get("rooms") or {}).get(lead)
        if room is None or room.get("queue") is not None:
            return
        try:
            room["queue"] = await scenes._queue_items(self._commands, self._controller.zone(lead).host)
        except Exception as exc:                      # noqa: BLE001 - the party starts all the same
            log.info("party: the queue of %s could not be read: %s", lead, exc)

    def _host(self, zone: str) -> str | None:
        try:
            return self._controller.zone(zone).host
        except (KeyError, AttributeError):
            return None

    async def _read_queue(self, zone: str) -> list[dict] | None:
        host = self._host(zone)
        if host is None:
            return None
        try:
            return await scenes._queue_items(self._commands, host)
        except Exception as exc:                      # noqa: BLE001 - the party pauses all the same
            log.info("party: the queue of %s could not be read: %s", zone, exc)
            return None

    def _keep(self, party: Party, items: list[dict] | None) -> None:
        """Write the queue a party with no speakers keeps, and who added
        what, into the party."""
        if items is None:
            return
        party.saved_queue = {"items": items, "added": copy.deepcopy(self._added.get(party.id, {}))}

    async def _put_back(self, party: Party, zone: str, record: dict | None) -> None:
        """``zone`` now leads a party that had no speakers: put the queue the
        party kept on it, so the guests' songs come back. With no queue
        kept, the party takes what ``zone`` has loaded as its own."""
        kept = party.saved_queue or {}
        items = kept.get("items") or []
        party.saved_queue = None
        self._seen.discard(party.id)
        if not items:
            room = ((record or {}).get("rooms") or {}).get(zone) or {}
            party.own_source = (room.get("transport") or {}).get("media_uri") or None
            return
        party.own_source = None
        added = kept.get("added")
        if isinstance(added, dict):
            self._added[party.id] = {u: list(w) for u, w in added.items() if isinstance(w, list) and w}
        host = self._host(zone)
        try:
            if host is None:
                raise KeyError(zone)
            await scenes.put_queue(self._commands, zone, host, items)
            await self._commands.set_source(zone, f"x-rincon-queue:{zone}#0", "")
            await self._commands.play(zone)
        except Exception as exc:                      # noqa: BLE001 - the speaker joins all the same
            log.info("party %r: its queue could not be put back on %s: %s", party.name, zone, exc)

    def _own_source(self, music: dict | None, lead: str, record: dict | None) -> str | None:
        """The source the party puts on ``lead`` when it is not the queue."""
        kind = (music or {}).get("kind")
        if kind == "tv":
            return f"x-sonos-htastream:{lead}:spdif"
        if kind == "line_in":
            return f"x-rincon-stream:{music.get('source') or lead}"
        if kind == "item":
            uri = music.get("uri") or ""
            queueable = getattr(self._commands, "_queueable", None)
            if music.get("item_kind") == "audiobook" or (queueable is not None and not queueable(uri)):
                return uri or None
            return None
        # Quiet: the lead keeps what it had loaded.
        room = ((record or {}).get("rooms") or {}).get(lead) or {}
        return (room.get("transport") or {}).get("media_uri") or None

    async def _hand_over(self, party: Party, zone: str) -> None:
        """Before ``zone`` leaves ``party``, give the group's lead to the next
        speaker if ``zone`` leads it, so the rest carry on together instead
        of being left following a speaker no longer theirs."""
        if party.speakers and party.speakers[0] == zone and len(party.speakers) > 1:
            heir = party.speakers[1]
            try:
                await self._commands.delegate(zone, heir)
                await self._settle({**{u: heir for u in party.speakers if u != zone}, zone: zone})
            except Exception as exc:                  # noqa: BLE001 - the speaker moves all the same
                log.info("party %r: handing the lead to %s failed: %s", party.name, heir, exc)
        if party.speakers == [zone]:
            self._keep(party, await self._read_queue(zone))
        if zone in party.speakers:
            party.speakers.remove(zone)
            party.volumes.pop(zone, None)
        if not party.speakers:
            party.state = "paused"
            self._seen.discard(party.id)

    async def _release(self, plans: dict[str, tuple[str, Any]]) -> dict:
        """Carry out where each leaving speaker goes: rejoin its party's lead,
        or one ``revert`` for every speaker going back to before any party."""
        report: dict = {"rejoined": [], "reverted": None}
        records = [r for kind, r in plans.values() if kind == "revert" and r]
        expected: dict[str, str] = {}
        for zone, (kind, target) in plans.items():
            if kind != "join":
                continue
            try:
                await self._commands.join(zone, target.speakers[0])
                expected[zone] = target.speakers[0]
            except Exception as exc:                  # noqa: BLE001 - it is counted in the party all the same
                log.info("party %r: %s could not rejoin: %s", target.name, zone, exc)
            target.speakers.append(zone)
            report["rejoined"].append({"zone": zone, "party": target.id})
        await self._settle(expected)
        if records:
            merged = {"name": records[0].get("name", ""),
                      "at": min((r.get("at") or time.time()) for r in records), "rooms": {}}
            for record in records:
                for uuid, room in (record.get("rooms") or {}).items():
                    merged["rooms"].setdefault(uuid, room)
            report["reverted"] = await scenes.revert(self._controller, self._commands, merged)
        return report

    # -- parties ---------------------------------------------------------------

    async def _catch_up(self) -> None:
        """Read the speakers once more, as after a topology change: a speaker
        that left while the engine held its lock (or before it was made) is
        let go now rather than at the next change someone else makes."""
        try:
            await self.on_state({"type": "snapshot"})
        except Exception:                             # noqa: BLE001 - never fails the change itself
            log.exception("parties: the catch-up read failed")

    async def start(self, **kwargs) -> dict:
        try:
            return await self._start(**kwargs)
        finally:
            await self._catch_up()

    async def end(self, party_id: str) -> dict:
        try:
            return await self._end(party_id)
        finally:
            await self._catch_up()

    async def add_speaker(self, party_id: str, zone: str, by: Actor, take: bool = False) -> dict:
        try:
            return await self._add_speaker(party_id, zone, by, take)
        finally:
            await self._catch_up()

    async def remove_speaker(self, party_id: str, zone: str) -> dict:
        try:
            return await self._remove_speaker(party_id, zone)
        finally:
            await self._catch_up()

    async def _start(self, *, name: str, speakers: list[str], by: Actor,
                     volumes: dict[str, int] | None = None, music: dict | None = None,
                     preset_id: str | None = None, take: bool = False) -> dict:
        """Start a party on ``speakers`` (the first leads) and return it.

        Raises ``SpeakerTaken`` for a speaker another party holds unless
        ``take``, ``MixedHousehold`` for speakers on two systems, and
        ``NoSpeakers`` when none of them can be reached or what they were
        could not be written down. A speaker that is not on the network is
        left out."""
        async with self._lock:
            speakers = list(dict.fromkeys(speakers))
            if not speakers:
                raise ValueError("at least one speaker is needed")
            household = self._household(speakers)
            speakers = [z for z in speakers if self._online(z)]
            if not speakers:
                raise NoSpeakers("none of the speakers can be reached")
            taken = self._check_taken(speakers, take)
            if music and music.get("kind") == "tv" and music.get("source") in speakers:
                # The television's soundbar leads its group.
                speakers.remove(music["source"])
                speakers.insert(0, music["source"])
            volumes = {z: int(v) for z, v in (volumes or {}).items() if z in speakers}
            # Built (and so checked) before any other party is touched.
            scene = scenes.normalize({"name": name, "groups": [{
                "household": household,
                "rooms": [{"uuid": z, "volume": volumes.get(z)} for z in speakers],
                "play": music or {"kind": "silence"}}]}, household)
            for zone, old in taken:
                await self._hand_over(old, zone)
            report = await scenes.apply(self._controller, self._commands, scene, self._metadata_fn)
            records = _split(report.get("before"), speakers)
            # A speaker the scene could not reach or write down is not the party's.
            held = [z for z in speakers if records.get(z)]
            if not held:
                for zone, _ in taken:
                    self._stacks.pop(zone, None)
                self._save()
                raise NoSpeakers("what the speakers were could not be written down")
            await self._keep_queue(records[held[0]], held[0])
            party = Party(id="pt_" + secrets.token_hex(6), name=scene["name"], owner=by.id, owner_name=by.name,
                          household=household, speakers=held, volumes={z: v for z, v in volumes.items() if z in held},
                          state="playing", preset_id=preset_id, created=time.time(),
                          own_source=self._own_source(scene["groups"][0]["play"], held[0], records[held[0]]))
            self._parties[party.id] = party
            for zone in held:
                self._push(zone, party.id, records[zone])
            for zone, _ in taken:
                if zone not in held:
                    self._stacks.pop(zone, None)
            self._save()
        return {**_public(party), "report": {k: report.get(k) for k in ("set", "missing", "failed")}}

    def on_end(self, callback: Callable[[str], Awaitable[None] | None]) -> None:
        """Call ``callback(party_id)`` whenever a party ends. Any number of
        listeners; each may be sync or async.

        Order inside ``end``: the party is first forgotten (removed from the
        engine and saved, so e.g. new file uploads for it are refused), then
        every listener runs, then the speakers are released. So listeners run
        after the party is gone but before its speakers are let go (live sound
        stops before the speakers rejoin their old groups). They run while the
        engine holds its lock, so they must not call back into the engine's
        locked methods. A failing listener is logged and does not stop the
        others or the end itself."""
        self._end_listeners.append(callback)

    async def _fire_end(self, party_id: str) -> None:
        for callback in list(self._end_listeners):
            try:
                result = callback(party_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("parties: an on_end listener failed for %s", party_id)

    async def _end(self, party_id: str) -> dict:
        """End the party: each speaker rejoins the party it was taken from if
        that one is still going, else goes back to how it was before any
        party. The party and its attribution are forgotten."""
        async with self._lock:
            party = self._party(party_id)
            plans = {zone: self._leave_plan(zone, party_id) for zone in party.speakers}
            for zone in list(self._stacks):
                self._drop_entries(zone, party_id)
            del self._parties[party_id]
            self._added.pop(party_id, None)
            self._seen.discard(party_id)
            self._save()
            await self._fire_end(party_id)
            report = await self._release(plans)
            self._save()
        return {"id": party_id, "name": party.name, **report}

    async def _add_speaker(self, party_id: str, zone: str, by: Actor, take: bool = False) -> dict:
        """Add ``zone`` to the party: its state is written down, it steps out
        of whatever group it is in, then joins the party's lead (or leads
        the party, when the party has no speakers left, and gets the queue
        the party kept). Raises
        ``NoSpeakers`` for a speaker that cannot be reached."""
        async with self._lock:
            party = self._party(party_id)
            if zone in party.speakers:
                return self.get(party_id)
            if self._household([zone]) != party.household:
                raise MixedHousehold([party.household, self._household([zone])])
            if not self._online(zone):
                raise NoSpeakers(f"{zone} cannot be reached")
            taken = self._check_taken([zone], take, mine=party_id)
            volume = party.volumes.get(zone)
            scene = scenes.normalize({"name": party.name, "groups": [{
                "household": party.household, "rooms": [{"uuid": zone, "volume": volume}], "play": None}]},
                party.household)
            for z, old in taken:
                await self._hand_over(old, z)
            report = await scenes.apply(self._controller, self._commands, scene, self._metadata_fn)
            record = _split(report.get("before"), [zone]).get(zone)
            if record is None:
                self._stacks.pop(zone, None)
                self._save()
                raise NoSpeakers(f"what {zone} was could not be written down")
            if party.speakers:
                try:
                    await self._commands.join(zone, party.speakers[0])
                    await self._settle({zone: party.speakers[0]})
                except Exception as exc:              # noqa: BLE001 - reported by the topology that follows
                    log.info("party %r: %s could not join: %s", party.name, zone, exc)
            else:
                await self._keep_queue(record, zone)
                await self._put_back(party, zone, record)
            party.speakers.append(zone)
            party.state = "playing"
            self._push(zone, party_id, record)
            self._save()
            return self.get(party_id)

    async def _remove_speaker(self, party_id: str, zone: str) -> dict:
        """Take ``zone`` out of the party, back to the party it came from if
        that one is still going, else to before any party."""
        async with self._lock:
            party = self._party(party_id)
            if zone not in party.speakers:
                return self.get(party_id)
            await self._hand_over(party, zone)
            plans = {zone: self._leave_plan(zone, party_id)}
            self._save()
            await self._release(plans)
            self._save()
            return self.get(party_id)


    # -- who added what ------------------------------------------------------

    def record_add(self, party_id: str, uri: str, by: Actor) -> None:
        if party_id not in self._parties:
            return
        self._added.setdefault(party_id, {}).setdefault(uri, []).append({"id": by.id, "name": by.name})
        self._save()

    def record_remove(self, party_id: str, uri: str, nth: int = 0) -> None:
        """Forget the adder of the ``nth`` copy of ``uri`` (the oldest by
        default): that copy left the queue."""
        who = self._added.get(party_id, {}).get(uri)
        if not who or not 0 <= nth < len(who):
            return
        who.pop(nth)
        if not who:
            del self._added[party_id][uri]
        self._save()

    def added_by(self, party_id: str, uri: str) -> list[str]:
        return [w["id"] for w in self._added.get(party_id, {}).get(uri, [])]

    def can_remove(self, party_id: str, uri: str, principal: Actor) -> bool:
        """A user may remove anything; anyone else (a guest) only a track
        whose oldest attribution is theirs. Fail closed: a display or any
        future kind is never treated as a user."""
        if principal.is_user:
            return True
        who = self._added.get(party_id, {}).get(uri)
        return bool(who) and who[0]["id"] == principal.id

    # -- outside changes -----------------------------------------------------

    def _lead_of(self, zone: str) -> str | None:
        try:
            return self._controller.coordinator_of(zone).uuid
        except KeyError:
            return None

    def _media_of(self, zone: str) -> str | None:
        """What ``zone``'s group has loaded, None when it cannot be told."""
        try:
            transport = getattr(self._controller.coordinator_of(zone), "transport", None)
        except KeyError:
            return None
        uri = getattr(transport, "media_uri", None)
        return uri if isinstance(uri, str) else None

    def _plays_its_own(self, party: Party, uri: str) -> bool:
        """Whether the party's lead plays something the party put there."""
        if not uri or uri.startswith("x-rincon:"):
            # Nothing loaded yet, or following another room (the topology
            # check answers for that).
            return True
        if uri == f"x-rincon-queue:{party.speakers[0]}#0":
            return True
        if party.own_source and uri == party.own_source:
            return True
        return _LIVE_PATH in uri

    async def on_state(self, message: dict) -> None:
        """After a topology change, let go of every party speaker that is no
        longer in the party's group, and say so. Never regroups.

        The party's group is the one holding most of its speakers (on a tie,
        the one its lead is in), so a lead moved away by another app is the
        one let go, and the party's lead becomes that group's. Nothing is
        read while the engine itself is regrouping.

        Then, last action wins: a party whose lead has been seen playing what
        the party put on and now plays something else lets all its speakers
        go, says so for each, pauses and keeps its queue."""
        if message.get("type") not in _TOPOLOGY_MESSAGES or self._lock.locked():
            return
        notices = []
        changed = False
        for party in list(self._parties.values()):
            leads = {z: self._lead_of(z) for z in party.speakers}
            known = {z: lead for z, lead in leads.items() if lead is not None}
            if not known:
                continue
            counts = Counter(known.values())
            best = max(counts.values())
            current = known.get(party.speakers[0])
            group = current if current is not None and counts[current] == best \
                else next(lead for lead, n in counts.items() if n == best)
            for zone, lead in known.items():
                if lead == group:
                    continue
                party.speakers.remove(zone)
                party.volumes.pop(zone, None)
                # Moved by hand, it is in no party now: no way back is owed.
                self._stacks.pop(zone, None)
                notices.append({"type": "party_notice", "party": party.id, "zone": zone, "event": "left"})
            head = group if group in party.speakers else next((z for z in party.speakers if known.get(z) == group), None)
            if head is not None and party.speakers and party.speakers[0] != head:
                party.speakers.remove(head)
                party.speakers.insert(0, head)
                changed = True
            if not party.speakers:
                party.state = "paused"
        keeping: list[tuple[Party, str]] = []
        for party in list(self._parties.values()):
            if not party.speakers:
                continue
            uri = self._media_of(party.speakers[0])
            if uri is None:
                continue
            if self._plays_its_own(party, uri):
                self._seen.add(party.id)
                continue
            if party.id not in self._seen:
                continue
            # Something else was put on the party's speakers: they leave it.
            lead = party.speakers[0]
            for zone in party.speakers:
                self._stacks.pop(zone, None)
                notices.append({"type": "party_notice", "party": party.id, "zone": zone, "event": "left"})
            party.speakers = []
            party.volumes = {}
            party.state = "paused"
            self._seen.discard(party.id)
            keeping.append((party, lead))
            changed = True
        if notices or changed:
            self._save()
        if keeping:
            for party, lead in keeping:
                items = await self._read_queue(lead)
                # Unless a speaker was added to it meanwhile.
                if not party.speakers and party.id in self._parties:
                    self._keep(party, items)
            self._save()
        for notice in notices:
            await self._notify(notice)
