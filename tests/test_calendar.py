"""Calendar tests, built from the iCalendar shapes real servers emit."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from uione.connectors.calendar import (
    CalDavBackend,
    CalendarAccount,
    CalendarError,
    Event,
    InMemoryCalendarBackend,
    build_calendar_source,
    day_bounds,
    expand,
    free_slots,
    parse_events,
)
from uione.connectors.calendar.compose import build_event
from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
)

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))
IST = ZoneInfo("Europe/Istanbul")


def ics(body: str) -> str:
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n{body}\r\nEND:VCALENDAR\r\n"


def vevent(**kwargs) -> str:
    lines = ["BEGIN:VEVENT"]
    lines += [f"{k.upper().replace('_', '-')}:{v}" for k, v in kwargs.items()]
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


# -- parsing ---------------------------------------------------------------


def test_a_simple_event_is_parsed() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Incident review",
            dtstart="20260727T093000Z",
            dtend="20260727T100000Z",
            location="Room 3",
        )
    )

    event = parse_events(document)[0]

    assert event.summary == "Incident review"
    assert event.start == datetime(2026, 7, 27, 9, 30, tzinfo=UTC)
    assert event.duration == timedelta(minutes=30)
    assert event.location == "Room 3"


def test_attendees_are_extracted_without_the_mailto_prefix() -> None:
    document = ics(
        "BEGIN:VEVENT\r\nUID:e1\r\nSUMMARY:Review\r\nDTSTART:20260727T090000Z\r\n"
        "DTEND:20260727T100000Z\r\nATTENDEE:mailto:bob@corp.example\r\n"
        "ATTENDEE:mailto:sre@corp.example\r\nEND:VEVENT"
    )

    event = parse_events(document)[0]

    assert event.attendees == ["bob@corp.example", "sre@corp.example"]


def test_an_all_day_event_is_flagged() -> None:
    """A date, not a datetime — treating it as midnight UTC shifts the day."""
    document = ics(
        "BEGIN:VEVENT\r\nUID:e1\r\nSUMMARY:Public holiday\r\n"
        "DTSTART;VALUE=DATE:20260727\r\nDTEND;VALUE=DATE:20260728\r\nEND:VEVENT"
    )

    event = parse_events(document, tz=IST)[0]

    assert event.all_day
    assert event.start.date() == date(2026, 7, 27)
    assert "all day" in event.render(tz=IST)


def test_folded_lines_are_handled() -> None:
    """Servers fold long lines at 75 octets; a naive split loses the rest."""
    document = ics(
        "BEGIN:VEVENT\r\nUID:e1\r\n"
        "SUMMARY:A very long meeting title that the server has folded across\r\n"
        "  two physical lines\r\n"
        "DTSTART:20260727T090000Z\r\nDTEND:20260727T100000Z\r\nEND:VEVENT"
    )

    event = parse_events(document)[0]

    assert "folded across two physical lines" in event.summary


def test_duration_is_used_when_there_is_no_end() -> None:
    document = ics(
        vevent(uid="e1", summary="Standup", dtstart="20260727T090000Z", duration="PT15M")
    )

    assert parse_events(document)[0].duration == timedelta(minutes=15)


def test_a_malformed_calendar_yields_nothing_rather_than_raising() -> None:
    """One bad document must not empty someone's whole day."""
    assert parse_events("this is not a calendar") == []


def test_one_broken_event_does_not_lose_the_others() -> None:
    document = ics(
        vevent(uid="ok", summary="Good", dtstart="20260727T090000Z", dtend="20260727T100000Z")
        + "\r\n"
        + "BEGIN:VEVENT\r\nUID:bad\r\nSUMMARY:No start\r\nEND:VEVENT"
    )

    events = parse_events(document)

    assert [e.summary for e in events] == ["Good"]


# -- recurrence ------------------------------------------------------------


def window(days: int = 14) -> tuple[datetime, datetime]:
    start = datetime(2026, 7, 27, tzinfo=UTC)
    return start, start + timedelta(days=days)


