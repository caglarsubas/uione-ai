"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from uione import __version__
from uione.api import deps
from uione.api.routes import assistant, health
from uione.config import get_settings

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
    return app


app = create_app()
