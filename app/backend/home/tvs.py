"""Samsung TVs: the household's list, pairing, and opening TV mode on one.

A Samsung (Tizen) TV takes remote-control commands on a local websocket:
``wss://<host>:8002/api/v2/channels/samsung.remote.control?name=<base64>``
with a self-signed certificate (so TLS is not verified), and on older sets
``ws://<host>:8001/...``. The first connect from a new name puts an allow
prompt on the TV; once accepted, the ``ms.channel.connect`` event carries a
token that later connects send, so the prompt does not come back.

Opening TV mode is launching the TV's browser at a URL. Every TV call is
bounded (``config.timeout``) and fails as ``tv_unreachable`` (nothing
answered) or ``tv_needs_pairing`` (the TV answered but did not let us in).
A TV with a MAC is sent a Wake-on-LAN packet first, and again before each
retry, for ``config.wake_window`` seconds. Uses aiohttp and a UDP socket only.

Which browser to launch depends on the model: older Tizen sets have
``org.tizen.browser``; many 2022+ sets have only Samsung Internet
(``3201907018807``). Unless the TV record names one (``browser_app``), the
TV's REST API is asked whether Samsung Internet is installed, and the answer
is kept on the record.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from ..config import settings
from ..sonos import events
from .presets import TV_SHOWS
from .store import home_dir, read_json, write_json

APP_NAME = "Sonora Home"
CHANNEL = "/api/v2/channels/samsung.remote.control"
BROWSER = "org.tizen.browser"
SAMSUNG_INTERNET = "3201907018807"
_APP_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
NAME_LIMIT = 60
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_MAC = re.compile(r"[0-9a-f]{12}")


@dataclass
class Config:
    """Timings and ports, kept apart so tests can shorten and redirect them."""
    timeout: float = 5.0          # opening the socket, and the TV's answer
    pair_wait: float = 30.0       # someone walking to the TV to press Allow
    wake_window: float = 15.0     # after Wake-on-LAN, keep trying this long
    retry_every: float = 1.0
    secure: tuple[str, int] = ("wss", 8002)
    plain: tuple[str, int] = ("ws", 8001)
    rest: tuple[str, int] = ("http", 8001)   # the TV's app-info REST API


config = Config()


class TvError(Exception):
    def __init__(self, status: int, error: str, message: str = "") -> None:
        super().__init__(error)
        self.status, self.error, self.message = status, error, message

    @property
    def body(self) -> dict:
        return {"error": self.error, **({"message": self.message} if self.message else {})}


def _unreachable() -> TvError:
    return TvError(504, "tv_unreachable")


def _needs_pairing() -> TvError:
    return TvError(409, "tv_needs_pairing")


# -- the list -----------------------------------------------------------------


def _mac(value) -> str | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"[:\-.\s]", "", str(value)).lower()
    if not _MAC.fullmatch(digits):
        raise ValueError("mac must be six hex bytes, like aa:bb:cc:dd:ee:ff")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _normalize(body: dict, old: dict | None = None) -> dict:
    if not isinstance(body, dict):
        raise ValueError("not a tv")
    old = old or {}
    name = str(body.get("name", old.get("name")) or "").strip()[:NAME_LIMIT]
    if not name:
        raise ValueError("a name is needed")
    host = str(body.get("host", old.get("host")) or "").strip()
    if not _HOST.fullmatch(host):
        raise ValueError("host must be an IP address or a host name")
    mac = _mac(body["mac"]) if "mac" in body else old.get("mac")
    mode = body.get("mode_default")
    if mode is None:                        # missing or null: keep the old one
        mode = old.get("mode_default") or "cover"
    if mode not in TV_SHOWS:
        raise ValueError("mode_default must be video, visualizer or cover")
    # null or "" clears it, so the next show asks the TV again.
    app = body["browser_app"] if "browser_app" in body else old.get("browser_app")
    app = str(app).strip() if app else None
    if app is not None and not _APP_ID.fullmatch(app):
        raise ValueError("browser_app must be an app id")
    return {"name": name, "host": host, "mac": mac, "mode_default": mode, "browser_app": app}


def public(tv: dict) -> dict:
    """A TV as the interface sees it: never the token."""
    return {"id": tv["id"], "name": tv["name"], "host": tv["host"], "mac": tv.get("mac"),
            "mode_default": tv.get("mode_default", "cover"), "browser_app": tv.get("browser_app"),
            "paired": bool(tv.get("token"))}


class Tvs:
    """``home/tvs.json``: ``{"tvs": [{id, name, host, mac, token, mode_default}]}``."""

    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        found = read_json(self.path, {}).get("tvs")
        return [t for t in found if isinstance(t, dict) and t.get("id")] if isinstance(found, list) else []

    def list(self) -> list[dict]:
        return self._load()

    def get(self, tv_id: str) -> dict | None:
        return next((t for t in self._load() if t["id"] == tv_id), None)

    def add(self, body: dict) -> dict:
        tv = {"id": "tv_" + secrets.token_hex(6), **_normalize(body), "token": None,
              "created": time.time()}
        with self._lock:
            write_json(self.path, {"tvs": [*self._load(), tv]})
        return tv

    def update(self, tv_id: str, body: dict) -> dict | None:
        with self._lock:
            items = self._load()
            for i, old in enumerate(items):
                if old["id"] == tv_id:
                    items[i] = {**old, **_normalize(body, old)}
                    write_json(self.path, {"tvs": items})
                    return items[i]
        return None

    def remove(self, tv_id: str) -> bool:
        with self._lock:
            items = self._load()
            kept = [t for t in items if t["id"] != tv_id]
            if len(kept) == len(items):
                return False
            write_json(self.path, {"tvs": kept})
        return True

    def set_fields(self, tv_id: str, **fields) -> None:
        with self._lock:
            items = self._load()
            for t in items:
                if t["id"] == tv_id:
                    t.update(fields)
            write_json(self.path, {"tvs": items})

    def set_token(self, tv_id: str, token: str) -> None:
        self.set_fields(tv_id, token=token)


_store: Tvs | None = None


def store() -> Tvs:
    """The list in the current data dir (a new one if the dir changes)."""
    global _store
    path = home_dir() / "tvs.json"
    if _store is None or _store.path != path:
        _store = Tvs(path)
    return _store


# -- what the TV opens --------------------------------------------------------


async def lan_base(host: str) -> str:
    """Sonora's address as a TV on the LAN reaches it: the address speakers
    already use (the event callback), else the route toward ``host``; the
    same choice ``media.media_url`` makes for speakers."""
    from .. import main  # lazy: main imports home

    controller = main.state.get("controller")
    ip = getattr(getattr(controller, "events", None), "_callback_ip", None)
    if not ip:
        try:
            ip = await asyncio.to_thread(events.local_ip_for, host)
        except OSError:
            raise _unreachable() from None
    return f"http://{ip}:{settings.port}"


def display_url_for(tv: dict, party_id: str, mode: str, base_url: str) -> str:
    """The page a TV opens for ``party_id`` in ``mode``: the TV's own display
    link. It is made on the first show (or when the link was deleted), kept on
    the TV record with its URL (the URL carries the token, so the record is
    never returned whole), and pointed at the party and mode on every show."""
    from .auth import DisplayLinks  # lazy: auth is heavier than this module

    links = DisplayLinks()
    link = links.get(tv["display_id"]) if tv.get("display_id") else None
    stored = tv.get("display_url") or ""
    if link is None or "/auth/display" not in stored:
        link, token = links.create(f"TV: {tv['name']}"[:40], party=party_id, mode=mode)
        stored = f"{base_url}/auth/display?{urlencode({'token': token})}"
        store().set_fields(tv["id"], display_id=link["id"], display_url=stored)
        return stored
    if link["party"] != party_id or link["mode"] != mode:
        links.update(link["id"], party=party_id, mode=mode)
    tail = stored[stored.index("/auth/display"):]     # the address the TV reaches may change
    url = f"{base_url}{tail}"
    if url != stored:
        store().set_fields(tv["id"], display_url=url)
    return url


def launch_message(url: str, app: str = BROWSER) -> dict:
    # NATIVE_LAUNCH with the URL as metaTag is what opens org.tizen.browser.
    # Samsung Internet gets the same message; some firmwares may want
    # action_type DEEP_LINK for it instead (unverified on real sets).
    return {"method": "ms.channel.emit", "params": {
        "event": "ed.apps.launch", "to": "host",
        "data": {"appId": app, "action_type": "NATIVE_LAUNCH", "metaTag": url}}}


async def browser_app(session: aiohttp.ClientSession, tv: dict) -> str:
    """The browser to launch: the record's ``browser_app``, else Samsung
    Internet when the TV's REST API says it is installed (200), else
    ``org.tizen.browser``. A definite answer is kept on the record; a TV
    that does not answer gets the old browser this time and is asked again
    next time."""
    if tv.get("browser_app"):
        return tv["browser_app"]
    scheme, port = config.rest
    url = f"{scheme}://{tv['host']}:{port}/api/v2/applications/{SAMSUNG_INTERNET}"
    try:
        async with asyncio.timeout(config.timeout):
            async with session.get(url, ssl=False) as response:
                found = SAMSUNG_INTERNET if response.status == 200 else BROWSER
    except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
        return BROWSER
    store().set_fields(tv["id"], browser_app=found)
    return found


# -- Wake-on-LAN --------------------------------------------------------------


def magic_packet(mac: str) -> bytes:
    """Six 0xFF bytes, then the MAC sixteen times."""
    raw = bytes.fromhex(_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def _send_udp(packet: bytes, addr: tuple[str, int]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, addr)
    finally:
        sock.close()


def wake(mac: str, host: str) -> None:
    """Broadcast the magic packet everywhere and on the TV's /24. A failed
    send is ignored: the connect that follows tells whether it worked."""
    packet = magic_packet(mac)
    targets = [("255.255.255.255", 9)]
    try:
        net = ipaddress.ip_network(f"{ipaddress.IPv4Address(host)}/24", strict=False)
        targets.append((str(net.broadcast_address), 9))
    except ValueError:
        pass
    for addr in targets:
        try:
            _send_udp(packet, addr)
        except OSError:
            pass


# -- the remote-control socket ------------------------------------------------


def _urls(tv: dict) -> list[str]:
    name = base64.b64encode(APP_NAME.encode()).decode()
    (s_scheme, s_port), (p_scheme, p_port) = config.secure, config.plain
    secure = f"{s_scheme}://{tv['host']}:{s_port}{CHANNEL}?name={name}"
    if tv.get("token"):
        secure += f"&token={tv['token']}"
    return [secure, f"{p_scheme}://{tv['host']}:{p_port}{CHANNEL}?name={name}"]


async def _open(session: aiohttp.ClientSession, tv: dict, budget: float):
    """The first socket that opens within ``budget`` seconds: the secure port
    first, then the plain one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    for url in _urls(tv):
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            async with asyncio.timeout(left):
                return await session.ws_connect(URL(url, encoded=True), ssl=False,
                                                autoclose=True, autoping=True)
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            continue
    raise _unreachable()


