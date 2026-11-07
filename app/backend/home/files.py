"""Music files dropped into a party: stored for the party's lifetime, served
to the speakers with Range support, deleted when the party ends.

``POST /api/parties/{id}/files?name=<file name>`` takes the file's bytes as
the request body (python-multipart is not installed, so there is no multipart
form); it is streamed to disk in chunks and refused past 300 MB. A file is
kept only if its extension is one we accept *and* its first bytes are what
that extension says. Files live in ``home/uploads/<party id>/<key>.<ext>`` and
are served to speakers at ``/api/home/files/<key>.<ext>``.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .store import home_dir

__all__ = ["MAX_BYTES", "FileError", "Stored", "router", "store_stream", "delete_party",
           "cleanup", "file_uri_path", "didl_for", "title_artist"]

router = APIRouter()

MAX_BYTES = 300 * 1024 * 1024
#: All of one party's files together.
PARTY_MAX_BYTES = 2 * 1024 ** 3
#: Free disk space that must remain after an upload.
MIN_FREE_BYTES = 1024 ** 3
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CHUNK = 64 * 1024
_KEY = re.compile(r"[A-Za-z0-9_-]{16,64}")
_PARTY = re.compile(r"[A-Za-z0-9_]{1,64}")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")

#: extension -> (Content-Type, DLNA protocolInfo MIME)
TYPES = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "flac": "audio/flac", "wav": "audio/wav", "ogg": "audio/ogg",
}
_HEAD = 4096


class FileError(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


@dataclass
class Stored:
    key: str
    ext: str
    path: Path
    size: int
    title: str
    artist: str


def _uploads() -> Path:
    path = home_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension_of(name: str) -> str | None:
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    if ext in ("mp4", "m4b"):
        ext = "m4a"
    elif ext in ("oga",):
        ext = "ogg"
    elif ext == "wave":
        ext = "wav"
    return ext if ext in TYPES else None


def magic_ok(ext: str, head: bytes) -> bool:
    if ext == "mp3":
        return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext == "m4a":
        return head[4:8] == b"ftyp"
    if ext == "aac":
        # ADTS frames, or an ID3-tagged stream
        return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF0 == 0xF0)
    if ext == "flac":
        return head[:4] == b"fLaC"
    if ext == "wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == "ogg":
        return head[:4] == b"OggS"
    return False


def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _text_frame(data: bytes) -> str:
    if not data:
        return ""
    enc, body = data[0], data[1:]
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc, "latin-1")
    try:
        return body.decode(codec, errors="replace").split("\x00")[0].strip()
    except (LookupError, UnicodeError):
        return ""


def title_artist(path: Path) -> tuple[str, str]:
    """ID3v2 ``TIT2`` / ``TPE1`` of ``path`` (``""`` for each that is absent
    or not ID3v2)."""
    title = artist = ""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] != b"ID3" or len(head) < 10:
                return "", ""
            version = head[3]
            size = min(_synchsafe(head[6:10]), 1 << 20)
            tag = f.read(size)
    except OSError:
        return "", ""
    if version not in (3, 4):
        return "", ""
    pos = 0
    while pos + 10 <= len(tag):
        fid = tag[pos:pos + 4]
        if not fid.strip(b"\x00"):
            break
        n = _synchsafe(tag[pos + 4:pos + 8]) if version == 4 else int.from_bytes(tag[pos + 4:pos + 8], "big")
        body = tag[pos + 10:pos + 10 + n]
        if fid == b"TIT2":
            title = _text_frame(body)
        elif fid == b"TPE1":
            artist = _text_frame(body)
        pos += 10 + n
        if title and artist:
            break
    return title, artist


async def store_stream(party_id: str, name: str, chunks) -> Stored:
    """Write the async iterator ``chunks`` to a new file of the party.
    Raises ``FileError`` (415 / 413 / 400); a refused file leaves nothing."""
    if not _PARTY.fullmatch(party_id):
        raise FileError(404, "not_found")
    ext = extension_of(name)
    if ext is None:
        raise FileError(415, "unsupported_type")
    folder = _uploads() / party_id
    folder.mkdir(parents=True, exist_ok=True)
    used = _folder_bytes(folder)
    if used >= PARTY_MAX_BYTES:
        _rmdir_if_empty(folder)
        raise FileError(413, "party_full")
    if _free(folder) < MIN_FREE_BYTES:
        _rmdir_if_empty(folder)
        raise FileError(507, "disk_full")
    key = secrets.token_urlsafe(16)
    path = folder / f"{key}.{ext}"
    size, head, checked = 0, b"", False
    try:
        with open(path, "wb") as out:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FileError(413, "too_large")
                if used + size > PARTY_MAX_BYTES:
                    raise FileError(413, "party_full")
                if _free(folder) - size < MIN_FREE_BYTES:
                    raise FileError(507, "disk_full")
                if not checked:
                    head += chunk
                    if len(head) >= 12:
                        checked = True
                        if not magic_ok(ext, head):
                            raise FileError(415, "unsupported_type")
                out.write(chunk)
        if size == 0:
            raise FileError(400, "empty")
        if not checked and not magic_ok(ext, head):
            raise FileError(415, "unsupported_type")
    except BaseException:
        path.unlink(missing_ok=True)
        _rmdir_if_empty(folder)
        raise
    title, artist = title_artist(path)
    stem = os.path.splitext(os.path.basename(name or ""))[0].strip() or "Track"
    title, artist = _clean(title or stem)[:200], _clean(artist)[:200]
    return Stored(key, ext, path, size, title or "Track", artist)


def _clean(text: str) -> str:
    """``text`` without the control characters XML cannot carry."""
    return _XML_BAD.sub("", text).strip()


def _folder_bytes(folder: Path) -> int:
    total = 0
    for f in folder.iterdir():
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _free(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def _rmdir_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def discard(stored: "Stored") -> None:
    """Delete a stored file, and its party's folder if that is now empty."""
    stored.path.unlink(missing_ok=True)
    _rmdir_if_empty(stored.path.parent)


