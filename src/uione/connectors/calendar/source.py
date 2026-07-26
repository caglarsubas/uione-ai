"""Calendar exposed as governed MCP tools.

Risk classification, per the connector certification rule (F3.8):

* reads are ``READ``. Calendar entries are written by colleagues and by external
  meeting invitations, so ``returns_untrusted_content`` is set: a meeting title
  is a place an outsider can put text in front of the model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from uione.connectors.calendar.backend import CalendarBackend, CalendarError, day_bounds
from uione.connectors.calendar.events import free_slots
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

MAX_DAYS = 14


def build_calendar_source(
    backend: CalendarBackend, *, name: str = "calendar", timezone: str = "UTC"
) -> InMemoryToolSource:
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001
        tz = UTC

    source = InMemoryToolSource(name)

    async def today(_args: dict) -> ToolResult:
        start, end = day_bounds(datetime.now(tz).date(), tz)
        try:
            events = await backend.events_between(start, end)
        except CalendarError as exc:
            return ToolResult.failure(str(exc))

        if not events:
            return ToolResult.success("Nothing scheduled today.", {"count": 0})
        return ToolResult.success(
            "\n".join(e.render(tz=tz) for e in events),
            {"count": len(events), "recurring": sum(1 for e in events if e.recurring)},
        )

    async def upcoming(args: dict) -> ToolResult:
        try:
            days = max(1, min(int(args.get("days", 7)), MAX_DAYS))
        except (TypeError, ValueError):
            days = 7

        start = datetime.now(tz)
        try:
            events = await backend.events_between(start, start + timedelta(days=days))
        except CalendarError as exc:
            return ToolResult.failure(str(exc))

        if not events:
            # Same keys whether or not anything was found: a caller that has to
            # branch on which fields exist will eventually forget to.
            return ToolResult.success(
                f"Nothing scheduled in the next {days} day(s).", {"count": 0, "days": days}
            )

        lines = []
        current_day = None
        for event in events:
            local = event.start.astimezone(tz)
            if local.date() != current_day:
                current_day = local.date()
                lines.append(f"{current_day:%A %d %B}")
            lines.append("  " + event.render(tz=tz))
        return ToolResult.success("\n".join(lines), {"count": len(events), "days": days})

    async def availability(args: dict) -> ToolResult:
        """Free working-hour slots. Times only — never what fills the rest."""
        try:
            offset = max(0, min(int(args.get("days_ahead", 0)), MAX_DAYS))
        except (TypeError, ValueError):
            offset = 0

        day = (datetime.now(tz) + timedelta(days=offset)).date()
        start, end = day_bounds(day, tz)
        try:
            events = await backend.events_between(start, end)
        except CalendarError as exc:
            return ToolResult.failure(str(exc))

        slots = free_slots(events, day=day, tz=tz)
        if not slots:
            return ToolResult.success(f"No free slots on {day}.", {"count": 0, "date": str(day)})
        return ToolResult.success(
            f"Free on {day}: {', '.join(slots)}", {"count": len(slots), "date": str(day)}
        )

    source.register(
        "today",
        today,
        description="Today's calendar entries.",
        risk=RiskClass.READ,
        # Meeting titles and descriptions are written by other people, including
        # external senders whose invitations land here.
        returns_untrusted_content=True,
    )
    source.register(
        "upcoming",
        upcoming,
        description="Calendar entries over the next few days.",
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": f"How many days ahead (1-{MAX_DAYS})."}
            },
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "availability",
        availability,
        description="Free working-hour slots on a day. Returns times only.",
        parameters={
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "0 for today."},
            },
        },
        risk=RiskClass.READ,
        # Slot times contain no text anyone authored, so this one is safe to read
        # without tainting the session — which matters because A2A availability
        # answers run through it.
        returns_untrusted_content=False,
    )
    return source
