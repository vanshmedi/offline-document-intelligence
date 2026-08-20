"""
FastAPI application.

Serves the JSON API under /api and the single-page console at /. The front end
is static files with no build step, so `uvicorn` is the only process involved --
there is no node toolchain, no bundler, and nothing to compile before a demo.

Run it:
    product-intel serve
    python -m product_intel.api          (equivalent)
    uvicorn product_intel.api.app:app --reload
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from product_intel.api.routes import router
from product_intel.api.state import STATE
from product_intel.config import WORKSPACE_DIR

log = logging.getLogger(__name__)

WEB_DIR = WORKSPACE_DIR / "web"

DESCRIPTION = """
Evidence-grounded product intelligence for industrial commerce.

Every attribute returned by this API carries either an `evidence` block naming
the source document, page and verbatim quote it was read from, or an
`inference` block explaining how it was derived. There is no third state.
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="Product Intelligence Engine",
        description=DESCRIPTION,
        version="2.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # The console is served from the same origin, so CORS is only needed for
    # someone pointing a separate front end at this API during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a stack trace to the browser, but always log it in full.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            """
            Serve static files, falling back to index.html.

            The client routes with the History API, so a deep link like
            /products/VX100-2P-C20 must return the shell rather than a 404.
            """
            candidate = (WEB_DIR / path).resolve()
            try:
                candidate.relative_to(WEB_DIR.resolve())  # no path traversal
            except ValueError:
                return FileResponse(WEB_DIR / "index.html")
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIR / "index.html")
    else:
        log.warning("web/ not found at %s; serving the API only", WEB_DIR)

    return app


app = create_app()
