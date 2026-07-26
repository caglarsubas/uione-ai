"""Calendar events and iCalendar parsing.

iCalendar is parsed with the ``icalendar`` library rather than by hand. The
format looks simple and is not: folded lines, escaped separators, per-property
timezone references, all-day events expressed as dates rather than datetimes,
and recurrence rules with their own grammar. Hand-rolling it produces something
that works on the developer's own calendar and fails on everyone else's.

The one thing handled here rather than delegated is **recurrence expansion**, and
only the common subset. That limit is stated in :func:`expand`, because a
recurring meeting silently missing from a morning brief is worse than one the
brief admits it cannot compute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from icalendar import Calendar

log = structlog.get_logger(__name__)

#: Recurrence frequencies we expand. Anything else is surfaced with a note
#: rather than dropped or guessed at.
SUPPORTED_FREQ = frozenset({"DAILY", "WEEKLY", "MONTHLY"})

_WEEKDAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    attendees: list[str] = field(default_factory=list)
    organizer: str = ""
    status: str = ""

    #: True when this instance came from expanding a recurrence rule.
    recurring: bool = False

    #: Set when the event repeats in a way we do not expand.
    recurrence_note: str = ""

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start

    def render(self, *, tz: ZoneInfo | None = None) -> str:
        local_start = self.start.astimezone(tz) if tz else self.start

        if self.all_day:
            when = f"{local_start.date()} (all day)"
        else:
            minutes = int(self.duration.total_seconds() // 60)
            when = f"{local_start:%H:%M} ({minutes}m)"

        line = f"{when} {self.summary or '(no title)'}"
        if self.location:
            line += f" — {self.location}"
        if self.attendees:
            shown = ", ".join(a.split("@")[0] for a in self.attendees[:4])
            more = f" +{len(self.attendees) - 4}" if len(self.attendees) > 4 else ""
            line += f" — with {shown}{more}"
        if self.recurrence_note:
            line += f" [{self.recurrence_note}]"
        return line


def _as_datetime(value, *, tz: ZoneInfo, end_of_day: bool = False) -> tuple[datetime, bool]:
    """Normalise an iCalendar date-or-datetime to an aware datetime.

    Returns ``(datetime, all_day)``. All-day events arrive as plain dates, and
    treating one as midnight-UTC shifts it into the wrong day for anyone not in
    UTC — which for a morning brief means the wrong day entirely.
    """
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=tz)), False
    if isinstance(value, date):
        moment = time(23, 59, 59) if end_of_day else time(0, 0)
        return datetime.combine(value, moment, tzinfo=tz), True
    raise TypeError(f"unsupported date value: {value!r}")


def _addresses(component, key: str) -> list[str]:
    raw = component.get(key)
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for value in values:
        text = str(value)
        out.append(text.removeprefix("mailto:").removeprefix("MAILTO:").strip())
    return [v for v in out if v]


def parse_events(ics: str | bytes, *, tz: ZoneInfo | None = None) -> list[Event]:
    """Parse VEVENTs from an iCalendar document.

    Never raises on a malformed calendar: one bad event must not empty someone's
    whole day. Unparseable components are logged and skipped.
    """
    tz = tz or UTC
    try:
        calendar = Calendar.from_ical(ics)
    except Exception:  # noqa: BLE001
        log.warning("calendar.parse_failed")
        return []

    events: list[Event] = []
    for component in calendar.walk("VEVENT"):
        try:
            events.append(_to_event(component, tz))
        except Exception:  # noqa: BLE001
            log.warning("calendar.event_skipped", uid=str(component.get("UID", "?")))
    return events


def _to_event(component, tz: ZoneInfo) -> Event:
    start, all_day = _as_datetime(component.decoded("DTSTART"), tz=tz)

    if "DTEND" in component:
        end, _ = _as_datetime(component.decoded("DTEND"), tz=tz, end_of_day=all_day)
    elif "DURATION" in component:
        end = start + component.decoded("DURATION")
    else:
        # RFC 5545: no DTEND and no DURATION means a whole day for a date, or a
        # zero-length instant for a datetime.
        end = start + (timedelta(days=1) if all_day else timedelta())

    rrule = component.get("RRULE")
    note = ""
    if rrule is not None:
        freq = str(rrule.get("FREQ", [""])[0]).upper()
        if freq not in SUPPORTED_FREQ:
            note = f"repeats {freq.lower() or 'irregularly'}; occurrences not expanded"

    return Event(
        uid=str(component.get("UID", "")),
        summary=str(component.get("SUMMARY", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(component.get("LOCATION", "")),
        attendees=_addresses(component, "ATTENDEE"),
        organizer=(_addresses(component, "ORGANIZER") or [""])[0],
        status=str(component.get("STATUS", "")),
        recurrence_note=note,
    )


def expand(
    events: list[Event], *, start: datetime, end: datetime, ics: str | bytes | None = None
) -> list[Event]:
    """Expand recurrences into concrete instances within a window.

    Handles DAILY, WEEKLY (with BYDAY) and MONTHLY-by-date, honouring COUNT and
    UNTIL. Deliberately does **not** attempt BYSETPOS, BYMONTHDAY lists, or
    "third Thursday" style rules: those are where a naive expander starts
    inventing meetings, and an invented meeting in a brief is worse than a
    missing one. Unsupported rules keep their note and their original instance.
    """
    if ics is None:
        return [e for e in events if e.overlaps(start, end)]

    try:
        calendar = Calendar.from_ical(ics)
    except Exception:  # noqa: BLE001
        return [e for e in events if e.overlaps(start, end)]

    expanded: list[Event] = []
    for component in calendar.walk("VEVENT"):
        try:
            base = _to_event(component, start.tzinfo or UTC)
        except Exception:  # noqa: BLE001
            continue

        rrule = component.get("RRULE")
        if rrule is None:
            if base.overlaps(start, end):
                expanded.append(base)
            continue

        freq = str(rrule.get("FREQ", [""])[0]).upper()
        if freq not in SUPPORTED_FREQ:
            if base.overlaps(start, end):
                expanded.append(base)
            continue

        expanded.extend(_expand_rule(base, rrule, freq, start, end))

    return sorted(expanded, key=lambda e: e.start)


def _expand_rule(base: Event, rrule, freq: str, window_start: datetime, window_end: datetime):
    interval = int(rrule.get("INTERVAL", [1])[0])
    count = rrule.get("COUNT")
    count = int(count[0]) if count else None
    until = rrule.get("UNTIL")
    until = until[0] if until else None
    if isinstance(until, date) and not isinstance(until, datetime):
        until = datetime.combine(until, time(23, 59, 59), tzinfo=base.start.tzinfo)

    by_day = [str(d).upper()[-2:] for d in rrule.get("BYDAY", [])]
    wanted_weekdays = {_WEEKDAY_INDEX[d] for d in by_day if d in _WEEKDAY_INDEX}

    duration = base.duration
    produced: list[Event] = []
    emitted = 0
    cursor = base.start
    # Bounded so a malformed rule cannot spin: two years of daily occurrences is
    # far beyond any window a brief asks for.
    for _ in range(800):
        if cursor > window_end:
            break
        if until and cursor > until:
            break
        if count is not None and emitted >= count:
            break

        matches = not wanted_weekdays or cursor.weekday() in wanted_weekdays
        if matches:
            emitted += 1
            instance_end = cursor + duration
            if cursor < window_end and instance_end > window_start:
                produced.append(
                    Event(
                        uid=base.uid,
                        summary=base.summary,
                        start=cursor,
                        end=instance_end,
                        all_day=base.all_day,
                        location=base.location,
                        attendees=list(base.attendees),
                        organizer=base.organizer,
                        status=base.status,
                        recurring=cursor != base.start,
                    )
                )

        cursor = _advance(cursor, freq, interval, wanted_weekdays)

    return produced


def _advance(cursor: datetime, freq: str, interval: int, wanted_weekdays: set[int]) -> datetime:
    if freq == "DAILY":
        return cursor + timedelta(days=interval)
    if freq == "WEEKLY":
        # With BYDAY, step one day at a time and let the weekday filter decide;
        # jumping a whole interval would skip the other days in the same week.
        return cursor + (timedelta(days=1) if wanted_weekdays else timedelta(weeks=interval))
    # MONTHLY, by date.
    month = cursor.month + interval
    year = cursor.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(cursor.day, _days_in_month(year, month))
    return cursor.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def free_slots(
    events: list[Event],
    *,
    day: date,
    tz: ZoneInfo,
    start_hour: int = 9,
    end_hour: int = 18,
    slot_minutes: int = 60,
) -> list[str]:
    """Working-hour slots with nothing scheduled in them.

    Computed from actual event times rather than by reading rendered text, and
    returning *only* times — the A2A disclosure contract may permit free/busy
    while forbidding meeting subjects, and this function cannot leak one while
    answering the other because it never sees them.
    """
    slots: list[str] = []
    step = timedelta(minutes=slot_minutes)
    cursor = datetime.combine(day, time(start_hour, 0), tzinfo=tz)
    finish = datetime.combine(day, time(end_hour, 0), tzinfo=tz)

    while cursor < finish:
        slot_end = cursor + step
        if not any(e.overlaps(cursor, slot_end) for e in events):
            slots.append(f"{cursor:%H:%M}")
        cursor = slot_end

    return slots
