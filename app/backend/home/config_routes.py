"""What the page needs to know about this install: ``GET /api/home/config``.

Today that is the shared remote browser (a neko container the household runs
beside Sonora, see docs/remote-browser.md). Users get the full-control URL,
guests the watch-only one, or nothing when none is set.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request

from .auth import request_principal

router = APIRouter()


def _env(name: str) -> str | None:
    return (os.environ.get(name) or "").strip() or None


@router.get("/api/home/config")
async def home_config(request: Request) -> dict:
    principal = request_principal(request)
    # Fail closed: only a household member gets the full-control link.
    url = None
    if principal.kind == "user":
        url = _env("SONORA_NEKO_URL")
    elif principal.kind == "guest":
        url = _env("SONORA_NEKO_VIEW_URL")
    from . import analysis  # lazy: numpy-heavy
    return {"remote_browser": {"url": url} if url else None, "visuals": analysis.visuals_mode()}