async def _admitted(ws, wait: float) -> str | None:
    """Wait for ``ms.channel.connect``; its token, if it carried one. A
    refusal, a hang-up or silence (the allow prompt still up) is
    ``tv_needs_pairing``."""
    try:
        async with asyncio.timeout(wait):
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                event = data.get("event") if isinstance(data, dict) else None
                if event == "ms.channel.connect":
                    token = (data.get("data") or {}).get("token")
                    return str(token) if token else None
                if event in ("ms.channel.unauthorized", "ms.channel.timeOut"):
                    break
    except TimeoutError:
        pass
    raise _needs_pairing()


async def _try(session, tv: dict, wait: float):
    ws = await _open(session, tv, config.timeout)
    try:
        return ws, await _admitted(ws, wait)
    except BaseException:
        await ws.close()
        raise


@contextlib.asynccontextmanager
async def channel(tv: dict, wait: float) -> AsyncIterator:
    """An admitted socket to ``tv`` (waking it first when it has a MAC);
    yields ``(session, ws, token)``. A new token is stored before the body runs."""
    async with aiohttp.ClientSession() as session:
        if tv.get("mac"):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + config.wake_window
            while True:
                wake(tv["mac"], tv["host"])     # again each try: UDP may be lost
                try:
                    ws, token = await _try(session, tv, wait)
                    break
                except TvError as exc:
                    if exc.error != "tv_unreachable" or loop.time() + config.retry_every >= deadline:
                        raise
                await asyncio.sleep(config.retry_every)
        else:
            ws, token = await _try(session, tv, wait)
        try:
            if token and token != tv.get("token"):
                store().set_token(tv["id"], token)
            yield session, ws, token
        finally:
            await ws.close()


