"""Rendering A2A commitments for the approval queue.

A commitment arriving from another assistant has to read, in the approval card,
as a request from a *person* — "Alice's assistant is asking you to…" — not as an
opaque protocol event. Nobody approves what they cannot picture.
"""

from __future__ import annotations

from uione.a2a.messages import A2ARequest, RequestKind
from uione.mcphub import RiskClass, ToolSpec

_SPECS = {
    RequestKind.PROPOSE_MEETING: ToolSpec(
        server="a2a",
        tool="accept_meeting",
        description="Accept a meeting proposed by a colleague's assistant",
        risk=RiskClass.REVERSIBLE_WRITE,
    ),
    RequestKind.DELEGATE_TASK: ToolSpec(
        server="a2a",
        tool="accept_task",
        description="Take on work delegated by a colleague's assistant",
        risk=RiskClass.REVERSIBLE_WRITE,
    ),
}


def commitment_spec(kind: RequestKind) -> ToolSpec:
    return _SPECS[kind]


def render_commitment(request: A2ARequest) -> str:
    who = request.provenance()
    if request.kind is RequestKind.PROPOSE_MEETING:
        when = request.payload.get("slot") or request.payload.get("when") or "a proposed time"
        subject = request.payload.get("subject") or "a meeting"
        return f"{who} proposes {subject} at {when}"
    if request.kind is RequestKind.DELEGATE_TASK:
        title = request.payload.get("title") or "a task"
        return f"{who} is asking you to take on: {title}"
    return f"{who} requests {request.kind}"
