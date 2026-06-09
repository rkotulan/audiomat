"""FastAPI app for audiomat — wires routers + serves the built frontend.

Heavy lifting lives in :mod:`audiomat.routers`. This file only owns:

* the FastAPI() instance + lifespan + CORS,
* mounting ``frontend/dist`` (or ``static/``) for the production SPA build,
* a SPA-fallback exception handler so React Router deep-links survive a hard
  refresh.

Run for local dev::

    uvicorn audiomat.api:app --reload --host 0.0.0.0 --port 8000

The Vite dev server proxies ``/api`` → ``:8000`` (see
``frontend/vite.config.ts``).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import asyncio

from audiomat.routers import (
    backup,
    chapters,
    models,
    preview,
    projects,
    render,
    settings,
    system,
    voices,
)
from audiomat.state import PATHS, clear_tts, idle_unload_loop


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Set up runtime dirs + idle-unload background task on startup;
    tear down resources on shutdown.

    Also runs the v0.2 → v0.3 SQLite migration if the library still has
    JSON state files lying around. The migration is idempotent and
    safe to call on a fresh install (zero JSON candidates → zero work).
    Wrapped in try/except so a migration failure logs loudly but
    doesn't block server startup — operators can recover via the CLI.
    """
    PATHS.ensure_dirs()

    try:
        from audiomat.migrations.v0_3_sqlite import migrate_v0_2_to_v0_3
        report = migrate_v0_2_to_v0_3(PATHS.library_root)
        if not report.empty:
            import logging as _log
            _log.getLogger("audiomat").info(
                "v0.2 → v0.3 migration: %s", report,
            )
    except Exception as e:  # noqa: BLE001
        import logging as _log
        _log.getLogger("audiomat").exception(
            "v0.2 → v0.3 migration failed (server starting anyway): %s", e,
        )

    idle_task = asyncio.create_task(idle_unload_loop(), name="audiomat-idle-unload")
    try:
        yield
    finally:
        idle_task.cancel()
        try:
            await idle_task
        except (asyncio.CancelledError, Exception):
            pass
        # On shutdown, drop the model to free GPU
        clear_tts()


app = FastAPI(
    title="audiomat",
    version="0.4.5",
    description="Convert eBooks into audiobooks with cloned voices.",
    lifespan=lifespan,
)


# Vite dev server proxy is the production path, but allow direct CORS
# from :5173 for cases where someone runs both servers without proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Wire routers. Order matters within a single APIRouter (literal paths
# must come before {slug} catchalls — see voices.py / preview.py), but
# inclusion order across routers does not.
app.include_router(system.router)
app.include_router(settings.router)
app.include_router(models.router)
app.include_router(voices.router)
app.include_router(projects.router)
app.include_router(preview.router)
app.include_router(chapters.router)
app.include_router(render.router)
app.include_router(backup.router)


# ----------------------------------------------------------------------------
# Static frontend mount
# ----------------------------------------------------------------------------

# Multi-stage Docker build copies frontend/dist → /app/static. In dev we
# don't mount static at all — Vite at :5173 serves it.
_STATIC_DIR = Path(__file__).parent.parent / "frontend" / "dist"
if not _STATIC_DIR.exists():
    _STATIC_DIR = Path(__file__).parent.parent / "static"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="frontend")


@app.exception_handler(StarletteHTTPException)
async def spa_404_fallback(request: Request, exc: StarletteHTTPException):
    """SPA deep-link fallback. Hard-refreshing a React Router path like
    ``/projects/Rezavy_les_v1`` hits the StaticFiles mount, which 404s
    because no such file exists in ``dist/``. We catch the 404 and serve
    ``index.html`` so React Router can handle the route client-side.
    ``/api/*`` paths still return JSON 404s normally."""
    if exc.status_code == 404 and not request.url.path.startswith("/api"):
        if _STATIC_DIR.exists():
            index = _STATIC_DIR / "index.html"
            if index.exists():
                return FileResponse(index)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )
