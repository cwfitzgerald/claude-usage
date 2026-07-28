"""Starlette application for the local usage dashboard."""

from __future__ import annotations

import asyncio
import webbrowser
from contextlib import asynccontextmanager
from importlib.resources import files

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .views import (
    SORT_KEYS,
    SUBAGENT_SORT_KEYS,
    filter_sessions,
    session_detail,
    session_summary,
    sort_sessions,
    totals_view,
)
from .service import UsageService


def create_app(
    service: UsageService, *, open_browser_url: str | None = None
) -> Starlette:
    static = files("claude_usage").joinpath("static")

    async def open_browser() -> None:
        # Give the socket a brief moment to begin accepting connections after
        # lifespan startup returns to Uvicorn.
        await asyncio.sleep(0.25)
        await asyncio.to_thread(webbrowser.open, open_browser_url)

    @asynccontextmanager
    async def lifespan(_: Starlette):
        await service.start_refresh()
        if open_browser_url:
            asyncio.create_task(open_browser())
        yield

    # The assets are unversioned (no content hash in the URL), so without an
    # explicit directive a browser applies *heuristic* caching and can reuse a
    # stale copy without revalidating — silently serving a dashboard that predates
    # the last edit. "no-cache" means "revalidate before use", which fixes that.
    #
    # A bare FileResponse doesn't implement conditional responses (that lives in
    # StaticFiles), so each load re-sends the body rather than answering 304. At
    # ~32KB total over loopback that's not worth the machinery to avoid.
    _REVALIDATE = {"Cache-Control": "no-cache"}

    async def index(_: Request):
        return FileResponse(str(static.joinpath("index.html")), headers=_REVALIDATE)

    async def stylesheet(_: Request):
        return FileResponse(
            str(static.joinpath("app.css")),
            media_type="text/css",
            headers=_REVALIDATE,
        )

    async def script(_: Request):
        return FileResponse(
            str(static.joinpath("app.js")),
            media_type="text/javascript",
            headers=_REVALIDATE,
        )

    async def status(_: Request):
        return JSONResponse(service.status())

    async def refresh(_: Request):
        started = await service.start_refresh()
        return JSONResponse({**service.status(), "started": started}, status_code=202)

    async def sessions(request: Request):
        params = request.query_params
        sort = params.get("sort", "cost")
        order = params.get("order", "desc")
        if sort not in SORT_KEYS or order not in {"asc", "desc"}:
            return JSONResponse({"error": "invalid sort or order"}, status_code=400)
        try:
            selected = filter_sessions(
                service.snapshot.sessions,
                since=params.get("since"),
                tool=params.get("tool"),
                query=params.get("q"),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        selected = sort_sessions(selected, sort, order)
        return JSONResponse(
            {
                "meta": {
                    **service.status(),
                    "filtered_count": len(selected),
                    "totals": totals_view(selected),
                },
                "items": [session_summary(session) for session in selected],
            }
        )

    async def detail(request: Request):
        tool = request.path_params["tool"]
        session_id = request.path_params["session_id"]
        subagent_sort = request.query_params.get("subagent_sort", "cost")
        if subagent_sort not in SUBAGENT_SORT_KEYS:
            return JSONResponse({"error": "invalid subagent sort"}, status_code=400)
        session = service.snapshot.by_id.get((tool, session_id))
        if session is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return JSONResponse(session_detail(session, subagent_sort=subagent_sort))

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/", index),
            Route("/static/app.css", stylesheet),
            Route("/static/app.js", script),
            Route("/api/v1/status", status),
            Route("/api/v1/refresh", refresh, methods=["POST"]),
            Route("/api/v1/sessions", sessions),
            Route("/api/v1/sessions/{tool}/{session_id}", detail),
        ],
    )
