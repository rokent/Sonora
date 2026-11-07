"""YouTube audio for the speakers: opaque media keys and a Range relay.

A queue item is ``http://<lan-ip>:<port>/api/home/media/<key>.m4a``. The key
maps only to a YouTube video id (kept in ``home/media.json`` so queued items
survive a restart, purged after 24 h unused). Each speaker request resolves
the googlevideo URL (cached in ``youtube``) and relays it with Range
pass-through, so the speaker sees an ordinary progressive AAC file it can seek
in. Nothing is stored on disk but the key map.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import settings
from ..sonos import events
from . import youtube
from .store import home_dir, read_json, write_json
from .youtube import Video

__all__ = ["MEDIA_URI", "MediaKeys", "keys", "media_url", "didl_for", "router"]

router = APIRouter()

_TTL = 24 * 3600.0
#: How stale ``used`` on disk may get before a touch is written: speakers
#: make many range requests per track, and 24 h purges do not need precision.
_TOUCH_WRITE = 600.0
_KEY = re.compile(r"[A-Za-z0-9_-]{16,64}")
#: A queue item's URI as ``media_url`` builds it; group 1 is the key.
MEDIA_URI = re.compile(r"https?://[^/]+/api/home/media/([A-Za-z0-9_-]{16,64})\.m4a")
_PASSED = ("Content-Length", "Content-Range", "Accept-Ranges")
#: The only Range form relayed (one range); anything else is ignored.
_RANGE = re.compile(r"bytes=\d*-\d*")


class MediaKeys:
    """``key -> {video_id, used}``, persisted atomically."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        raw = read_json(path, {})
        self._keys: dict[str, dict] = {
            k: {"video_id": str(v["video_id"]), "used": float(v.get("used", 0))}
            for k, v in (raw.items() if isinstance(raw, dict) else ())
            if isinstance(v, dict) and v.get("video_id")
        }
        self._written = {k: v["used"] for k, v in self._keys.items()}
        if self._purge():
            self._save()

    def _purge(self) -> bool:
        cutoff = time.time() - _TTL
        old = [k for k, v in self._keys.items() if v["used"] < cutoff]
        for k in old:
            del self._keys[k]
        return bool(old)

    def _save(self) -> None:
        write_json(self.path, self._keys)
        self._written = {k: v["used"] for k, v in self._keys.items()}

    def issue(self, video_id: str) -> str:
        if not youtube._ID.fullmatch(video_id or ""):
            raise ValueError(f"not a YouTube video id: {video_id!r}")
        key = secrets.token_urlsafe(16)
        with self._lock:
            self._purge()
            self._keys[key] = {"video_id": video_id, "used": time.time()}
            self._save()
        return key

    def video_of(self, key: str) -> str | None:
        with self._lock:
            entry = self._keys.get(key)
            now = time.time()
            if entry is None or entry["used"] < now - _TTL:
                return None
            entry["used"] = now
            if now - self._written.get(key, 0) > _TOUCH_WRITE:
                self._save()
            return entry["video_id"]


_keys: MediaKeys | None = None


def keys() -> MediaKeys:
    """The key map in the current data dir (re-read if the dir changes)."""
    global _keys
    path = home_dir() / "media.json"
    if _keys is None or _keys.path != path:
        _keys = MediaKeys(path)
    return _keys


def media_url(key: str, host_for: str) -> str:
    """The URL a speaker fetches: the address speakers already reach us on
    (the event callback address), else the route toward ``host_for``."""
    from .. import main  # lazy: main imports home

    controller = main.state.get("controller")
    ip = getattr(getattr(controller, "events", None), "_callback_ip", None)
    if not ip:
        ip = events.local_ip_for(host_for)
    return f"http://{ip}:{settings.port}/api/home/media/{key}.m4a"


def _x(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _hms(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def didl_for(video: Video, uri: str) -> str:
    """The DIDL-Lite a speaker gets with a YouTube queue item."""
    art = (f"<upnp:albumArtURI>{_x(video.thumbnail)}</upnp:albumArtURI>"
           if video.thumbnail else "")
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        f'<item id="youtube:{_x(video.id)}" parentID="youtube" restricted="true">'
        f'<res protocolInfo="http-get:*:audio/mp4:*" duration="{_hms(video.duration)}">'
        f"{_x(uri)}</res>{art}"
        f"<dc:title>{_x(video.title)}</dc:title>"
        f"<dc:creator>{_x(video.channel)}</dc:creator>"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


def _error(status: int, kind: str) -> JSONResponse:
    return JSONResponse({"error": kind}, status_code=status)


async def _open(method: str, video_id: str, rng: str | None):
    """The upstream response for ``video_id``. A 403/410 means the cached
    googlevideo URL expired: drop it, resolve once more and retry once."""
    from .. import main  # lazy: main imports home

    session = main.state["outward"]
    for attempt in (0, 1):
        src = await youtube.audio_source(video_id)
        if not main.fetchguard.safe_to_fetch(src.url):
            main.fetchguard.refuse(src.url, "youtube media")
            raise main.fetchguard.Refused("unsafe youtube media url")
        headers = dict(src.headers)
        if rng:
            headers["Range"] = rng
        upstream = await session.request(
            method, src.url, headers=headers, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15))
        if upstream.status in (403, 410):
            upstream.release()
            youtube.forget_audio(video_id)
            if attempt == 0:
                continue
        return upstream
    return upstream  # pragma: no cover - loop always returns


@router.api_route("/api/home/media/{key}.m4a", methods=["GET", "HEAD"],
                  include_in_schema=False)
async def media(key: str, request: Request) -> Response:
    """Relay a YouTube video's AAC audio to a speaker (no session needed)."""
    from .. import main  # lazy: main imports home

    video_id = keys().video_of(key) if _KEY.fullmatch(key) else None
    if video_id is None:
        return _error(404, "not_found")
    method = "HEAD" if request.method == "HEAD" else "GET"
    try:
        rng = request.headers.get("range")
        if rng is not None and not _RANGE.fullmatch(rng.strip()):
            rng = None
        upstream = await _open(method, video_id, rng.strip() if rng else None)
    except youtube.YouTubeError:
        return _error(502, "youtube_failed")
    except (aiohttp.ClientError, TimeoutError, main.fetchguard.Refused):
        return _error(502, "upstream_failed")
    if upstream.status >= 400 and upstream.status != 416:
        upstream.release()
        return _error(502, "upstream_failed")

    passed = {name: upstream.headers[name] for name in _PASSED
              if name in upstream.headers}
    if upstream.status == 416:
        passed.pop("Content-Length", None)    # the body is not relayed
    if method == "HEAD" or upstream.status == 416:
        upstream.release()
        return Response(status_code=upstream.status, headers=passed,
                        media_type="audio/mp4")

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()

    return StreamingResponse(body(), status_code=upstream.status,
                             media_type="audio/mp4", headers=passed)
