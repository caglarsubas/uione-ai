"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from uione import __version__
from uione.api.routes import health
from uione.config import get_settings

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info(
        "uione.startup",
        version=__version__,
        environment=settings.environment,
        model_plane=settings.model_plane_url,
    )
    yield
    log.info("uione.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="UiOne AI",
        version=__version__,
        description="On-premise, open-weight, MCP-native enterprise assistant platform.",
        lifespan=lifespan,
    )
    app.include_router(health.router, tags=["health"])
    return app


app = create_app()
