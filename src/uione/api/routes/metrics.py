"""The Prometheus scrape endpoint.

Separate from ``/system/health`` because they answer different questions to
different audiences: health is "should the load balancer send me traffic", and
this is "what has been happening". Health is therefore unauthenticated and this
is not.

**Disabled unless `UIONE_METRICS_TOKEN` is set, and 404 when disabled.** These
series say how many approvals a deployment is holding, how often its writes fail
to confirm, and how much GPU it is burning — an operational profile of the
organisation, not something to serve to whoever asks. 404 rather than 401 because
an unconfigured deployment should not advertise that the endpoint exists.

The token is compared with :func:`secrets.compare_digest`. A plain ``==`` on a
secret leaks its length and prefix to anyone patient enough to time the
responses, and this endpoint is reachable by definition.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from uione.api.deps import Services, get_services
from uione.config import Settings, get_settings

router = APIRouter()


def _authorise(request: Request, settings: Settings) -> None:
    configured = settings.metrics_token.strip()
    if not configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip(), configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="metrics require a bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/metrics", include_in_schema=False)
async def metrics(
    request: Request,
    services: Services = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> Response:
    _authorise(request, settings)

    registry = services.telemetry

    # Gauges are read at scrape time rather than pushed, so they cannot go stale
    # between a change and the next scrape.
    for server, state in services.gateway.server_health().items():
        if state == "unknown":
            # No series at all rather than a 1. This gauge shipped reading from
            # the circuit breaker, so it reported a *dead* connector as up for
            # the first four failures of an outage — and would have reported one
            # nobody had ever called as up forever.
            #
            # Absent is the honest encoding: `uione_connector_up == 0` should
            # page, and "never exercised" is not an outage. Prometheus has
            # `absent()` for asking the other question.
            continue
        registry.connector_up.set(1 if state == "ok" else 0, server=server)

    gate = getattr(services.model, "gate", None)
    if gate is not None:
        registry.model_plane_in_flight.set(getattr(gate, "in_flight", 0))
        registry.model_plane_queued.set(getattr(gate, "queued", 0))

    usage = services.model.usage
    registry.observe_usage(usage.by_model, usage.calls)

    pending = await services.governor.approvals.pending_count()
    registry.approvals_pending.set(pending)

    return Response(
        content=registry.render(),
        # The version is part of the contract: Prometheus content-negotiates on
        # it, and omitting it makes some scrapers fall back to a guess.
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
