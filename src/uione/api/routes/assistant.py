"""Assistant surface: chat, the morning brief, and the approval queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from uione.a2a import (
    A2ARequest,
    AgentCard,
    AgentDirectory,
    Capability,
    Facet,
    RequestKind,
)
from uione.api.deps import Services, default_schedule, get_principal, get_services
from uione.config import get_settings
from uione.mcphub import Principal
from uione.modelplane import ChatMessage
from uione.proactive import JobKind, QueueBuilder, Schedule, ScheduledJob

log = structlog.get_logger(__name__)

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
    pregenerated: bool = False
    """True when this was prepared ahead of time rather than on request."""
    age_seconds: float | None = None


class ScheduleView(BaseModel):
    kind: str
    enabled: bool
    at: str
    timezone: str
    next_run: datetime | None = None
    last_run: datetime | None = None
    runs: int = 0
    failures: int = 0
    last_error: str | None = None


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    at: str | None = Field(default=None, description="HH:MM in the user's timezone.")
    timezone: str | None = None


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
    history, tainted = await services.conversations.history(principal.user_id)
    run = await services.runtime.run(
        principal,
        request.message,
        history=history,
        max_steps=request.max_steps,
        tainted=tainted,
    )
    await _remember(services, principal, request.message, run)

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


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> StreamingResponse:
    """The same conversation, delivered as it happens.

    **Server-sent events rather than a WebSocket.** Everything here flows one
    way, so the second direction would be unused protocol. SSE is plain HTTP,
    which matters more than it sounds for a product deployed behind somebody
    else's reverse proxy: no upgrade handshake to be refused, no separate
    timeout to be tuned, and it works through the corporate middleboxes that
    quietly drop WebSocket upgrades.

    The headers are not decoration. `X-Accel-Buffering: no` stops nginx
    buffering the whole response and delivering it at once, which turns a stream
    back into the wait it replaced — and is invisible in development, because
    nobody runs nginx there.
    """

    history, tainted = await services.conversations.history(principal.user_id)

    async def events():
        # Collected as the stream runs and written once at the end. Persisting
        # per event would leave a half-turn behind if the client disconnects
        # mid-answer, and a conversation containing a question with no answer
        # replays as though the assistant fell silent.
        turn: list[ChatMessage] = [ChatMessage(role="user", content=request.message)]
        saw_untrusted = tainted
        try:
            async for kind, payload in services.runtime.stream(
                principal,
                request.message,
                history=history,
                max_steps=request.max_steps,
                tainted=tainted,
            ):
                if kind == "done" and payload.get("final"):
                    turn.append(ChatMessage(role="assistant", content=payload["final"]))
                if kind == "tool_result" and payload.get("untrusted"):
                    saw_untrusted = True
                yield _sse(kind, payload)
        except Exception as exc:  # noqa: BLE001 — the client must learn it stopped
            # Without this the connection simply ends, and a half-written answer
            # is indistinguishable from a complete one.
            log.exception("chat.stream_failed", principal=principal.user_id)
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            # Even a failed turn is remembered, so "that didn't work, try again"
            # has something to refer to. Only the prose is kept: tool results
            # are re-fetched rather than replayed, because a stale count read
            # back as current is worse than one more call.
            if len(turn) > 1:
                await services.conversations.append(principal.user_id, turn, tainted=saw_untrusted)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _remember(services: Services, principal: Principal, message: str, run) -> None:
    """Keep the prose of a turn, not its tool traffic.

    Tool results are deliberately not stored. Replaying "5 unread" from an hour
    ago as though it were current is worse than spending one more call to ask,
    and it is exactly the kind of confident staleness this product is built to
    avoid elsewhere.
    """
    turn = [ChatMessage(role="user", content=message)]
    if run.final:
        turn.append(ChatMessage(role="assistant", content=run.final))
    if len(turn) > 1:
        await services.conversations.append(principal.user_id, turn, tainted=run.taint.tainted)


@router.post("/chat/new", status_code=204)
async def new_conversation(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> Response:
    """Start again. The audit log keeps everything that was said and done."""
    await services.conversations.clear(principal.user_id)
    return Response(status_code=204)


def _sse(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"


@router.get("/brief", response_model=BriefResponse)
async def brief(
    greeting: str = "Good morning",
    refresh: bool = False,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> BriefResponse:
    """Return the user's brief.

    Served from the pre-generated copy when one is fresh, which is the whole
    point of the scheduler: "good morning" should be answered immediately, not
    after several seconds of generation. ``refresh=true`` forces a rebuild.
    """
    settings = get_settings()
    pregenerated = False
    age_seconds: float | None = None

    stored = (
        None
        if refresh
        else services.brief_store.get(
            principal.user_id, max_age=timedelta(minutes=settings.brief_max_age_minutes)
        )
    )
    if stored is not None:
        result = stored.brief
        pregenerated = True
        age_seconds = round(stored.age(datetime.now(UTC)).total_seconds(), 1)
    else:
        result = await services.brief.generate(principal, greeting=greeting)
        # Cache it, so a second reader this morning does not pay again.
        services.brief_store.put(principal.user_id, result)

    return BriefResponse(
        pregenerated=pregenerated,
        age_seconds=age_seconds,
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


@router.get("/me/undoable")
async def undoable(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> list[dict]:
    """What this person's assistant did that can still be taken back (G13).

    Read-only. The rail exists to make the last hour visible — "a visible undo
    window changes user psychology from fear to experimentation" — and seeing
    that a thing *is* reversible is most of that. Performing the reversal is a
    mutating action and goes through governance like any other; it is not a
    button this endpoint grants.
    """
    entries = await services.governor.journal.undoable_for(principal)
    return [
        {
            "id": e.id,
            "tool": e.tool,
            "risk": str(e.risk),
            "at": e.at.isoformat(),
            "undo_tool": e.undo_tool,
        }
        for e in entries
    ]


@router.get("/queue")
async def action_queue(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> dict:
    """Everything awaiting this person, ranked (F6.3).

    No model call, so it is cheap enough to poll — which is what makes it a
    surface you leave open rather than one you request. See
    `uione.proactive.queue`.
    """
    approvals = await services.governor.approvals.pending_for(principal)
    queue = await QueueBuilder(services.gateway).build(principal, approvals=approvals)
    return queue.to_dict()


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
        for a in await services.governor.approvals.pending_for(principal)
    ]


@router.post("/approvals/{action_id}/approve", response_model=DecisionResponse)
async def approve(
    action_id: str,
    request: DecisionRequest | None = None,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> DecisionResponse:
    action = await services.governor.approvals.get(action_id)
    if action is None or action.principal_id != principal.user_id:
        # Same response for missing and not-yours: otherwise this endpoint
        # enumerates other people's action IDs.
        raise HTTPException(status_code=404, detail="no such pending action")

    try:
        context = await services.governor.approve(action_id, note=request.note if request else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    spec = services.gateway.spec(action.tool)
    await services.governor.record_decision(principal, spec, approved=True)

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
    action = await services.governor.approvals.get(action_id)
    if action is None or action.principal_id != principal.user_id:
        raise HTTPException(status_code=404, detail="no such pending action")

    try:
        await services.governor.reject(action_id, note=request.note if request else None)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await services.governor.record_decision(
        principal, services.gateway.spec(action.tool), approved=False
    )

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
            for e in await services.governor.journal.recent_for(principal)
        ],
        "visible_tools": [s.qualified_name for s in services.gateway.tools_for(principal)],
    }


@router.get("/system/health")
async def system_health(services: Services = Depends(get_services)) -> dict[str, Any]:
    """Per-connector status, so degradation is visible rather than inferred."""
    health = services.gateway.server_health()
    ingestion = services.refresher.status()
    return {
        "connectors": health,
        # `unknown` is not degraded. A connector nobody has called yet is
        # unexercised, not broken, and listing it would put a permanent warning
        # on every deployment that does not use all nine.
        "degraded": sorted(services.gateway.degraded_servers()),
        # The evidence, so "tasks is failing" becomes "tasks is failing: gitea
        # unreachable" — which sends somebody to start gitea rather than to read
        # logs.
        "connector_detail": services.gateway.server_details(),
        # "How old are the permissions we are enforcing?" must be answerable
        # with a number rather than an assumption, so the age is reported even
        # when everything is healthy.
        "ingestion": ingestion,
        "quarantined": [s["source"] for s in ingestion if s["quarantined"]],
        # A connector that failed to start is why a tool is missing. Answering
        # that from health beats answering it from someone's memory of the logs.
        "mcp_servers": services.mcp.health(),
    }


@router.get("/me/schedule", response_model=list[ScheduleView])
async def my_schedule(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> list[ScheduleView]:
    now = datetime.now(UTC)
    return [
        ScheduleView(
            kind=str(job.kind),
            enabled=job.enabled,
            at=job.schedule.at.strftime("%H:%M"),
            timezone=job.schedule.timezone,
            next_run=job.next_run(now) if job.enabled else None,
            last_run=job.last_run,
            runs=job.runs,
            failures=job.failures,
            last_error=job.last_error,
        )
        for job in services.scheduler.for_user(principal.user_id)
    ]


@router.put("/me/schedule", response_model=ScheduleView)
async def set_schedule(
    update: ScheduleUpdate,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> ScheduleView:
    """Set when this user's morning brief is prepared.

    A user who cannot change when their assistant wakes up will simply stop
    opening the brief, which is a worse outcome than the wrong default.
    """
    settings = get_settings()
    existing = next(
        (
            j
            for j in services.scheduler.for_user(principal.user_id)
            if j.kind == JobKind.MORNING_BRIEF
        ),
        None,
    )
    schedule = existing.schedule if existing else default_schedule(settings)

    if update.at is not None:
        try:
            hour, _, minute = update.at.partition(":")
            at = time(int(hour), int(minute or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="at must be HH:MM") from None
    else:
        at = schedule.at

    timezone = update.timezone or schedule.timezone
    try:
        ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=422, detail=f"unknown timezone {timezone!r}") from None

    job = await services.scheduler.save(
        ScheduledJob(
            user_id=principal.user_id,
            kind=JobKind.MORNING_BRIEF,
            schedule=Schedule(at=at, timezone=timezone, jitter_s=schedule.jitter_s),
            enabled=update.enabled if update.enabled is not None else True,
            last_run=existing.last_run if existing else None,
        )
    )
    now = datetime.now(UTC)
    return ScheduleView(
        kind=str(job.kind),
        enabled=job.enabled,
        at=job.schedule.at.strftime("%H:%M"),
        timezone=job.schedule.timezone,
        next_run=job.next_run(now) if job.enabled else None,
        last_run=job.last_run,
    )


# -- A2A -------------------------------------------------------------------


class ColleagueView(BaseModel):
    agent_id: str
    owner_id: str
    display_name: str
    capabilities: list[str]
    external: bool


class AskColleagueRequest(BaseModel):
    agent_id: str
    kind: str = Field(description="ask_availability | ask_workload | ask_task_status")
    payload: dict[str, Any] = Field(default_factory=dict)


class AskColleagueResponse(BaseModel):
    outcome: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    withheld: str = ""
    reason: str = ""
    pending_action_id: str | None = None


class ContractView(BaseModel):
    owner_id: str
    default: list[str]
    by_role: dict[str, list[str]]
    by_user: dict[str, list[str]]
    external_default: list[str]


class ContractUpdate(BaseModel):
    default: list[str] | None = None
    by_role: dict[str, list[str]] | None = None
    by_user: dict[str, list[str]] | None = None


def ensure_agent(principal: Principal, services: Services) -> AgentCard:
    """Make sure this user has an assistant in the directory.

    Registered lazily on first contact rather than provisioned up front, so the
    directory reflects people who actually use the product instead of every row
    in the HR system.
    """
    existing = services.directory.for_owner(principal.user_id)
    if existing is not None:
        return existing
    return services.directory.register(
        AgentCard(
            agent_id=AgentDirectory.agent_id_for(principal.user_id),
            owner_id=principal.user_id,
            display_name=f"{principal.display_name}'s assistant",
            capabilities=frozenset(Capability),
        )
    )


@router.get("/colleagues", response_model=list[ColleagueView])
async def colleagues(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> list[ColleagueView]:
    """Assistants this user can address.

    The directory lists who exists, not what they will tell you — that is the
    contract's job, evaluated per request.
    """
    ensure_agent(principal, services)
    return [
        ColleagueView(
            agent_id=c.agent_id,
            owner_id=c.owner_id,
            display_name=c.display_name,
            capabilities=sorted(str(x) for x in c.capabilities),
            external=c.external,
        )
        for c in services.directory.all()
        if c.owner_id != principal.user_id
    ]


@router.post("/colleagues/ask", response_model=AskColleagueResponse)
async def ask_colleague(
    request: AskColleagueRequest,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> AskColleagueResponse:
    """Ask another employee's assistant something on this user's behalf."""
    try:
        kind = RequestKind(request.kind)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"unknown request kind {request.kind!r}"
        ) from None

    mine = ensure_agent(principal, services)

    response = await services.a2a.send(
        A2ARequest(
            from_agent=mine.agent_id,
            to_agent=request.agent_id,
            kind=kind,
            payload=request.payload,
        ),
        # Roles come from the verified token, not the request body, so a caller
        # cannot claim a role to widen what a colleague's contract reveals.
        requester_roles=principal.roles,
    )

    return AskColleagueResponse(
        outcome=str(response.outcome),
        summary=response.render(),
        data=response.data,
        withheld=response.withheld_note,
        reason=response.reason,
        pending_action_id=response.pending_action_id,
    )


@router.get("/me/disclosure", response_model=ContractView)
async def my_disclosure(
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> ContractView:
    """What this user's assistant may reveal about them, and to whom."""
    contract = services.contracts.for_owner(principal.user_id)
    return ContractView(
        owner_id=contract.owner_id,
        default=sorted(str(f) for f in contract.default),
        by_role={r: sorted(str(f) for f in fs) for r, fs in contract.by_role.items()},
        by_user={u: sorted(str(f) for f in fs) for u, fs in contract.by_user.items()},
        external_default=sorted(str(f) for f in contract.external_default),
    )


@router.put("/me/disclosure", response_model=ContractView)
async def set_disclosure(
    update: ContractUpdate,
    principal: Principal = Depends(get_principal),
    services: Services = Depends(get_services),
) -> ContractView:
    """Change what this user's assistant may reveal.

    Owned by the subject: Bob decides what Bob's assistant says about Bob. That
    is the only arrangement an employee would accept, and the one a works council
    will ask about.
    """

    def parse(names: list[str]) -> frozenset[Facet]:
        try:
            return frozenset(Facet(n) for n in names)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    contract = services.contracts.for_owner(principal.user_id)
    if update.default is not None:
        contract.default = parse(update.default)
    if update.by_role is not None:
        contract.by_role = {r: parse(f) for r, f in update.by_role.items()}
    if update.by_user is not None:
        contract.by_user = {u: parse(f) for u, f in update.by_user.items()}
    services.contracts.set(contract)
    # Stored immediately rather than at shutdown: a contract that reverts to the
    # default on restart is a *narrowing*, so the failure looks like a colleague's
    # assistant refusing a question it answered yesterday — with nothing in any
    # log to connect the two.
    await services.disclosures.save(contract)

    return await my_disclosure(principal, services)
