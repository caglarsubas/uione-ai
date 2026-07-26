"""Assistant surface: chat, the morning brief, and the approval queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from uione.api.deps import Services, get_principal, get_services
from uione.mcphub import Principal

router = APIRouter()


# -- models ----------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    max_steps: int = Field(default=6, ge=1, le=20)


class ToolCallView(BaseModel):
    tool: str | None
    arguments: dict[str, Any]
    ok: bool
    held: bool
    pending_action_id: str | None = None
    repairs: list[str] = Field(default_factory=list)
    detail: str | None = None


class ChatResponse(BaseModel):
    reply: str | None
    stop_reason: str
    tool_calls: list[ToolCallView]
    pending_approvals: list[str]
    untrusted_content_seen: bool
    notice: str | None = None


class SectionView(BaseModel):
    section: str
    heading: str
    source: str
    available: bool
    error: str | None = None
    duration_ms: float


class BriefResponse(BaseModel):
    body: str
    generated_at: datetime
    complete: bool
    """False when a source was unreachable. Surface this in the UI."""
    unavailable: list[str]
    sections: list[SectionView]
    provenance: dict[str, str]
    connections: list[str] = Field(default_factory=list)
    """Identifiers found in more than one system, computed from shared keys."""
    untrusted_content_seen: bool
    model: str
    notice: str | None = None


class PendingActionView(BaseModel):
    id: str
    tool: str
    risk: str
    reason: str
    preview: str
    created_at: datetime


class DecisionRequest(BaseModel):
    note: str | None = None


class DecisionResponse(BaseModel):
    id: str
    status: str
    executed: bool = False
    result: str | None = None
    autonomy: dict[str, Any] = Field(default_factory=dict)


# -- routes ----------------------------------------------------------------


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> ChatResponse:
    run = await services.runtime.run(principal, request.message, max_steps=request.max_steps)

    calls = [
        ToolCallView(
            tool=inv.resolved_name,
            arguments=inv.arguments,
            ok=inv.ok,
            held=inv.held,
            pending_action_id=inv.pending_action_id,
            repairs=inv.repairs,
            detail=inv.result.error,
        )
        for inv in run.invocations
    ]

    notice = None
    if run.held_actions:
        notice = (
            f"{len(run.held_actions)} action(s) need your approval before they run. See /approvals."
        )
    elif run.taint.suspicious:
        notice = (
            "Content retrieved during this request matched known prompt-injection "
            "patterns. It was treated as data only."
        )

    return ChatResponse(
        reply=run.final,
        stop_reason=str(run.stop_reason),
        tool_calls=calls,
        pending_approvals=run.held_actions,
        untrusted_content_seen=run.taint.tainted,
        notice=notice,
    )


@router.get("/brief", response_model=BriefResponse)
async def brief(
    greeting: str = "Good morning",
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> BriefResponse:
    result = await services.brief.generate(principal, greeting=greeting)

    return BriefResponse(
        body=result.body,
        generated_at=result.generated_at,
        complete=result.complete,
        unavailable=result.degraded_sources,
        sections=[
            SectionView(
                section=s.section,
                heading=s.heading,
                source=s.tool,
                available=s.ok,
                error=s.error,
                duration_ms=round(s.duration_ms, 1),
            )
            for s in result.sections
        ],
        provenance=result.provenance,
        connections=result.connections,
        untrusted_content_seen=result.taint.tainted,
        model=result.model,
        notice=result.error,
    )


@router.get("/approvals", response_model=list[PendingActionView])
async def list_approvals(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> list[PendingActionView]:
    return [
        PendingActionView(
            id=a.id,
            tool=a.tool,
            risk=str(a.risk),
            reason=a.reason,
            preview=a.preview,
            created_at=a.created_at,
        )
        for a in services.governor.approvals.pending_for(principal)
    ]


@router.post("/approvals/{action_id}/approve", response_model=DecisionResponse)
async def approve(
    action_id: str,
    request: DecisionRequest | None = None,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> DecisionResponse:
    action = services.governor.approvals.get(action_id)
    if action is None or action.principal_id != principal.user_id:
        # Same response for missing and not-yours: otherwise this endpoint
        # enumerates other people's action IDs.
        raise HTTPException(status_code=404, detail="no such pending action")

    try:
        context = services.governor.approve(action_id, note=request.note if request else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    spec = services.gateway.spec(action.tool)
    services.governor.record_decision(principal, spec, approved=True)

    call = await services.gateway.call(principal, action.tool, action.arguments, context=context)

    return DecisionResponse(
        id=action_id,
        status="approved",
        executed=call.ok,
        result=call.result.content if call.ok else call.result.error,
        autonomy=services.governor.autonomy.describe(principal).get(action.tool, {}),
    )


@router.post("/approvals/{action_id}/reject", response_model=DecisionResponse)
async def reject(
    action_id: str,
    request: DecisionRequest | None = None,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> DecisionResponse:
    action = services.governor.approvals.get(action_id)
    if action is None or action.principal_id != principal.user_id:
        raise HTTPException(status_code=404, detail="no such pending action")

    try:
        services.governor.reject(action_id, note=request.note if request else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    services.governor.record_decision(principal, services.gateway.spec(action.tool), approved=False)

    return DecisionResponse(id=action_id, status="rejected")


@router.get("/me/autonomy")
async def my_autonomy(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> dict[str, Any]:
    """What this user's assistant may do unattended, and what it has done.

    The transparency surface from gap G15: a user who cannot see what their
    assistant is allowed to do cannot meaningfully consent to it.
    """
    return {
        "tools": services.governor.autonomy.describe(principal),
        "recent_actions": [
            {
                "tool": e.tool,
                "at": e.at,
                "risk": str(e.risk),
                "reversible": e.reversible,
            }
            for e in services.governor.journal.recent_for(principal)
        ],
        "visible_tools": [s.qualified_name for s in services.gateway.tools_for(principal)],
    }


@router.get("/system/health")
async def system_health(services: Services = Depends(get_services)) -> dict[str, Any]:
    """Per-connector status, so degradation is visible rather than inferred."""
    health = services.gateway.server_health()
    return {
        "connectors": health,
        "degraded": [name for name, status in health.items() if status != "ok"],
    }
