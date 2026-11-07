"""Sonora Home media relay: speakers fetch YouTube audio from Sonora.

A queue item is ``http://<lan-ip>:<port>/api/home/media/<key>.m4a``; each
request resolves the (cached) googlevideo URL and relays it with Range
pass-through. The fake upstream is a local aiohttp server; nothing reaches
the network.
"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from backend import main
from backend.config import settings
from backend.home import media, youtube
from backend.home.youtube import AudioSource, Video

from home_client import Client

DATA = bytes(range(256)) * 40          # 10240 bytes of "audio"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    for name in ("SONORA_OIDC_ISSUER", "SONORA_OIDC_GROUP", "SONORA_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main.fetchguard, "safe_to_fetch", lambda url: True)
    youtube._cache_clear()
    yield tmp_path
    youtube._cache_clear()


class Upstream:
    """A googlevideo stand-in: serves DATA with Range support at /good, and
    403s at /expired. Records every request's headers."""

    def __init__(self):
        self.seen: list[tuple[str, str, dict]] = []
        app = web.Application()
        app.router.add_route("*", "/good", self.good)
        app.router.add_route("*", "/expired", self.expired)
        app.router.add_route("*", "/gone", self.gone)
        self.server = TestServer(app)

    def url(self, path):
        return str(self.server.make_url(path))

    async def good(self, request):
        self.seen.append(("good", request.method, dict(request.headers)))
        rng = request.headers.get("Range")
        headers = {"Accept-Ranges": "bytes", "Content-Type": "audio/mp4"}
        if rng:
            start, _, end = rng.removeprefix("bytes=").partition("-")
            start = int(start)
            end = int(end) if end else len(DATA) - 1
            body = DATA[start:end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(DATA)}"
            status = 206
        else:
            body, status = DATA, 200
        if request.method == "HEAD":
            headers["Content-Length"] = str(len(body))
            return web.Response(status=status, headers=headers)
        return web.Response(body=body, status=status, headers=headers)

    async def expired(self, request):
        self.seen.append(("expired", request.method, dict(request.headers)))
        return web.Response(status=403, text="expired")

    async def gone(self, request):
        self.seen.append(("gone", request.method, dict(request.headers)))
        return web.Response(status=410, text="gone")


def run(coro_fn, monkeypatch, sources):
    """Start the fake upstream, point ``audio_source`` at ``sources`` (a list
    of paths handed out in order, one per resolve), then run ``coro_fn``."""

    async def go():
        up = Upstream()
        await up.server.start_server()
        session = aiohttp.ClientSession()
        monkeypatch.setitem(main.state, "outward", session)
        resolves = []
        queue = list(sources)

        async def fake_source(video_id):
            hit = youtube._audio_cache.get(video_id)
            if hit is not None:
                return hit
            resolves.append(video_id)
            path = queue.pop(0) if len(queue) > 1 else queue[0]
            src = AudioSource(url=up.url(path), headers={"User-Agent": "yt"},
                              content_type="audio/mp4", size=len(DATA),
                              expires=time.time() + 3600)
            youtube._audio_cache[video_id] = src
            return src

        monkeypatch.setattr(youtube, "audio_source", fake_source)
        try:
            return await coro_fn(up, resolves)
        finally:
            await session.close()
            await up.server.close()

    return asyncio.run(go())


def client():
    return Client(main.app, host="sonora.local")


def test_unknown_key_is_404(monkeypatch):
    async def go(up, resolves):
        r = await client().request("GET", "/api/home/media/nosuchkeyAAAAAAAAAAAAA.m4a")
        assert r.status == 404
        assert resolves == [] and up.seen == []
    run(go, monkeypatch, ["/good"])


def test_full_file_is_relayed_without_session(monkeypatch):
    # Sign-in on: a speaker carries no session and must still get the file.
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        r = await client().request("GET", f"/api/home/media/{key}.m4a")
        assert r.status == 200
        assert r.body == DATA
        assert r.headers["content-type"] == "audio/mp4"
        assert r.headers["content-length"] == str(len(DATA))
        assert r.headers["accept-ranges"] == "bytes"
        assert up.seen[0][2]["User-Agent"] == "yt"
        assert resolves == ["dQw4w9WgXcQ"]
    run(go, monkeypatch, ["/good"])


def test_range_is_forwarded_and_206_returned(monkeypatch):
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        r = await client().request("GET", f"/api/home/media/{key}.m4a",
                                   headers={"Range": "bytes=100-199"})
        assert r.status == 206
        assert r.body == DATA[100:200]
        assert r.headers["content-range"] == f"bytes 100-199/{len(DATA)}"
        assert r.headers["content-length"] == "100"
        assert r.headers["content-type"] == "audio/mp4"
        assert up.seen[0][2]["Range"] == "bytes=100-199"
    run(go, monkeypatch, ["/good"])


def test_head_answers_headers_without_body(monkeypatch):
    monkeypatch.setenv("SONORA_OIDC_ISSUER", "https://idp.example")
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        r = await client().request("HEAD", f"/api/home/media/{key}.m4a")
        assert r.status == 200
        assert r.body == b""
        assert r.headers["content-length"] == str(len(DATA))
        assert r.headers["content-type"] == "audio/mp4"
        assert up.seen[0][1] == "HEAD"
    run(go, monkeypatch, ["/good"])


