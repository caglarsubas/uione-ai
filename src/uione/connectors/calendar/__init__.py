"""Calendar connector — CalDAV/iCalendar."""

from uione.connectors.calendar.backend import (
    CalDavBackend,
    CalendarAccount,
    CalendarBackend,
    CalendarError,
    InMemoryCalendarBackend,
    day_bounds,
)
from uione.connectors.calendar.compose import build_event, escape_text, fold, valid_address
from uione.connectors.calendar.events import (
    SUPPORTED_FREQ,
    Event,
    expand,
    free_slots,
    parse_events,
)
from uione.connectors.calendar.source import build_calendar_source

__all__ = [
    "build_event",
    "escape_text",
    "fold",
    "valid_address",
    "SUPPORTED_FREQ",
    "CalDavBackend",
    "CalendarAccount",
    "CalendarBackend",
    "CalendarError",
    "Event",
    "InMemoryCalendarBackend",
    "build_calendar_source",
    "day_bounds",
    "expand",
    "free_slots",
    "parse_events",
]