def test_a_daily_rule_expands() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Standup",
            dtstart="20260727T090000Z",
            dtend="20260727T091500Z",
            rrule="FREQ=DAILY;COUNT=3",
        )
    )
    start, end = window()

    instances = expand(parse_events(document), start=start, end=end, ics=document)

    assert len(instances) == 3
    assert [i.start.day for i in instances] == [27, 28, 29]


def test_weekly_by_day_expands_to_the_named_days() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Team sync",
            dtstart="20260727T100000Z",
            dtend="20260727T103000Z",
            rrule="FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4",
        )
    )
    start, end = window()

    instances = expand(parse_events(document), start=start, end=end, ics=document)

    # Monday 27th, Wednesday 29th, Monday 3rd, Wednesday 5th.
    assert [i.start.weekday() for i in instances] == [0, 2, 0, 2]


def test_until_bounds_the_expansion() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Daily",
            dtstart="20260727T090000Z",
            dtend="20260727T093000Z",
            rrule="FREQ=DAILY;UNTIL=20260729T235959Z",
        )
    )
    start, end = window()

    instances = expand(parse_events(document), start=start, end=end, ics=document)

    assert all(i.start.day <= 29 for i in instances)


def test_instances_outside_the_window_are_excluded() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Daily",
            dtstart="20260727T090000Z",
            dtend="20260727T093000Z",
            rrule="FREQ=DAILY;COUNT=30",
        )
    )
    start, end = window(days=3)

    instances = expand(parse_events(document), start=start, end=end, ics=document)

    assert len(instances) == 3


def test_an_unsupported_rule_is_noted_not_invented() -> None:
    """A naive expander guesses here; an invented meeting is worse than a gap."""
    document = ics(
        vevent(
            uid="e1",
            summary="Board meeting",
            dtstart="20260727T090000Z",
            dtend="20260727T100000Z",
            rrule="FREQ=YEARLY;BYSETPOS=3;BYDAY=TH",
        )
    )

    event = parse_events(document)[0]

    assert "not expanded" in event.recurrence_note
    assert "not expanded" in event.render()


def test_expansion_is_bounded_against_a_runaway_rule() -> None:
    document = ics(
        vevent(
            uid="e1",
            summary="Forever",
            dtstart="20260727T090000Z",
            dtend="20260727T091000Z",
            rrule="FREQ=DAILY",
        )
    )
    start, end = window(days=365 * 5)

    instances = expand(parse_events(document), start=start, end=end, ics=document)

    assert len(instances) < 900


# -- free slots ------------------------------------------------------------


def event_at(hour: int, *, minutes: int = 60, summary: str = "Busy") -> Event:
    start = datetime(2026, 7, 27, hour, 0, tzinfo=UTC)
    return Event(
        uid=f"e{hour}", summary=summary, start=start, end=start + timedelta(minutes=minutes)
    )


def test_free_slots_exclude_busy_hours() -> None:
    slots = free_slots([event_at(9), event_at(14)], day=date(2026, 7, 27), tz=UTC)

    assert "09:00" not in slots
    assert "14:00" not in slots
    assert "10:00" in slots


def test_free_slots_return_times_only() -> None:
    """The A2A contract may permit free/busy while forbidding subjects."""
    slots = free_slots(
        [event_at(9, summary="Salary review with HR")], day=date(2026, 7, 27), tz=UTC
    )

    assert all("Salary" not in slot for slot in slots)
    assert all(len(slot) == 5 for slot in slots)


def test_a_full_day_has_no_free_slots() -> None:
    busy = [event_at(h) for h in range(9, 18)]

    assert free_slots(busy, day=date(2026, 7, 27), tz=UTC) == []


def test_a_partial_overlap_still_blocks_the_slot() -> None:
    slots = free_slots([event_at(10, minutes=15)], day=date(2026, 7, 27), tz=UTC)

    assert "10:00" not in slots


# -- CalDAV transport ------------------------------------------------------


