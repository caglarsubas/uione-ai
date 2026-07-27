"""Calendar exposed as governed MCP tools.

Risk classification, per the connector certification rule (F3.8):

* reads are ``READ``. Calendar entries are written by colleagues and by external
  meeting invitations, so ``returns_untrusted_content`` is set: a meeting title
  is a place an outsider can put text in front of the model.
* ``propose_meeting`` is ``EXTERNAL_FACING``. Creating an event with attendees
  causes the server to email invitations, so it leaves the building — and it
  puts an entry in other people's calendars, which no deletion takes back once
  they have seen it. It is classified by what it does to *them*, not by whether
  the row can be removed from a database.

The egress policy sees the attendee list as arguments, so the same check that
stops mail reaching an unapproved domain stops an invitation reaching one. That
is not a coincidence to be relied on quietly: attendees are passed as a plain
list precisely so the existing check applies without a special case.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from uione.connectors.calendar.backend import CalendarBackend, CalendarError, day_bounds
from uione.connectors.calendar.compose import build_event, valid_address
from uione.connectors.calendar.events import free_slots
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

MAX_DAYS = 14

#: Longest meeting this will create. Not a technical limit — a guard against a
#: parsing slip turning "09:00 for 30 minutes" into a nine-hour block in
#: everyone's calendar.
MAX_MEETING_MINUTES = 8 * 60

#: Most people one invitation may reach. An assistant that can invite the whole
#: company is one prompt away from doing so.
MAX_ATTENDEES = 20


def build_calendar_source(
    backend: CalendarBackend,
    *,
    name: str = "calendar",
    timezone: str = "UTC",
    organizer: str = "",
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

    async def propose_meeting(args: dict) -> ToolResult:
        summary = str(args.get("title", "")).strip()
        when = str(args.get("start", "")).strip()
        attendees = args.get("attendees") or []

        if not summary:
            return ToolResult.failure("title is required")
        if isinstance(attendees, str):
            # Models pass a comma-separated string about as often as a list.
            # Accepting both beats failing on a formatting preference.
            attendees = [a.strip() for a in attendees.split(",") if a.strip()]
        if not isinstance(attendees, list) or not attendees:
            return ToolResult.failure("attendees is required, as a list of email addresses")
        if len(attendees) > MAX_ATTENDEES:
            return ToolResult.failure(
                f"refusing to invite {len(attendees)} people; the limit is {MAX_ATTENDEES}"
            )

        invalid = [a for a in attendees if not valid_address(str(a))]
        if invalid:
            # Refused rather than dropped. A model that invents a plausible
            # colleague's address must not have a calendar server email it, and
            # silently removing the bad ones would send a meeting to a subset
            # while reporting success.
            return ToolResult.failure(f"not valid email addresses: {', '.join(map(str, invalid))}")

        try:
            start = datetime.fromisoformat(when)
        except ValueError:
            return ToolResult.failure("start must be an ISO 8601 time, e.g. 2026-07-28T09:30")
        if start.tzinfo is None:
            # A naive time means the user's timezone, not the server's. Guessing
            # the server's is how an 09:00 meeting arrives at 11:00 for somebody
            # in another office.
            start = start.replace(tzinfo=tz)

        try:
            minutes = int(args.get("minutes", 30))
        except (TypeError, ValueError):
            return ToolResult.failure("minutes must be a number")
        if minutes <= 0 or minutes > MAX_MEETING_MINUTES:
            return ToolResult.failure(f"minutes must be between 1 and {MAX_MEETING_MINUTES}")

        end = start + timedelta(minutes=minutes)

        # Checked, not assumed. Proposing a meeting on top of an existing one is
        # the fastest way for somebody to stop trusting this.
        try:
            clashes = [e for e in await backend.events_between(start, end) if not e.all_day]
        except CalendarError as exc:
            return ToolResult.failure(str(exc))
        if clashes:
            return ToolResult.failure(
                "that time is not free: "
                + "; ".join(e.render(tz=tz) for e in clashes[:3])
                + ". Check availability and choose another slot."
            )

        try:
            uid, ics = build_event(
                summary=summary,
                start=start,
                end=end,
                organizer=organizer or "assistant@localhost",
                attendees=[str(a) for a in attendees],
                description=str(args.get("description", "")),
                location=str(args.get("location", "")),
            )
            url = await backend.create_event(uid, ics)
        except (ValueError, CalendarError) as exc:
            return ToolResult.failure(str(exc))

        log.info("calendar.meeting_created", uid=uid, attendees=len(attendees), minutes=minutes)
        return ToolResult.success(
            f"Proposed “{summary}” at {start.astimezone(tz):%Y-%m-%d %H:%M} "
            f"for {minutes}m with {len(attendees)} attendee(s). "
            "Invitations are marked tentative until they reply.",
            {"uid": uid, "url": url, "start": start.isoformat(), "attendees": attendees},
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
    source.register(
        "propose_meeting",
        propose_meeting,
        description=(
            "Propose a meeting: creates a tentative calendar event and invites the attendees. "
            "Check availability first — a clashing time is refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {
                    "type": "string",
                    "description": "ISO 8601 start time, e.g. 2026-07-28T09:30.",
                },
                "minutes": {"type": "integer", "description": "Duration, default 30."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses. Every one is invited by the server.",
                },
                "location": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title", "start", "attendees"],
        },
        # It emails people and puts an entry in their calendar. Neither is taken
        # back by deleting the event afterwards.
        risk=RiskClass.EXTERNAL_FACING,
    )
    return source