@pytest.mark.parametrize("dead", ["/expired", "/gone"])
def test_expired_upstream_is_resolved_again(monkeypatch, dead):
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        r = await client().request("GET", f"/api/home/media/{key}.m4a",
                                   headers={"Range": "bytes=0-9"})
        assert r.status == 206
        assert r.body == DATA[:10]
        assert [s[0] for s in up.seen] == [dead.strip("/"), "good"]
        assert up.seen[1][2]["Range"] == "bytes=0-9"
        assert resolves == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"]
        assert youtube._audio_cache["dQw4w9WgXcQ"].url.endswith("/good")
    run(go, monkeypatch, [dead, "/good"])


def test_still_expired_after_retry_is_502(monkeypatch):
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        r = await client().request("GET", f"/api/home/media/{key}.m4a")
        assert r.status == 502
        assert len(up.seen) == 2           # one retry only
        assert "dQw4w9WgXcQ" not in youtube._audio_cache
    run(go, monkeypatch, ["/expired"])


def test_resolver_failure_is_502(monkeypatch):
    key = media.keys().issue("dQw4w9WgXcQ")

    async def go(up, resolves):
        async def broken(video_id):
            raise youtube.YouTubeError("removed")
        monkeypatch.setattr(youtube, "audio_source", broken)
        r = await client().request("GET", f"/api/home/media/{key}.m4a")
        assert r.status == 502
        assert r.json() == {"error": "youtube_failed"}
    run(go, monkeypatch, ["/good"])


def test_keys_persist_across_reload(home):
    path = home / "home" / "media.json"
    keys = media.MediaKeys(path)
    key = keys.issue("dQw4w9WgXcQ")
    assert len(key) >= 22 and key.replace("-", "").replace("_", "").isalnum()
    assert keys.issue("dQw4w9WgXcQ") != key       # each add its own key
    again = media.MediaKeys(path)
    assert again.video_of(key) == "dQw4w9WgXcQ"
    assert again.video_of("nope") is None
    stored = json.loads(path.read_text())
    assert stored[key]["video_id"] == "dQw4w9WgXcQ"


def test_unused_keys_are_purged_after_a_day(home, monkeypatch):
    path = home / "home" / "media.json"
    keys = media.MediaKeys(path)
    old = keys.issue("aaaaaaaaaaa")
    fresh = keys.issue("bbbbbbbbbbb")
    now = time.time()
    monkeypatch.setattr(media.time, "time", lambda: now + 23 * 3600)
    assert keys.video_of(fresh) == "bbbbbbbbbbb"       # touches it
    monkeypatch.setattr(media.time, "time", lambda: now + 25 * 3600)
    reloaded = media.MediaKeys(path)
    assert reloaded.video_of(old) is None
    assert reloaded.video_of(fresh) == "bbbbbbbbbbb"
    assert old not in json.loads(path.read_text())


def test_keys_follow_the_data_dir(tmp_path, monkeypatch):
    first = media.keys().issue("dQw4w9WgXcQ")
    monkeypatch.setattr(settings, "data_dir", tmp_path / "other")
    assert media.keys().video_of(first) is None


def test_didl_escapes_and_carries_metadata():
    video = Video(id="dQw4w9WgXcQ", title='Rock & <Roll> "live"', channel="A&B <c>",
                  duration=3725, thumbnail="https://i.ytimg.com/vi/x/hq.jpg?a=1&b=2")
    uri = "http://10.0.0.5:50205/api/home/media/abc.m4a"
    didl = media.didl_for(video, uri)
    assert "<dc:title>Rock &amp; &lt;Roll&gt; &quot;live&quot;</dc:title>" in didl
    assert "<dc:creator>A&amp;B &lt;c&gt;</dc:creator>" in didl
    assert "<upnp:albumArtURI>https://i.ytimg.com/vi/x/hq.jpg?a=1&amp;b=2</upnp:albumArtURI>" in didl
    assert "<upnp:class>object.item.audioItem.musicTrack</upnp:class>" in didl
    assert (f'<res protocolInfo="http-get:*:audio/mp4:*" duration="1:02:05">{uri}</res>'
            in didl)
    assert "Rock & " not in didl
    # It parses as XML.
    import xml.etree.ElementTree as ET
    ET.fromstring(didl)


def test_media_url_uses_the_callback_ip(monkeypatch):
    class Events:
        _callback_ip = "192.168.1.20"
    monkeypatch.setitem(main.state, "controller", type("C", (), {"events": Events()})())
    monkeypatch.setattr(settings, "port", 50205)
    assert media.media_url("k1", "192.168.1.50") == \
        "http://192.168.1.20:50205/api/home/media/k1.m4a"


def test_media_url_falls_back_to_route_to_speaker(monkeypatch):
    class Events:
        _callback_ip = None
    monkeypatch.setitem(main.state, "controller", type("C", (), {"events": Events()})())
    monkeypatch.setattr(settings, "port", 1234)
    seen = []
    monkeypatch.setattr(media.events, "local_ip_for",
                        lambda host: seen.append(host) or "10.1.1.1")
    assert media.media_url("k2", "10.1.1.9") == "http://10.1.1.1:1234/api/home/media/k2.m4a"
    assert seen == ["10.1.1.9"]


def test_forget_audio_drops_the_cache_entry():
    youtube._audio_cache["x"] = AudioSource("u", {}, "audio/mp4", None, time.time() + 99)
    youtube.forget_audio("x")
    youtube.forget_audio("never-cached")
    assert "x" not in youtube._audio_cache
