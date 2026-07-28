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

    #: Requests at the engine right now, and requests waiting for a slot.
    #:
    #: `queued` is the number worth an alert. It says the engine is the
    #: bottleneck, which is the one problem no amount of tuning elsewhere
    #: fixes — the answer is more or faster hardware, and an operator should
    #: learn that from a graph rather than from users complaining.
    in_flight: int = 0
    queued: int = 0


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Process liveness. Never depends on downstreams."""
    return HealthResponse(status="ok", version=__version__)


def _gate_depth() -> tuple[int, int]:
    """Queue depth, or zeros before startup has wired anything.

    Read defensively: readiness must answer even when the service is half
    constructed, because that is exactly when somebody is looking at it.
    """
    from uione.api.deps import _services

    if _services is None:
        return 0, 0
    gate = getattr(_services.model, "gate", None)
    return (gate.in_flight, gate.queued) if gate else (0, 0)


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
        in_flight, queued = _gate_depth()
        return ReadinessResponse(
            status="degraded",
            version=__version__,
            model_plane="unreachable",
            detail=type(exc).__name__,
            in_flight=in_flight,
            queued=queued,
        )

    in_flight, queued = _gate_depth()
    # A full queue is not "not ready". The service is working exactly as
    # designed and is simply saturated; returning 503 here would make a load
    # balancer pull a healthy instance out of rotation for being busy, which
    # sends its traffic to the others and saturates them too.
    return ReadinessResponse(
        status="ready",
        version=__version__,
        model_plane="reachable",
        in_flight=in_flight,
        queued=queued,
    )
