"""YouTube resolver: search, metadata and the audio-only stream URL.

Wraps yt-dlp (the Python library). Every yt-dlp call goes through ``_ydl`` so
tests replace that one function. Nothing is written to disk; results are
cached in memory only.

YouTube serves its stream URLs behind JavaScript challenges; yt-dlp solves
them with the ``yt-dlp-ejs`` scripts (a dependency) run by a JavaScript
runtime. Node is the one Sonora's image carries, so ``_opts`` enables it by
path whenever it is on PATH (yt-dlp enables only deno by default).

At most ``JOBS`` yt-dlp runs go at once, so a burst of guest searches cannot
pile up threads; a speaker's re-resolve (the media relay asking for a fresh
stream URL) has a slot of its own, so it never waits behind them. One video
is extracted once at a time: concurrent callers share the one run, which
fills both the metadata and the stream-URL caches.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import weakref
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

__all__ = [
    "Video", "AudioSource", "YouTubeError", "NoAudio",
    "parse_video_id", "search", "info", "audio_source", "forget_audio",
    "until_available",
]

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
          "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_INFO_TTL = 3600.0
_FALLBACK_TTL = 3600.0
_EXPIRE_MARGIN = 600.0
#: How long "this video has no m4a audio" is remembered.
_NO_AUDIO_TTL = 600.0
#: yt-dlp runs at once (searches, adds, visuals); speaker re-resolves have
#: one more slot of their own.
JOBS = 3
PRIORITY_JOBS = 1


class YouTubeError(Exception):
    """yt-dlp could not do what was asked (removed video, network, ...)."""


class NoAudio(YouTubeError):
    """The video has no m4a (AAC) audio-only format."""


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    channel: str
    duration: int
    thumbnail: str


@dataclass
class AudioSource:
    url: str
    headers: dict[str, str]
    content_type: str
    size: int | None
    expires: float
    #: YouTube's pre-playback wait: googlevideo answers 403 before this
    #: time (epoch seconds; yt-dlp's ``available_at``). 0 when none.
    available_at: float = 0.0


def parse_video_id(text: str) -> str | None:
    """The video id in a pasted link or bare id, else None."""
    text = (text or "").strip()
    if _ID.fullmatch(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        url = urlparse(text)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    if url.scheme not in ("http", "https"):
        return None
    candidate = None
    if host in ("youtu.be", "www.youtu.be"):
        candidate = url.path.lstrip("/").split("/")[0]
    elif host in _HOSTS:
        parts = [p for p in url.path.split("/") if p]
        if url.path.rstrip("/") == "/watch":
            candidate = (parse_qs(url.query).get("v") or [""])[0]
        elif len(parts) == 2 and parts[0] in ("shorts", "embed", "live", "v"):
            candidate = parts[1]
    return candidate if candidate and _ID.fullmatch(candidate) else None


def _ydl(opts: dict, query: str, download: bool = False) -> dict:
    """The only place yt-dlp runs (blocking). Monkeypatched in tests."""
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        result = ydl.extract_info(query, download=download)
    return result or {}


def _opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    node = shutil.which("node")
    if node:
        # yt-dlp's ``js_runtimes``: {runtime: {"path": ...}}; the challenge
        # scripts come from the yt-dlp-ejs package.
        opts["js_runtimes"] = {"node": {"path": node}}
    opts.update(extra)
    return opts


#: ``loop -> (general, priority)`` semaphores: asyncio primitives belong to
#: one event loop, and tests run many.
_gates: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _gate(priority: bool) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    pair = _gates.get(loop)
    if pair is None:
        pair = _gates[loop] = (asyncio.Semaphore(JOBS), asyncio.Semaphore(PRIORITY_JOBS))
    return pair[1] if priority else pair[0]


async def _run(query: str, *, priority: bool = False, **extra) -> dict:
    async with _gate(priority):
        try:
            return await asyncio.to_thread(_ydl, _opts(**extra), query)
        except YouTubeError:
            raise
        except Exception as exc:  # yt-dlp raises DownloadError and friends
            raise YouTubeError(str(exc) or exc.__class__.__name__) from exc


def _thumb(d: dict) -> str:
    if d.get("thumbnail"):
        return d["thumbnail"]
    thumbs = [t for t in (d.get("thumbnails") or []) if t.get("url")]
    return thumbs[-1]["url"] if thumbs else ""


def _video(d: dict, vid: str | None = None) -> Video:
    return Video(
        id=vid or d["id"],
        title=d.get("title") or "",
        channel=d.get("channel") or d.get("uploader") or "",
        duration=int(d.get("duration") or 0),
        thumbnail=_thumb(d),
    )


async def search(term: str, limit: int = 10) -> list[Video]:
    data = await _run(f"ytsearch{limit}:{term}", extract_flat=True)
    out = []
    for e in data.get("entries") or []:
        if e and e.get("id") and _ID.fullmatch(str(e["id"])):
            out.append(_video(e))
    return out[:limit]


_info_cache: dict[str, tuple[float, Video]] = {}
_audio_cache: dict[str, AudioSource] = {}
_no_audio: dict[str, float] = {}
#: ``video id -> task`` of the extraction under way for it.
_inflight: dict[str, asyncio.Task] = {}


def _cache_clear() -> None:
    _info_cache.clear()
    _audio_cache.clear()
    _no_audio.clear()
    _inflight.clear()


def _expiry(url: str) -> float:
    try:
        exp = float(parse_qs(urlparse(url).query)["expire"][0])
        return exp - _EXPIRE_MARGIN
    except (KeyError, IndexError, ValueError):
        return time.time() + _FALLBACK_TTL


def _audio_of(data: dict) -> AudioSource | None:
    formats = [
        f for f in data.get("formats") or []
        if f.get("ext") == "m4a" and f.get("url")
        and f.get("vcodec") in (None, "none")
    ]
    if not formats:
        return None
    best = max(formats, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    size = best.get("filesize") or best.get("filesize_approx")
    return AudioSource(
        url=best["url"],
        headers=dict(best.get("http_headers") or {}),
        content_type="audio/mp4",
        size=int(size) if size else None,
        expires=_expiry(best["url"]),
        available_at=float(best.get("available_at") or 0),
    )


#: The longest Sonora waits out a pre-playback period.
_MAX_WAIT = 15.0


async def until_available(src: AudioSource) -> None:
    """Sleep until ``src`` may be fetched: YouTube now makes a stream URL wait
    a few seconds after it is issued (fetching sooner is a 403)."""
    delay = src.available_at - time.time()
    if delay > 0:
        await asyncio.sleep(min(delay, _MAX_WAIT))


async def _extract(video_id: str, priority: bool) -> None:
    data = await _run(f"https://www.youtube.com/watch?v={video_id}", priority=priority)
    now = time.time()
    _info_cache[video_id] = (now + _INFO_TTL, _video(data, video_id))
    src = _audio_of(data)
    if src is None:
        _audio_cache.pop(video_id, None)
        _no_audio[video_id] = now + _NO_AUDIO_TTL
    else:
        _audio_cache[video_id] = src
        _no_audio.pop(video_id, None)


async def _resolve(video_id: str, priority: bool = False) -> None:
    """Extract ``video_id`` once, filling both caches. A caller arriving while
    an extraction of the same video is under way awaits that one; one
    caller giving up (a speaker hanging up) does not cancel it for others."""
    task = _inflight.get(video_id)
    if task is None or task.get_loop() is not asyncio.get_running_loop():
        task = asyncio.ensure_future(_extract(video_id, priority))
        _inflight[video_id] = task

        def _done(t: asyncio.Task, vid: str = video_id) -> None:
            if _inflight.get(vid) is t:
                del _inflight[vid]
            if not t.cancelled():
                t.exception()        # retrieved: no "never retrieved" warning

        task.add_done_callback(_done)
    await asyncio.shield(task)


async def info(video_id: str) -> Video:
    hit = _info_cache.get(video_id)
    if hit and hit[0] > time.time():
        return hit[1]
    await _resolve(video_id)
    return _info_cache[video_id][1]


def forget_audio(video_id: str) -> None:
    """Drop the cached stream URL (googlevideo answered 403/410: it expired
    early or was revoked), so the next ``audio_source`` resolves afresh."""
    _audio_cache.pop(video_id, None)


def _cached_audio(video_id: str) -> AudioSource | None:
    hit = _audio_cache.get(video_id)
    if hit and hit.expires > time.time():
        return hit
    if _no_audio.get(video_id, 0) > time.time():
        raise NoAudio(video_id)
    return None


async def audio_source(video_id: str, priority: bool = False) -> AudioSource:
    """The stream URL for ``video_id``; ``priority`` for a speaker's request
    (its own yt-dlp slot)."""
    hit = _cached_audio(video_id)
    if hit is not None:
        return hit
    await _resolve(video_id, priority)
    hit = _cached_audio(video_id)
    if hit is None:   # pragma: no cover - _extract fills one or the other
        raise NoAudio(video_id)
    return hit
