"""Scream receiver: a Windows PC's "network sound card".

The Scream driver sends what Windows plays as UDP datagrams (multicast
``239.255.77.77:4010``, or unicast to this port). Each datagram is a 5-byte
header and raw PCM:

* byte 0: sample rate code; bit 7 set = 44100 base, clear = 48000 base, the
  low 7 bits a multiplier
* byte 1: bits per sample (16, 24 or 32)
* byte 2: channels
* bytes 3-4: channel mask (little endian), not needed here

Sources are keyed by sender IP. Audio from a source is played only when that
IP is mapped to a party or a zone (``scream.json``); otherwise it is ignored,
though the source still shows in the list so it can be mapped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import time
from collections.abc import Callable
from typing import Any

from .store import home_dir, read_json, write_json

log = logging.getLogger(__name__)

GROUP = "239.255.77.77"
DEFAULT_PORT = 4010
HEADER = 5
_FORMATS = {16: "s16le", 24: "s24le", 32: "s32le"}
_MAX_SOURCES = 64
_QUEUE_PACKETS = 200      # about a second of audio per source; older is dropped
_RETRY_SECONDS = 3.0      # between tries to start a session that was refused
_FEEDER_IDLE = 30.0       # a source's worker leaves after this long without packets

InputFormat = tuple[str, int, int]


def parse(data: bytes) -> tuple[InputFormat, bytes] | None:
    """``((fmt, rate, channels), pcm)`` for a Scream datagram, or None when it
    is not one (too short, unknown width, no channels, bad rate)."""
    if len(data) <= HEADER:
        return None
    code, bits, channels = data[0], data[1], data[2]
    fmt = _FORMATS.get(bits)
    multiplier = code & 0x7F
    if fmt is None or channels == 0 or multiplier == 0:
        return None
    rate = (44100 if code & 0x80 else 48000) * multiplier
    frame = channels * bits // 8
    pcm = data[HEADER:]
    pcm = pcm[: len(pcm) - len(pcm) % frame]
    if not pcm:
        return None
    return (fmt, rate, channels), pcm


def valid_ip(text: Any) -> str | None:
    """The normalised IPv4 address in ``text``, or None."""
    try:
        ip = ipaddress.ip_address(str(text))
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


def valid_target(value: Any) -> dict[str, str] | None:
    """``{"kind", "id"}`` when ``value`` is a party or zone target."""
    if not isinstance(value, dict):
        return None
    kind, ident = value.get("kind"), value.get("id")
    if kind not in ("party", "zone") or not isinstance(ident, str):
        return None
    ident = ident.strip()
    if not ident or len(ident) > 100:
        return None
    return {"kind": kind, "id": ident}


class _Source:
    def __init__(self) -> None:
        self.last_seen = 0.0
        self.format: InputFormat | None = None
        self.queue: asyncio.Queue[tuple[InputFormat, bytes]] = asyncio.Queue(_QUEUE_PACKETS)
        self.task: asyncio.Task | None = None
        self.session: Any = None
        self.session_key: tuple | None = None   # (format, target) the session was started for
        self.retry_at = 0.0


class ScreamReceiver(asyncio.DatagramProtocol):
    """Datagrams in, live sessions out. ``hub`` is a function giving the live
    hub (it may raise while the controller is not up)."""

    def __init__(self, hub: Callable[[], Any], path_fn: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._hub, self._clock, self._mono = hub, clock, mono
        self._path_fn = path_fn or (lambda: home_dir() / "scream.json")
        self.sources: dict[str, _Source] = {}
        self._map: dict[str, dict[str, str]] | None = None
        self.transport: asyncio.DatagramTransport | None = None

    # -- mapping -------------------------------------------------------------

    def mapping(self) -> dict[str, dict[str, str]]:
        if self._map is None:
            raw = read_json(self._path_fn(), {})
            self._map = {}
            if isinstance(raw, dict):
                for ip, target in raw.items():
                    if valid_ip(ip) == ip and (t := valid_target(target)):
                        self._map[ip] = t
        return self._map

    def set_target(self, ip: str, target: dict[str, str] | None) -> None:
        mapping = dict(self.mapping())
        if target is None:
            mapping.pop(ip, None)
        else:
            mapping[ip] = target
        write_json(self._path_fn(), mapping)
        self._map = mapping
        source = self.sources.get(ip)
        if source is not None:
            source.session_key = None     # the next packet starts on the new target

    def listing(self) -> list[dict[str, Any]]:
        rows = []
        for ip in sorted(set(self.sources) | set(self.mapping()), key=lambda i: ipaddress.ip_address(i)):
            source = self.sources.get(ip)
            fmt = source.format if source else None
            rows.append({
                "ip": ip,
                "last_seen": source.last_seen if source and source.last_seen else None,
                "format": {"format": fmt[0], "rate": fmt[1], "channels": fmt[2]} if fmt else None,
                "target": self.mapping().get(ip),
            })
        return rows

    # -- datagrams -----------------------------------------------------------

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        parsed = parse(data)
        ip = valid_ip(addr[0]) if addr else None
        if parsed is None or ip is None:
            return
        fmt, pcm = parsed
        source = self.sources.get(ip)
        if source is None:
            if len(self.sources) >= _MAX_SOURCES:
                return
            source = self.sources[ip] = _Source()
        source.last_seen, source.format = self._clock(), fmt
        if ip not in self.mapping():
            return                       # heard, listed, not played
        if source.queue.full():
            source.queue.get_nowait()    # a stalled session must not grow memory
        source.queue.put_nowait((fmt, pcm))
        if source.task is None or source.task.done():
            source.task = asyncio.ensure_future(self._work(ip, source))

    async def _work(self, ip: str, source: _Source) -> None:
        try:
            while True:
                try:
                    fmt, pcm = await asyncio.wait_for(source.queue.get(), _FEEDER_IDLE)
                except asyncio.TimeoutError:
                    return
                try:
                    await self._deliver(ip, source, fmt, pcm)
                except Exception:
                    log.exception("scream: delivering audio from %s failed", ip)
        finally:
            source.task = None

    async def _deliver(self, ip: str, source: _Source, fmt: InputFormat, pcm: bytes) -> None:
        target = self.mapping().get(ip)
        if target is None:
            return
        key = (fmt, target["kind"], target["id"])
        session = source.session
        if session is not None and (session.ended or source.session_key != key):
            if not session.ended:
                await session.end("source_changed")
            session = source.session = None
        if session is None:
            # A session that ended on silence is not restarted by more silence:
            # that would cut the queue back in just to stop it again.
            if not any(pcm) or self._mono() < source.retry_at:
                return
            try:
                session = await self._hub().start(
                    target=(target["kind"], target["id"]),
                    source_name=f"Windows ({ip})", by="scream", input_format=fmt)
            except Exception as exc:
                source.retry_at = self._mono() + _RETRY_SECONDS
                log.info("scream: %s not played on %s %s: %s", ip, target["kind"], target["id"], exc)
                return
            source.session, source.session_key = session, key
        await session.feed(pcm)

    async def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
        for source in self.sources.values():
            if source.task is not None:
                source.task.cancel()
            if source.session is not None and not source.session.ended:
                try:
                    await source.session.end("shutdown")
                except Exception:
                    log.exception("scream: ending a session failed")


# -- the socket ----------------------------------------------------------------

_receiver: ScreamReceiver | None = None


def receiver() -> ScreamReceiver:
    """The one receiver (made on first use; routes read its list and mapping)."""
    global _receiver
    if _receiver is None:
        from . import live_routes
        _receiver = ScreamReceiver(live_routes.hub)
    return _receiver


def configured_port() -> int:
    try:
        return int(os.environ.get("SONORA_SCREAM_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def open_socket(port: int) -> socket.socket:
    """A non-blocking UDP socket on ``0.0.0.0:port`` that has joined the Scream
    multicast group (unicast to the same port works as well). Joining the group
    is best effort: a host without a multicast route keeps unicast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton("0.0.0.0")))
        except OSError as exc:
            log.warning("scream: could not join %s (%s); unicast only", GROUP, exc)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


async def start(port: int | None = None, proto: ScreamReceiver | None = None) -> ScreamReceiver | None:
    """Listen for Scream audio. Returns None (with a log line) when it is
    switched off (port 0) or the port cannot be bound."""
    port = configured_port() if port is None else port
    if port == 0:
        log.info("scream: receiver disabled (SONORA_SCREAM_PORT=0)")
        return None
    try:
        sock = open_socket(port)
    except OSError as exc:
        log.warning("scream: receiver disabled, UDP port %s cannot be bound: %s", port, exc)
        return None
    proto = proto or receiver()
    try:
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: proto, sock=sock)
    except OSError as exc:
        sock.close()
        log.warning("scream: receiver disabled: %s", exc)
        return None
    log.info("scream: listening on UDP %s (multicast %s)", port, GROUP)
    return proto


async def stop() -> None:
    global _receiver
    if _receiver is not None:
        await _receiver.close()
        _receiver = None
