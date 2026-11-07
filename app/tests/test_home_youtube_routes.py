"""YouTube search and add-to-party routes: status paths, guests, attribution,
and an untouched queue on every failure. No network: youtube is faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.home import media, youtube
from backend.home.youtube import AudioSource, Video

from test_home_routes import (GUEST, OTHER_GUEST, USER, _start, client,  # noqa: F401
                              rig)

VID = "dQw4w9WgXcQ"
VIDEO = Video(VID, "Never Gonna <Give> You Up", "Rick", 213, "http://t/x.jpg")


@pytest.fixture
def yt(rig, monkeypatch):
    rig.ctl.events = SimpleNamespace(_callback_ip="10.0.0.5")
    state = SimpleNamespace(searched=[], probed=[], info_error=None, audio_error=None)

    async def info(video_id):
        if state.info_error:
            raise state.info_error
        return VIDEO

    async def audio_source(video_id):
        state.probed.append(video_id)
        if state.audio_error:
            raise state.audio_error
        return AudioSource("http://g/x", {}, "audio/mp4", None, 0)

    async def search(term, limit=10):
        state.searched.append((term, limit))
        return [Video(f"{i:011d}", f"t{i}", "c", 60, "") for i in range(15)]

    monkeypatch.setattr(youtube, "info", info)
    monkeypatch.setattr(youtube, "audio_source", audio_source)
    monkeypatch.setattr(youtube, "search", search)
    rig.yt = state
    return rig


async def _party():
    return (await _start()).json()["id"]


async def test_search_returns_results_for_guest_and_user(yt):
    for who in (GUEST, USER):
        r = await client(who).request("GET", "/api/youtube/search?q=%20rick%20")
        assert r.status == 200
        res = r.json()["results"]
        assert len(res) == 10
        assert set(res[0]) == {"id", "title", "channel", "duration", "thumbnail"}
    assert yt.yt.searched[0] == ("rick", 10)


@pytest.mark.parametrize("q", ["", "   ", "x" * 101])
async def test_search_bad_query_is_400(yt, q):
    r = await client(GUEST).request("GET", f"/api/youtube/search?q={q}")
    assert r.status == 400 and r.json() == {"error": "bad_query"}
    assert yt.yt.searched == []


async def test_search_missing_q_is_400_and_100_is_ok(yt):
    assert (await client(GUEST).request("GET", "/api/youtube/search")).status == 400
    assert (await client(GUEST).request("GET", f"/api/youtube/search?q={'x' * 100}")).status == 200


async def test_search_failure_is_502(yt, monkeypatch):
    async def boom(term, limit=10):
        raise youtube.YouTubeError("net")
    monkeypatch.setattr(youtube, "search", boom)
    r = await client(GUEST).request("GET", "/api/youtube/search?q=a")
    assert r.status == 502 and r.json() == {"error": "youtube_failed"}


async def test_guest_adds_with_attribution_and_badge(yt):
    pid = await _party()
    g = client(GUEST)
    r = await g.request("POST", f"/api/parties/{pid}/youtube",
                        {"video": f"https://youtu.be/{VID}?t=5"})
    assert r.status == 200, r.body
    assert r.json() == {"ok": True, "item": {"id": VID, "title": VIDEO.title, "channel": "Rick",
                                             "duration": 213, "thumbnail": "http://t/x.jpg"}}
    uri = yt.ctl.queues["A"][0]["uri"]
    assert uri.startswith("http://10.0.0.5:") and uri.endswith(".m4a")
    assert f":{settings.port}/api/home/media/" in uri
    assert media.keys().video_of(uri.rsplit("/", 1)[1][:-4]) == VID
    assert yt.yt.probed == [VID]
    q = (await g.request("GET", f"/api/parties/{pid}/queue")).json()["items"][0]
    assert q["source"] == "youtube" and q["video_id"] == VID
    assert q["added_by"] == {"id": "guest:1", "name": "Mara"} and q["can_remove"] is True
    other = (await client(OTHER_GUEST).request("GET", f"/api/parties/{pid}/queue")).json()["items"][0]
    assert other["can_remove"] is False


async def test_other_items_have_no_youtube_fields(yt):
    pid = await _party()
    await client(GUEST).request("POST", f"/api/parties/{pid}/queue", {"uri": "x-file:1"})
    item = (await client(GUEST).request("GET", f"/api/parties/{pid}/queue")).json()["items"][0]
    assert "source" not in item and "video_id" not in item


async def test_next_is_passed_to_the_speaker(yt, monkeypatch):
    pid = await _party()
    seen = []
    orig = yt.cmds.add_to_queue

    async def spy(zone, uri, metadata="", *, next_=False, position=0):
        seen.append((next_, "Never Gonna &lt;Give&gt;" in metadata))
        return await orig(zone, uri, metadata, next_=next_, position=position)
    monkeypatch.setattr(yt.cmds, "add_to_queue", spy)
    r = await client(USER).request("POST", f"/api/parties/{pid}/youtube",
                                   {"video": VID, "next": True})
    assert r.status == 200 and seen == [(True, True)]


@pytest.mark.parametrize("video", ["https://evil.example/watch?v=x", "nonsense", "x" * 12])
async def test_bad_link_is_400_and_queue_untouched(yt, video):
    pid = await _party()
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": video})
    assert r.status == 400 and r.json() == {"error": "bad_link"}
    assert yt.ctl.queues["A"] == [] and yt.yt.probed == []


async def test_empty_video_is_422_validation(yt):
    pid = await _party()
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": ""})
    assert r.status == 422 and yt.ctl.queues["A"] == []


async def test_info_failure_is_502_and_queue_untouched(yt):
    pid = await _party()
    yt.yt.info_error = youtube.YouTubeError("removed")
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": VID})
    assert r.status == 502 and r.json() == {"error": "youtube_failed"}
    assert yt.ctl.queues["A"] == []
    assert not any(c[0] == "add" for c in yt.cmds.calls)
    listed = (await client(USER).request("GET", "/api/parties")).json()["parties"][0]
    assert listed["queue_owners"] == {}


async def test_probe_failure_is_502_and_no_audio_is_422(yt):
    pid = await _party()
    yt.yt.audio_error = youtube.YouTubeError("403")
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": VID})
    assert r.status == 502 and r.json() == {"error": "youtube_failed"}
    yt.yt.audio_error = youtube.NoAudio("no m4a")
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": VID})
    assert r.status == 422 and r.json() == {"error": "no_audio"}
    assert yt.ctl.queues["A"] == [] and not any(c[0] == "add" for c in yt.cmds.calls)


async def test_unknown_party_is_404_without_calling_youtube(yt):
    r = await client(GUEST).request("POST", "/api/parties/nope/youtube", {"video": VID})
    assert r.status == 404 and yt.yt.probed == []


async def test_ended_party_is_404_for_guests(yt):
    pid = await _party()
    await client(USER).request("POST", f"/api/parties/{pid}/end")
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube", {"video": VID})
    assert r.status == 404 and yt.yt.probed == []


async def test_guest_cannot_play_next(yt):
    pid = await _party()
    r = await client(GUEST).request("POST", f"/api/parties/{pid}/youtube",
                                    {"video": VID, "next": True})
    assert r.status == 403 and r.json() == {"error": "forbidden"}
    assert yt.ctl.queues["A"] == [] and yt.yt.probed == []


async def test_user_play_next_is_attributed_with_a_fresh_key(yt):
    pid = await _party()
    u = client(USER)
    for nxt in (False, True):
        r = await u.request("POST", f"/api/parties/{pid}/youtube", {"video": VID, "next": nxt})
        assert r.status == 200
    uris = [i["uri"] for i in yt.ctl.queues["A"]]
    assert len(set(uris)) == 2                      # same video, two keys
    items = (await u.request("GET", f"/api/parties/{pid}/queue")).json()["items"]
    assert [i["added_by"] for i in items] == [{"id": "sub-1", "name": "Tudor"}] * 2
