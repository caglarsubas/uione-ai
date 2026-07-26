"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from uione import __version__
from uione.api import deps
from uione.api.routes import assistant, auth, health
from uione.config import get_settings
from uione.web import STATIC_DIR

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    services = await deps.startup()
    log.info(
        "uione.startup",
        version=__version__,
        environment=settings.environment,
        model_plane=settings.model_plane_url,
        connectors=sorted(services.gateway.server_health()),
        tools=len(services.gateway.catalog),
    )
    yield
    await deps.shutdown()
    log.info("uione.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="UiOne AI",
        version=__version__,
        description="On-premise, open-weight, MCP-native enterprise assistant platform.",
        lifespan=lifespan,
    )
    app.include_router(health.router, tags=["health"])
    app.include_router(assistant.router, tags=["assistant"])
    app.include_router(auth.router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/ui/")

    # Mounted last so the API routes above always win a path collision, and with
    # html=True so /ui/ serves index.html rather than 404ing on an empty path.
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    return app


app = create_app()
