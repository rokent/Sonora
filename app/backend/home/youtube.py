"""YouTube resolver: search, metadata and the audio-only stream URL.

Wraps yt-dlp (the Python library). Every yt-dlp call goes through ``_ydl`` so
tests replace that one function. Nothing is written to disk; results are
cached in memory only.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

__all__ = [
    "Video", "AudioSource", "YouTubeError", "NoAudio",
    "parse_video_id", "search", "info", "audio_source", "forget_audio",
]

_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
          "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_INFO_TTL = 3600.0
_FALLBACK_TTL = 3600.0
_EXPIRE_MARGIN = 600.0


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
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    opts.update(extra)
    return opts


async def _run(query: str, **extra) -> dict:
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


def _cache_clear() -> None:
    _info_cache.clear()
    _audio_cache.clear()


async def info(video_id: str) -> Video:
    hit = _info_cache.get(video_id)
    if hit and hit[0] > time.time():
        return hit[1]
    data = await _run(f"https://www.youtube.com/watch?v={video_id}")
    video = _video(data, video_id)
    _info_cache[video_id] = (time.time() + _INFO_TTL, video)
    return video


def _expiry(url: str) -> float:
    try:
        exp = float(parse_qs(urlparse(url).query)["expire"][0])
        return exp - _EXPIRE_MARGIN
    except (KeyError, IndexError, ValueError):
        return time.time() + _FALLBACK_TTL


def forget_audio(video_id: str) -> None:
    """Drop the cached stream URL (googlevideo answered 403/410: it expired
    early or was revoked), so the next ``audio_source`` resolves afresh."""
    _audio_cache.pop(video_id, None)


async def audio_source(video_id: str) -> AudioSource:
    hit = _audio_cache.get(video_id)
    if hit and hit.expires > time.time():
        return hit
    data = await _run(f"https://www.youtube.com/watch?v={video_id}")
    formats = [
        f for f in data.get("formats") or []
        if f.get("ext") == "m4a" and f.get("url")
        and f.get("vcodec") in (None, "none")
    ]
    if not formats:
        raise NoAudio(video_id)
    best = max(formats, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    size = best.get("filesize") or best.get("filesize_approx")
    src = AudioSource(
        url=best["url"],
        headers=dict(best.get("http_headers") or {}),
        content_type="audio/mp4",
        size=int(size) if size else None,
        expires=_expiry(best["url"]),
    )
    _audio_cache[video_id] = src
    return src
