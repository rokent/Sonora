"""A bare ASGI client for the Sonora Home tests (httpx is not installed, so
FastAPI's TestClient is not available). Keeps cookies between requests and
can open a WebSocket far enough to see whether it was accepted or closed."""

from __future__ import annotations

import json


class Client:
    """Send requests straight to an ASGI app, keeping the cookies it sets."""

    def __init__(self, app, scheme="http", host="sonora.test"):
        self.app, self.scheme, self.host, self.cookies = app, scheme, host, {}

    def _headers(self, headers, extra=()):
        hdrs = [(b"host", self.host.encode()), *extra]
        hdrs += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
        if self.cookies:
            jar = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            hdrs.append((b"cookie", jar.encode()))
        return hdrs

    async def request(self, method, path, body=None, headers=None, raw=None):
        """``raw`` (bytes, or a list of chunks) replaces the JSON body."""
        path, _, query = path.partition("?")
        chunks = ([raw] if isinstance(raw, bytes) else list(raw)) if raw is not None else None
        payload = (b"".join(chunks) if chunks is not None
                   else json.dumps(body).encode() if body is not None else b"")
        hdrs = self._headers(headers, [(b"content-type", b"application/json")])
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": self.scheme, "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": hdrs, "server": (self.host, 80), "client": ("1.2.3.4", 1)}
        if chunks is None:
            sent = [{"type": "http.request", "body": payload, "more_body": False}]
        else:
            sent = [{"type": "http.request", "body": c, "more_body": True} for c in chunks]
            sent.append({"type": "http.request", "body": b"", "more_body": False})
        out = {"status": 0, "headers": [], "body": b""}

        async def receive():
            return sent.pop(0) if sent else {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"], out["headers"] = msg["status"], msg["headers"]
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        await self.app(scope, receive, send)
        for k, v in out["headers"]:
            if k == b"set-cookie":
                name, _, rest = v.decode().partition("=")
                value = rest.split(";", 1)[0]
                if value in ("", '""') or "Max-Age=0" in rest:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = value
        return Response(out)

    async def websocket(self, path, headers=None):
        """Open a socket and hang up right after the handshake. Returns the
        messages the app sent: ``websocket.accept`` first if it let us in,
        ``websocket.close`` (with its ``code``) if it refused."""
        path, _, query = path.partition("?")
        scope = {"type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "scheme": "ws" if self.scheme == "http" else "wss", "path": path,
                 "raw_path": path.encode(), "query_string": query.encode(),
                 "headers": self._headers(headers), "server": (self.host, 80),
                 "client": ("1.2.3.4", 1), "subprotocols": []}
        incoming = [{"type": "websocket.connect"}]
        sent: list[dict] = []

        async def receive():
            return incoming.pop(0) if incoming else {"type": "websocket.disconnect",
                                                     "code": 1000}

        async def send(msg):
            sent.append(msg)

        await self.app(scope, receive, send)
        return sent


class Response:
    def __init__(self, out):
        self.status = out["status"]
        self.headers = {k.decode(): v.decode() for k, v in out["headers"]}
        self.set_cookies = [v.decode() for k, v in out["headers"] if k == b"set-cookie"]
        self.body = out["body"]

    def json(self):
        return json.loads(self.body)
