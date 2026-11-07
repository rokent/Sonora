"""YouTube resolver: link parsing and yt-dlp mapping. yt-dlp is never run."""

import time

import pytest

from backend.home import youtube

VID = "dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _clear_caches():
    youtube._cache_clear()
    yield
    youtube._cache_clear()


@pytest.mark.parametrize("text", [
    VID,
    f"  {VID}  ",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?t=42",
    f"https://www.youtube.com/watch?v={VID}&t=30s",
    f"https://youtube.com/watch?feature=share&v={VID}",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"www.youtube.com/watch?v={VID}",
])
def test_parse_video_id_forms(text):
    assert youtube.parse_video_id(text) == VID


@pytest.mark.parametrize("text", [
    "https://evil.example/watch?v=x",
    f"https://evil.example/watch?v={VID}",
    f"https://youtube.com.evil.example/watch?v={VID}",
    "https://www.youtube.com/watch?v=short",
    "https://www.youtube.com/",
    "", "hello world", "dQw4w9WgXc", "dQw4w9WgXcQQ",
])
def test_parse_video_id_rejects(text):
    assert youtube.parse_video_id(text) is None


def _patch(monkeypatch, result, calls=None):
    def fake(opts, query, download=False):
        if calls is not None:
            calls.append((opts, query))
        return result
    monkeypatch.setattr(youtube, "_ydl", fake)


async def test_search_maps_entries(monkeypatch):
    calls = []
    _patch(monkeypatch, {"entries": [
        {"id": VID, "title": "Song", "channel": "Rick", "duration": 212.0,
         "thumbnails": [{"url": "http://t/a.jpg"}, {"url": "http://t/b.jpg"}]},
        {"id": "aaaaaaaaaaa", "title": "Two", "uploader": "Up", "duration": None},
        None,
        {"title": "no id"},
    ]}, calls)
    out = await youtube.search("never gonna", limit=4)
    assert calls[0][1] == "ytsearch4:never gonna"
    assert calls[0][0]["extract_flat"] is True
    assert out == [
        youtube.Video(VID, "Song", "Rick", 212, "http://t/b.jpg"),
        youtube.Video("aaaaaaaaaaa", "Two", "Up", 0, ""),
    ]


async def test_search_failure_wraps(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(youtube, "_ydl", boom)
    with pytest.raises(youtube.YouTubeError):
        await youtube.search("x")


async def test_info_cached(monkeypatch):
    calls = []
    _patch(monkeypatch, {"id": VID, "title": "T", "channel": "C",
                         "duration": 10, "thumbnail": "http://t"}, calls)
    a = await youtube.info(VID)
    b = await youtube.info(VID)
    assert a == b == youtube.Video(VID, "T", "C", 10, "http://t")
    assert len(calls) == 1


def _fmt(ext, url, **kw):
    return {"ext": ext, "url": url, "acodec": "mp4a.40.2", "vcodec": "none",
            "http_headers": {"User-Agent": "ua"}, **kw}


async def test_audio_source_picks_m4a(monkeypatch):
    exp = int(time.time()) + 3600
    _patch(monkeypatch, {"id": VID, "formats": [
        _fmt("webm", "http://g/webm"),
        _fmt("m4a", f"http://g/low?expire={exp}", abr=48, filesize=100),
        _fmt("m4a", f"http://g/high?expire={exp}", abr=128, filesize=5000),
    ]})
    src = await youtube.audio_source(VID)
    assert src.url == f"http://g/high?expire={exp}"
    assert src.headers == {"User-Agent": "ua"}
    assert src.content_type == "audio/mp4"
    assert src.size == 5000
    assert src.expires == exp - 600


async def test_no_m4a_raises_noaudio(monkeypatch):
    _patch(monkeypatch, {"id": VID, "formats": [_fmt("webm", "http://g/w")]})
    with pytest.raises(youtube.NoAudio):
        await youtube.audio_source(VID)
    assert issubclass(youtube.NoAudio, youtube.YouTubeError)


async def test_audio_source_cache_respects_expire(monkeypatch):
    calls = []
    now = time.time()

    def fake(opts, query, download=False):
        calls.append(query)
        exp = int(now) + (3600 if len(calls) == 1 else 7200)
        return {"formats": [_fmt("m4a", f"http://g/{len(calls)}?expire={exp}")]}
    monkeypatch.setattr(youtube, "_ydl", fake)

    first = await youtube.audio_source(VID)
    assert (await youtube.audio_source(VID)) is first
    assert len(calls) == 1
    # Past the safety margin (10 min before expire): resolved again.
    monkeypatch.setattr(youtube.time, "time", lambda: first.expires + 1)
    second = await youtube.audio_source(VID)
    assert len(calls) == 2 and second.url != first.url


async def test_audio_source_fallback_expiry(monkeypatch):
    _patch(monkeypatch, {"formats": [_fmt("m4a", "http://g/noexpire")]})
    before = time.time()
    src = await youtube.audio_source(VID)
    assert before + 3500 < src.expires <= time.time() + 3600


async def test_js_runtime_option(monkeypatch):
    calls = []
    _patch(monkeypatch, {"formats": [_fmt("m4a", "http://g/x")]}, calls)
    monkeypatch.setattr(youtube.shutil, "which", lambda n: "/usr/bin/node")
    await youtube.audio_source(VID)
    assert calls[0][0]["js_runtimes"] == {"node": {}}
    assert calls[0][0]["noplaylist"] is True