def multistatus(*documents: str) -> str:
    inner = "".join(
        f"<response><propstat><prop><C:calendar-data>{d}</C:calendar-data>"
        f"</prop></propstat></response>"
        for d in documents
    )
    return (
        '<?xml version="1.0"?><multistatus xmlns="DAV:" '
        f'xmlns:C="urn:ietf:params:xml:ns:caldav">{inner}</multistatus>'
    )


def account(**kwargs) -> CalendarAccount:
    return CalendarAccount(
        url="https://cal.corp.example/dav/alice/", username="alice", password="pw", **kwargs
    )


async def test_a_report_query_returns_events() -> None:
    document = ics(
        vevent(uid="e1", summary="Review", dtstart="20260727T093000Z", dtend="20260727T100000Z")
    )
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = request.content.decode()
        return httpx.Response(207, text=multistatus(document))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        events = await CalDavBackend(account(), client=client).events_between(*window(1))

    assert captured["method"] == "REPORT"
    assert "time-range" in captured["body"], "the server must do the filtering"
    assert [e.summary for e in events] == ["Review"]


async def test_events_from_several_responses_are_merged() -> None:
    first = ics(vevent(uid="a", summary="A", dtstart="20260727T090000Z", dtend="20260727T093000Z"))
    second = ics(vevent(uid="b", summary="B", dtstart="20260727T110000Z", dtend="20260727T113000Z"))

    transport = httpx.MockTransport(lambda _r: httpx.Response(207, text=multistatus(first, second)))
    async with httpx.AsyncClient(transport=transport) as client:
        events = await CalDavBackend(account(), client=client).events_between(*window(1))

    assert [e.summary for e in events] == ["A", "B"]


async def test_duplicate_instances_are_collapsed() -> None:
    """Servers return the same recurring event under several hrefs."""
    document = ics(
        vevent(uid="a", summary="A", dtstart="20260727T090000Z", dtend="20260727T093000Z")
    )

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(207, text=multistatus(document, document))
    )
    async with httpx.AsyncClient(transport=transport) as client:
        events = await CalDavBackend(account(), client=client).events_between(*window(1))

    assert len(events) == 1


async def test_escaped_calendar_data_is_recovered() -> None:
    document = ics(
        vevent(
            uid="e1", summary="R&amp;D sync", dtstart="20260727T090000Z", dtend="20260727T093000Z"
        )
    ).replace("<", "&lt;")

    transport = httpx.MockTransport(lambda _r: httpx.Response(207, text=multistatus(document)))
    async with httpx.AsyncClient(transport=transport) as client:
        events = await CalDavBackend(account(), client=client).events_between(*window(1))

    assert events[0].summary == "R&D sync"


async def test_bad_credentials_are_reported_clearly() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CalendarError, match="credentials"):
            await CalDavBackend(account(), client=client).events_between(*window(1))


async def test_an_unreachable_server_raises_calendar_error() -> None:
    def boom(_request):
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(boom)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(CalendarError, match="unreachable"):
            await CalDavBackend(account(), client=client).events_between(*window(1))


# -- tools -----------------------------------------------------------------


async def build_gateway(backend) -> McpGateway:
    gateway = McpGateway(
        policy=ToolPolicy(
            [Grant(role="analyst", tools=frozenset({"calendar.*"}), max_risk=RiskClass.READ)]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )
    await gateway.register(build_calendar_source(backend, timezone="UTC"))
    return gateway


async def test_today_lists_events() -> None:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9)
    backend = InMemoryCalendarBackend(
        events=[Event(uid="e1", summary="Standup", start=start, end=start + timedelta(minutes=15))]
    )
    gateway = await build_gateway(backend)

    call = await gateway.call(ALICE, "calendar.today")

    assert call.ok
    assert "Standup" in call.result.content


async def test_an_empty_day_is_success_not_failure() -> None:
    gateway = await build_gateway(InMemoryCalendarBackend())

    call = await gateway.call(ALICE, "calendar.today")

    assert call.ok
    assert call.result.structured["count"] == 0


