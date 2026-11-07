"""Party presets store."""

import pytest

from backend.home.presets import Presets

HH = {"A": "hh1", "B": "hh1", "C": "hh2"}
household_of = HH.get
ITEM = {"kind": "item", "uri": "x-sonosapi-radio:1", "metadata": "<m/>", "title": "Radio"}


def _body(**extra):
    return {"name": "Party", "speakers": ["A", "B"], "volumes": {"A": 30, "B": 40}, **extra}


def test_add_and_list(tmp_path):
    store = Presets(tmp_path / "p.json")
    p = store.add(_body(music=ITEM, tv={"tv_id": "tv1", "show": "video"}), household_of)
    assert p["id"] and p["created"] and p["tv"] == {"tv_id": "tv1", "show": "video"}
    assert p["music"]["uri"] == "x-sonosapi-radio:1" and p["music"]["kind"] == "item"
    assert store.list() == [p] and store.get(p["id"]) == p
    assert store.add(_body(), household_of)["music"] is None


def test_mixed_household_rejected(tmp_path):
    store = Presets(tmp_path / "p.json")
    with pytest.raises(ValueError):
        store.add(_body(speakers=["A", "C"]), household_of)
    assert store.list() == []


def test_unknown_speaker_rejected(tmp_path):
    with pytest.raises(ValueError):
        Presets(tmp_path / "p.json").add(_body(speakers=["A", "Z"]), household_of)


@pytest.mark.parametrize("bad", [
    {"name": "  "}, {"speakers": []}, {"volumes": {"A": 101}}, {"volumes": {"A": -1}},
    {"music": {"kind": "item", "uri": ""}}, {"tv": {"tv_id": "t", "show": "nope"}}])
def test_invalid_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        Presets(tmp_path / "p.json").add(_body(**bad), household_of)


def test_update_and_remove(tmp_path):
    store = Presets(tmp_path / "p.json")
    p = store.add(_body(), household_of)
    u = store.update(p["id"], _body(name="Renamed", speakers=["B"]), household_of)
    assert u["name"] == "Renamed" and u["created"] == p["created"] and u["speakers"] == ["B"]
    assert u["volumes"] == {"B": 40}
    with pytest.raises(ValueError):
        store.update(p["id"], _body(speakers=["A", "C"]), household_of)
    assert store.update("nope", _body(), household_of) is None
    assert store.remove(p["id"])["id"] == p["id"]
    assert store.remove(p["id"]) is None and store.list() == []


def test_persisted(tmp_path):
    path = tmp_path / "sub" / "p.json"
    p = Presets(path).add(_body(music=ITEM), household_of)
    assert Presets(path).list() == [p]


def test_corrupt_file_is_set_aside_and_logged(tmp_path, caplog):
    import logging
    import os
    path = tmp_path / "presets.json"
    path.write_text("[[[")
    with caplog.at_level(logging.WARNING):
        store = Presets(path)
    assert store.list() == []
    aside = list(tmp_path.glob("presets.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_text() == "[[["
    assert any("not valid JSON" in r.getMessage() for r in caplog.records)
    store.add(_body(), household_of)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert Presets(path).list() == store.list()


def test_missing_file_is_not_set_aside(tmp_path):
    assert Presets(tmp_path / "presets.json").list() == []
    assert list(tmp_path.iterdir()) == []
