"""GET /api/home/config: the remote browser URL by who is asking."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from backend.home import config_routes
from backend.home.auth import Principal

from home_client import Client


def app_as(principal):
    app = FastAPI()

    @app.middleware("http")
    async def _who(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(config_routes.router)
    return app


def get(principal):
    return asyncio.run(Client(app_as(principal)).request("GET", "/api/home/config"))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("SONORA_NEKO_URL", raising=False)
    monkeypatch.delenv("SONORA_NEKO_VIEW_URL", raising=False)


def body(response):
    return response.json() if callable(getattr(response, "json", None)) else response[1]


def test_nothing_configured_is_null():
    for p in (Principal("user", "u", "Ana"), Principal("guest", "g", "Bo")):
        assert body(get(p)) == {"remote_browser": None}


def test_user_gets_full_control_url_guest_gets_view_url(monkeypatch):
    monkeypatch.setenv("SONORA_NEKO_URL", "https://neko.test/?pwd=admin")
    monkeypatch.setenv("SONORA_NEKO_VIEW_URL", "https://neko.test/?pwd=view")
    assert body(get(Principal("user", "u", "Ana"))) == {"remote_browser": {"url": "https://neko.test/?pwd=admin"}}
    assert body(get(Principal("guest", "g", "Bo"))) == {"remote_browser": {"url": "https://neko.test/?pwd=view"}}


def test_guest_without_view_url_gets_null(monkeypatch):
    monkeypatch.setenv("SONORA_NEKO_URL", "https://neko.test/?pwd=admin")
    assert body(get(Principal("guest", "g", "Bo"))) == {"remote_browser": None}


def test_service_principal_gets_null(monkeypatch):
    monkeypatch.setenv("SONORA_NEKO_URL", "https://neko.test/?pwd=admin")
    monkeypatch.setenv("SONORA_NEKO_VIEW_URL", "https://neko.test/?pwd=view")
    assert body(get(Principal("service", "ha", "Home Assistant"))) == {"remote_browser": None}
