"""Plug Sonora Home into Sonora's app: one call from ``backend/main.py``.

Kept here rather than in ``main.py`` so upstream merges stay clean. Later
tasks add their routers below.
"""

from __future__ import annotations

import inspect

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import Response

from . import auth_routes, config_routes, display_routes, ha, permissions, routes, tv_routes

#: Pages of Sonora Home's own that the interface routes by path: they are the
#: same page shell Sonora serves at ``/``.
pages = APIRouter()


async def _page_shell(request: Request) -> Response:
    """Whatever ``main.py`` answers at ``/`` (the built page, tagged for Home
    Assistant ingress), looked up at request time so this module never imports
    ``main``."""
    for route in request.app.router.routes:
        if (getattr(route, "path", None) == "/" and "GET" in (getattr(route, "methods", None) or ())
                and route.endpoint is not _page_shell):
            params = inspect.signature(route.endpoint).parameters
            return await (route.endpoint(request) if params else route.endpoint())
    raise HTTPException(404, "not found")


@pages.get("/join", include_in_schema=False)
async def join_page(request: Request) -> Response:
    return await _page_shell(request)


@pages.get("/tv", include_in_schema=False)
async def tv_page(request: Request) -> Response:
    return await _page_shell(request)


@pages.get("/party", include_in_schema=False)
async def party_page(request: Request) -> Response:
    return await _page_shell(request)


@pages.get("/party/{rest:path}", include_in_schema=False)
async def party_subpage(request: Request, rest: str) -> Response:
    return await _page_shell(request)


def install(app: FastAPI) -> None:
    """Register the permission guard and Sonora Home's routes.

    Call it right before ``origin.guard`` is registered. Starlette runs the
    last registered middleware first, so the origin guard (host and
    DNS-rebinding refusal) sees a request before this guard does; both must
    pass, and the principal is on ``request.state`` before any route runs (and
    before ``routes.attribution``, which credits queue additions to it). Its routers are included before
    any of ``main.py``'s routes, so ``/join`` wins over ``/{filename}``.
    """
    # Registered before the guard so it runs after it, with the principal known.
    app.middleware("http")(routes.attribution)
    app.middleware("http")(permissions.guard)
    app.include_router(auth_routes.router)
    app.include_router(routes.router)
    app.include_router(ha.router)
    app.include_router(config_routes.router)
    app.include_router(tv_routes.router)
    app.include_router(display_routes.router)
    app.include_router(pages)
    from . import media  # YouTube relay for speakers (Phase 2)
    app.include_router(media.router)
