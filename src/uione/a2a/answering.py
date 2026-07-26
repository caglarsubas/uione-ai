"""Answering A2A requests from real connector data.

The answerer is handed the facets it may reveal and builds a response from only
those. That shape is the enforcement: it cannot leak by forgetting to check,
because it is never given the un-permitted data to forget about.

Everything it reads goes through the governed gateway as the *owner*, so a
colleague's assistant can never learn something the owner themselves could not
see. A2A widens who may ask, never what may be reached.
"""

from __future__ import annotations

import re

import structlog

from uione.a2a.contracts import Facet
from uione.a2a.messages import A2ARequest, AgentCard
from uione.mcphub import McpGateway, Principal

log = structlog.get_logger(__name__)

_TIME_RANGE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

WORKING_HOURS = [f"{h:02d}:00" for h in range(9, 18)]


class GatewayAnswerer:
    """Answers A2A requests from the owner's own connectors."""

    def __init__(
        self,
        gateway: McpGateway,
        *,
        principal_for,
        calendar_tool: str = "calendar.today",
        availability_tool: str = "calendar.availability",
        tasks_tool: str = "tasks.my_open_issues",
    ) -> None:
        self._gateway = gateway
        self._principal_for = principal_for
        self._calendar_tool = calendar_tool
        self._availability_tool = availability_tool
        self._tasks_tool = tasks_tool

    async def __call__(
        self, target: AgentCard, request: A2ARequest, granted: frozenset[Facet]
    ) -> dict:
        owner: Principal = self._principal_for(target.owner_id)
        data: dict = {}

        if Facet.FREE_BUSY in granted:
            data["free_slots"] = await self._free_slots(owner)

        if Facet.WORKLOAD in granted:
            data["workload"] = await self._workload(owner)

        if Facet.TASK_STATUS in granted:
            count = await self._open_task_count(owner)
            if count is not None:
                data["open_tasks"] = count

        # Note what is absent. MEETING_SUBJECTS and TASK_DETAIL have no branch
        # here at all: the coarse facets are answerable from data the owner
        # already exposes, and the content facets need a deliberate design pass
        # rather than a quick mapping. Not implementing them is safer than
        # implementing them approximately.

        data["summary"] = _summarise(data, target)
        return data

    async def _free_slots(self, owner: Principal) -> list[str]:
        """Free times — never the meetings themselves.

        Uses the calendar connector's own availability tool when one exists,
        which computes slots from actual event times and returns *only* times.
        That matters beyond tidiness: the fallback below reads the rendered
        day, so a meeting whose title happens to contain "14:00" would mark the
        wrong hour busy, and a title is exactly the thing a disclosure contract
        may forbid us from seeing at all.
        """
        if self._gateway.has_tool(self._availability_tool):
            call = await self._gateway.call(owner, self._availability_tool)
            if call.ok:
                return _parse_slots(call.result.content)
            return []

        call = await self._gateway.call(owner, self._calendar_tool)
        if not call.ok:
            return []

        busy_hours = {
            m.group(1).zfill(2) + ":00" for m in _TIME_RANGE.finditer(call.result.content)
        }
        return [slot for slot in WORKING_HOURS if slot not in busy_hours]

    async def _workload(self, owner: Principal) -> str:
        count = await self._open_task_count(owner)
        if count is None:
            return "unknown"
        if count == 0:
            return "clear"
        if count <= 3:
            return "moderate"
        return "heavily loaded"

    async def _open_task_count(self, owner: Principal) -> int | None:
        call = await self._gateway.call(owner, self._tasks_tool)
        if not call.ok:
            return None
        structured = call.result.structured or {}
        if "count" in structured:
            return int(structured["count"])
        return len([line for line in call.result.content.splitlines() if line.startswith("[")])


def _parse_slots(text: str) -> list[str]:
    """Pull HH:MM values out of an availability answer."""
    return [match.group(0) for match in _TIME_RANGE.finditer(text)]


def _summarise(data: dict, target: AgentCard) -> str:
    parts = []
    if slots := data.get("free_slots"):
        parts.append(f"free at {', '.join(slots[:4])}")
    elif "free_slots" in data:
        parts.append("no free slots in working hours")
    if workload := data.get("workload"):
        parts.append(f"workload {workload}")
    if (count := data.get("open_tasks")) is not None:
        parts.append(f"{count} open task(s)")
    return f"{target.display_name}: " + ("; ".join(parts) or "nothing to share")