def _find(tv_id: str) -> dict:
    tv = store().get(tv_id)
    if tv is None:
        raise TvError(404, "not_found", tv_id)
    return tv


async def pair(tv_id: str) -> dict:
    """Connect, so the TV shows its allow prompt, and keep the token it
    gives. A TV on the plain port gives none and needs none."""
    tv = _find(tv_id)
    async with channel(tv, config.pair_wait) as (_, _, token):
        pass
    return {"ok": True, "paired": bool(token or tv.get("token"))}


def prepare(tv_id: str, mode: str | None) -> tuple[dict, str]:
    """The TV and the mode to show, or the instant refusals: ``not_found``,
    ``invalid``. Nothing touches the network."""
    tv = _find(tv_id)
    mode = mode or tv.get("mode_default") or "cover"
    if mode not in TV_SHOWS:
        raise TvError(400, "invalid", "mode must be video, visualizer or cover")
    return tv, mode


async def show(tv_id: str, party_id: str, mode: str | None = None) -> dict:
    """Open TV mode for ``party_id`` on the TV's browser."""
    tv, mode = prepare(tv_id, mode)
    url = display_url_for(tv, party_id, mode, await lan_base(tv["host"]))
    async with channel(tv, config.timeout) as (session, ws, _):
        app = await browser_app(session, tv)
        try:
            async with asyncio.timeout(config.timeout):
                await ws.send_str(json.dumps(launch_message(url, app)))
        except (aiohttp.ClientError, OSError, TimeoutError, ConnectionError):
            raise _unreachable() from None
    return {"ok": True, "tv_id": tv_id, "party_id": party_id, "mode": mode}
