"""File drop: accepted types, size and type refusals, Range serving, deletion
when a party ends and at startup. Tiny synthetic files with real magic bytes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import main
from backend.home import files, routes
from backend.home.store import home_dir

from test_home_routes import GUEST, USER, _start, client, rig  # noqa: F401

SAMPLES = {
    "song.mp3": b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40,
    "frame.mp3": b"\xff\xfb\x90\x00" + b"\x00" * 40,
    "song.m4a": b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 40,
    "song.flac": b"fLaC" + b"\x00" * 40,
    "song.wav": b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40,
    "song.ogg": b"OggS" + b"\x00" * 40,
    "song.aac": b"\xff\xf1\x50\x80" + b"\x00" * 40,
}


@pytest.fixture
def fr(rig):
    rig.ctl.events = SimpleNamespace(_callback_ip="10.0.0.5")
    added = []
    cmds = main.state["commands"]
    orig = cmds.add_to_queue

    async def spy(zone, uri, metadata="", **kw):
        added.append((uri, metadata, kw))
        return await orig(zone, uri, metadata, **kw)
    cmds.add_to_queue = spy
    rig.added = added
    return rig


async def _party():
    return (await _start()).json()["id"]


def _post(who, pid, name, data, **kw):
    return client(who).request("POST", f"/api/parties/{pid}/files?name={name}", raw=data, **kw)


@pytest.mark.parametrize("name", list(SAMPLES))
async def test_each_type_is_accepted_queued_and_served(fr, name):
    pid = await _party()
    r = await _post(GUEST, pid, name, SAMPLES[name])
    assert r.status == 200, r.body
    ext = name.rsplit(".", 1)[1]
    item = r.json()["item"]
    assert item["title"] == name.rsplit(".", 1)[0] and item["size"] == len(SAMPLES[name])
    uri, metadata, _ = fr.added[-1]
    assert uri.startswith("http://10.0.0.5:") and f".{ext}" in uri
    assert f"{files.TYPES[ext]}" in metadata and "<dc:title>" in metadata
    path = uri[uri.index("/api/home/files/"):]
    anon = client(GUEST)
    anon.cookies.clear()
    got = await anon.request("GET", path)
    assert got.status == 200 and got.body == SAMPLES[name]
    assert got.headers["content-type"].startswith(files.TYPES[ext])
    assert got.headers["accept-ranges"] == "bytes"
    # attributed to the adder
    assert routes.engine().added_by(pid, uri)


async def test_id3_tags_give_title_and_artist(fr):
    def frame(fid, text):
        body = b"\x03" + text.encode()
        return fid + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    tag = frame(b"TIT2", "Hello <World>") + frame(b"TPE1", "Ana")
    size = len(tag)
    head = b"ID3\x03\x00\x00" + bytes([(size >> 21) & 127, (size >> 14) & 127,
                                      (size >> 7) & 127, size & 127])
    pid = await _party()
    r = await _post(USER, pid, "x.mp3", head + tag + b"\xff\xfb\x90\x00" * 10)
    assert r.json()["item"]["title"] == "Hello <World>"
    assert r.json()["item"]["artist"] == "Ana"
    metadata = fr.added[-1][1]
    assert "Hello &lt;World&gt;" in metadata and "<dc:creator>Ana</dc:creator>" in metadata


async def test_413_over_limit_deletes_partial(fr, monkeypatch):
    monkeypatch.setattr(files, "MAX_BYTES", 100)
    pid = await _party()
    data = SAMPLES["song.flac"]
    r = await client(USER).request(
        "POST", f"/api/parties/{pid}/files?name=a.flac", raw=[data, b"\x00" * 100])
    assert r.status == 413 and r.json() == {"error": "too_large"}
    assert list((home_dir() / "uploads" / pid).iterdir()) == []
    assert fr.added == []
    # declared length refused up front
    r = await client(USER).request("POST", f"/api/parties/{pid}/files?name=a.flac",
                                   raw=b"x", headers={"content-length": "101"})
    assert r.status == 413


@pytest.mark.parametrize("name,data", [
    ("evil.exe", SAMPLES["song.mp3"]),
    ("noext", SAMPLES["song.mp3"]),
    ("fake.mp3", b"this is not audio at all, just text"),
    ("fake.flac", SAMPLES["song.ogg"]),
    ("fake.wav", b"RIFF\x00\x00\x00\x00AVI LIST"),
])
async def test_415_and_nothing_left(fr, name, data):
    pid = await _party()
    r = await _post(GUEST, pid, name, data)
    assert r.status == 415 and r.json() == {"error": "unsupported_type"}
    folder = home_dir() / "uploads" / pid
    assert not folder.exists() or list(folder.iterdir()) == []
    assert fr.added == []


async def test_empty_is_400_and_unknown_party_404(fr):
    pid = await _party()
    assert (await _post(GUEST, pid, "a.mp3", b"")).status == 400
    assert (await _post(GUEST, "pt_nope", "a.mp3", SAMPLES["song.mp3"])).status == 404


async def test_guest_cannot_play_next(fr):
    pid = await _party()
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/files?name=a.mp3&next=true",
                                    raw=SAMPLES["song.mp3"])
    assert r.status == 403
    r = await client(USER).request("POST", f"/api/parties/{pid}/files?name=a.mp3&next=true",
                                   raw=SAMPLES["song.mp3"])
    assert r.status == 200 and fr.added[-1][2].get("next_") is True


async def _uploaded(fr):
    pid = await _party()
    await _post(USER, pid, "a.flac", SAMPLES["song.flac"])
    uri = fr.added[-1][0]
    return pid, uri[uri.index("/api/home/files/"):]


async def test_range_requests(fr):
    pid, path = await _uploaded(fr)
    full = SAMPLES["song.flac"]
    c = client(USER)
    r = await c.request("GET", path, headers={"Range": "bytes=2-5"})
    assert r.status == 206 and r.body == full[2:6]
    assert r.headers["content-range"] == f"bytes 2-5/{len(full)}"
    r = await c.request("GET", path, headers={"Range": "bytes=10-"})
    assert r.status == 206 and r.body == full[10:]
    r = await c.request("GET", path, headers={"Range": "bytes=-4"})
    assert r.status == 206 and r.body == full[-4:]
    r = await c.request("GET", path, headers={"Range": f"bytes={len(full)}-"})
    assert r.status == 416 and r.headers["content-range"] == f"bytes */{len(full)}"
    r = await c.request("GET", path, headers={"Range": "bytes=0-1,3-4"})  # ignored form
    assert r.status == 200 and r.body == full
    r = await c.request("HEAD", path, headers={"Range": "bytes=0-3"})
    assert r.status == 206 and r.body == b"" and r.headers["content-length"] == "4"


async def test_bad_keys_are_404(fr):
    c = client(USER)
    for p in ("/api/home/files/short.mp3", "/api/home/files/" + "a" * 20 + ".mp3",
              "/api/home/files/" + "a" * 20 + ".exe", "/api/home/files/..%2Fx.mp3"):
        assert (await c.request("GET", p)).status == 404


async def test_files_deleted_when_party_ends(fr):
    pid, path = await _uploaded(fr)
    folder = home_dir() / "uploads" / pid
    assert any(folder.iterdir())
    r = await client(USER).request("POST", f"/api/parties/{pid}/end", {})
    assert r.status == 200
    assert not folder.exists()
    assert (await client(USER).request("GET", path)).status == 404


async def test_startup_cleanup_removes_orphans(fr):
    pid, path = await _uploaded(fr)
    orphan = home_dir() / "uploads" / "pt_gone"
    orphan.mkdir(parents=True)
    (orphan / "x.mp3").write_bytes(b"x")
    routes.reset()                      # a restart: a new engine is made
    await client(USER).request("GET", "/api/parties")
    assert not orphan.exists()
    assert (home_dir() / "uploads" / pid).exists()      # the live party keeps its files