def _find(key: str, ext: str) -> Path | None:
    if not _KEY.fullmatch(key) or ext not in TYPES:
        return None
    root = _uploads()
    for folder in root.iterdir():
        candidate = folder / f"{key}.{ext}"
        if folder.is_dir() and candidate.is_file():
            return candidate
    return None


def delete_party(party_id: str) -> None:
    if _PARTY.fullmatch(party_id):
        shutil.rmtree(_uploads() / party_id, ignore_errors=True)


def cleanup(live_party_ids) -> int:
    """Delete the uploads of every party not in ``live_party_ids`` (a restart
    after a party ended). Returns how many party folders were removed."""
    live = set(live_party_ids)
    removed = 0
    for folder in _uploads().iterdir():
        if folder.name not in live:
            shutil.rmtree(folder, ignore_errors=True) if folder.is_dir() else folder.unlink(missing_ok=True)
            removed += 1
    return removed


def _x(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def file_uri_path(stored: Stored) -> str:
    return f"/api/home/files/{stored.key}.{stored.ext}"


def didl_for(stored: Stored, uri: str) -> str:
    """The DIDL-Lite a speaker gets with a dropped file."""
    creator = f"<dc:creator>{_x(stored.artist)}</dc:creator>" if stored.artist else ""
    return (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        f'<item id="file:{_x(stored.key)}" parentID="files" restricted="true">'
        f'<res protocolInfo="http-get:*:{TYPES[stored.ext]}:*">{_x(uri)}</res>'
        f"<dc:title>{_x(stored.title)}</dc:title>{creator}"
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        "</item></DIDL-Lite>"
    )


def _ranges(header: str | None, size: int):
    """``(start, end)`` inclusive, ``None`` for the whole file, ``False`` when
    unsatisfiable. Only ``bytes=a-b`` / ``a-`` / ``-n`` is understood; any
    other form is ignored."""
    if header is None:
        return None
    m = _RANGE.fullmatch(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.groups()
    if a == "":
        n = int(b)
        if n == 0:
            return False
        return max(0, size - n), size - 1
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    if start >= size or end < start:
        return False
    return start, end


@router.api_route("/api/home/files/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve(name: str, request: Request) -> Response:
    """A dropped file, for a speaker (no session needed; the key is the secret)."""
    key, _, ext = name.rpartition(".")
    path = _find(key, ext)
    if path is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        size = path.stat().st_size
    except OSError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    mime = TYPES[ext]
    rng = _ranges(request.headers.get("range"), size)
    if rng is False:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}",
                                                  "Accept-Ranges": "bytes"}, media_type=mime)
    start, end = rng if rng else (0, size - 1)
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status = 206 if rng else 200
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=mime)

    async def body():
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    data = f.read(min(_CHUNK, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data
        except OSError:
            return

    return StreamingResponse(body(), status_code=status, media_type=mime, headers=headers)
