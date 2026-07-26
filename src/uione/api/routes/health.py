"""Liveness and readiness.

Readiness is deliberately honest: if the model plane is unreachable the service
reports degraded rather than healthy. Silently serving a broken assistant is the
failure mode that destroys user trust (gap G8).
"""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from uione import __version__
from uione.config import get_settings

router = APIRouter()


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    version: str
    model_plane: Literal["reachable", "unreachable"]
    detail: str | None = None


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Process liveness. Never depends on downstreams."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=ReadinessResponse)
async def ready(response: Response) -> ReadinessResponse:
    settings = get_settings()
    timeout = httpx.Timeout(
        settings.model_plane_timeout_s,
        connect=settings.model_plane_connect_timeout_s,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{settings.model_plane_url}/models")
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="degraded",
            version=__version__,
            model_plane="unreachable",
            detail=type(exc).__name__,
        )
    return ReadinessResponse(
        status="ready",
        version=__version__,
        model_plane="reachable",
    )
