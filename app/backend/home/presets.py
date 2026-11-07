"""Party presets: a saved set of speakers, their levels, and optional music
and television, kept in one JSON file. A preset's speakers all sit on one
household (system); the household lookup is passed in by the caller."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.sonos import scenes

NAME_LIMIT = 60
TV_SHOWS = ("video", "visualizer", "cover")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _normalize(body: dict, household_of: Callable[[str], str | None]) -> dict:
    if not isinstance(body, dict):
        raise ValueError("not a preset")
    name = str(body.get("name") or "").strip()[:NAME_LIMIT]
    if not name:
        raise ValueError("a name is needed")
    raw = body.get("speakers")
    speakers: list[str] = []
    for uuid in raw if isinstance(raw, list) else []:
        uuid = str(uuid)
        if uuid and uuid not in speakers:
            speakers.append(uuid)
    if not speakers:
        raise ValueError("at least one speaker is needed")
    households = set()
    for uuid in speakers:
        household = household_of(uuid)
        if household is None:
            raise ValueError(f"unknown speaker {uuid}")
        households.add(household)
    if len(households) > 1:
        raise ValueError("speakers must be on one system")
    raw_volumes = body.get("volumes") or {}
    if not isinstance(raw_volumes, dict):
        raise ValueError("volumes must be an object")
    volumes: dict[str, int] = {}
    for uuid, level in raw_volumes.items():
        try:
            level = int(level)
        except (TypeError, ValueError):
            raise ValueError("volume must be a number") from None
        if isinstance(level, bool) or not 0 <= level <= 100:
            raise ValueError("volume must be 0 to 100")
        if uuid in speakers:
            volumes[uuid] = level
    music = None
    if body.get("music") is not None:
        music = scenes._play(body["music"])
        if music is None or music["kind"] != "item":
            raise ValueError("music is not a playable item")
    tv = None
    raw_tv = body.get("tv")
    if raw_tv is not None:
        if (not isinstance(raw_tv, dict) or not str(raw_tv.get("tv_id") or "")
                or raw_tv.get("show") not in TV_SHOWS):
            raise ValueError("tv needs a tv_id and a show of video, visualizer or cover")
        tv = {"tv_id": str(raw_tv["tv_id"]), "show": raw_tv["show"]}
    return {"name": name, "speakers": speakers, "volumes": volumes, "music": music, "tv": tv}


class Presets:
    """The saved party presets, in one JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: list[dict] = []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("presets"), list):
            self._items = [p for p in data["presets"] if isinstance(p, dict) and p.get("id")]

    def list(self) -> list[dict]:
        return [dict(p) for p in self._items]

    def get(self, preset_id: str) -> dict | None:
        for preset in self._items:
            if preset["id"] == preset_id:
                return dict(preset)
        return None

    def add(self, body: dict, household_of: Callable[[str], str | None]) -> dict:
        preset = _normalize(body, household_of)
        preset["id"] = "pp_" + secrets.token_hex(6)
        preset["created"] = preset["updated"] = _now()
        self._items.append(preset)
        self._save()
        return dict(preset)

    def update(self, preset_id: str, body: dict,
               household_of: Callable[[str], str | None]) -> dict | None:
        for i, old in enumerate(self._items):
            if old["id"] == preset_id:
                preset = _normalize(body, household_of)
                preset["id"] = preset_id
                preset["created"] = old["created"]
                preset["updated"] = _now()
                self._items[i] = preset
                self._save()
                return dict(preset)
        return None

    def remove(self, preset_id: str) -> dict | None:
        for i, preset in enumerate(self._items):
            if preset["id"] == preset_id:
                del self._items[i]
                self._save()
                return dict(preset)
        return None

    def _save(self) -> None:
        _write_json(self._path, {"presets": self._items})