async def test_an_outage_degrades_rather_than_raising() -> None:
    backend = InMemoryCalendarBackend(fail_with="CalDAV server unreachable")
    gateway = await build_gateway(backend)

    call = await gateway.call(ALICE, "calendar.today")

    assert not call.ok
    assert "unreachable" in (call.result.error or "")


async def test_availability_returns_times_without_titles() -> None:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9)
    backend = InMemoryCalendarBackend(
        events=[
            Event(uid="e1", summary="Salary review", start=start, end=start + timedelta(hours=1))
        ]
    )
    gateway = await build_gateway(backend)

    call = await gateway.call(ALICE, "calendar.availability")

    assert call.ok
    assert "Salary" not in call.result.content
    assert "09:00" not in call.result.content


async def test_reads_of_titles_are_marked_untrusted() -> None:
    """Meeting titles are written by other people, including outside senders."""
    gateway = await build_gateway(InMemoryCalendarBackend())

    assert gateway.spec("calendar.today").returns_untrusted_content
    assert gateway.spec("calendar.upcoming").returns_untrusted_content


async def test_availability_is_not_marked_untrusted() -> None:
    """It returns only slot times, so it must not taint an A2A answer."""
    gateway = await build_gateway(InMemoryCalendarBackend())

    assert not gateway.spec("calendar.availability").returns_untrusted_content


async def test_the_upcoming_window_is_clamped() -> None:
    gateway = await build_gateway(InMemoryCalendarBackend())

    call = await gateway.call(ALICE, "calendar.upcoming", {"days": 9999})

    assert call.result.structured["days"] == 14


def test_day_bounds_cover_exactly_one_day() -> None:
    start, end = day_bounds(date(2026, 7, 27), IST)

    assert start.hour == 0
    assert end - start == timedelta(days=1)
    assert start.tzinfo is IST


# -- composing iCalendar ---------------------------------------------------


def test_a_summary_with_structural_characters_is_escaped() -> None:
    """A comma or semicolon in a title is syntax unless escaped, so
    "Review budget, headcount" either fails to parse or silently becomes two
    properties."""
    _, ics = build_event(
        summary="Review budget, headcount; and hiring",
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    assert r"SUMMARY:Review budget\, headcount\; and hiring" in ics


def test_a_backslash_is_escaped_before_everything_else() -> None:
    """Escaping the backslash last would escape the escapes we just added."""
    _, ics = build_event(
        summary=r"Path C:\reports, final",
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    assert r"C:\\reports\, final" in ics


def test_long_lines_are_folded_with_a_leading_space() -> None:
    """RFC 5545 folds at 75 octets, and the continuation space is protocol
    rather than indentation."""
    _, ics = build_event(
        summary="A " + "very " * 40 + "long title",
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75, f"unfolded line: {line[:40]}…"
    assert "\r\n " in ics


def test_folding_counts_octets_not_characters() -> None:
    """A title in Turkish folds earlier than its length suggests, and a server
    counting octets rejects a line this code thought was short enough."""
    _, ics = build_event(
        summary="Ödeme mutabakatı toplantısı " * 4,
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_the_output_uses_crlf() -> None:
    """Plenty of parsers accept bare LF, which is what makes this the bug that
    only appears against the one that does not."""
    _, ics = build_event(
        summary="Standup",
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 15, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    assert "\r\n" in ics
    assert not re.search(r"(?<!\r)\n", ics)


def test_an_invitation_is_tentative_not_confirmed() -> None:
    """Marking it CONFIRMED shows the meeting as settled in everyone's calendar
    before anybody accepted."""
    _, ics = build_event(
        summary="Review",
        start=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        end=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        organizer="me@corp.example",
        attendees=["bora@corp.example"],
    )

    assert "STATUS:TENTATIVE" in ics
    assert "PARTSTAT=NEEDS-ACTION" in ics


def test_an_event_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="end after it starts"):
        build_event(
            summary="Impossible",
            start=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            end=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
            organizer="me@corp.example",
            attendees=["bora@corp.example"],
        )
