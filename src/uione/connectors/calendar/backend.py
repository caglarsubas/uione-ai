"""Calendar backend protocol, and a CalDAV implementation.

CalDAV (RFC 4791) reaches Nextcloud, Radicale, Baikal, SOGo, Zimbra and anything
else that speaks the standard — which is most of the on-premise calendar estate
that has no MCP server and no prospect of one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import structlog

from uione.connectors.calendar.events import Event, expand, parse_events

log = structlog.get_logger(__name__)


class CalendarError(RuntimeError):
    """Any failure reaching or reading the calendar."""


@dataclass
class CalendarAccount:
    url: str
    username: str = ""
    password: str = ""
    timezone: str = "UTC"
    timeout_s: float = 20.0
    verify_tls: bool = True

    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001
            log.warning("calendar.unknown_timezone", timezone=self.timezone)
            return UTC


class CalendarBackend(Protocol):
    async def events_between(self, start: datetime, end: datetime) -> list[Event]: ...


@dataclass
class InMemoryCalendarBackend:
    """A calendar in a list. Used by tests and offline demos."""

    events: list[Event] = field(default_factory=list)
    fail_with: str | None = None

    async def events_between(self, start: datetime, end: datetime) -> list[Event]:
        if self.fail_with:
            raise CalendarError(self.fail_with)
        return sorted((e for e in self.events if e.overlaps(start, end)), key=lambda e: e.start)


#: A calendar-query REPORT. Asking the server to filter by time range is the
#: difference between fetching one day and fetching someone's entire history.
_QUERY = """<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start}" end="{end}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""

_CALENDAR_DATA = re.compile(
    r"<[^>]*calendar-data[^>]*>(.*?)</[^>]*calendar-data>", re.DOTALL | re.IGNORECASE
)


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&amp;", "&")
    )


class CalDavBackend:
    def __init__(
        self, account: CalendarAccount, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._account = account
        self._client = client

    async def events_between(self, start: datetime, end: datetime) -> list[Event]:
        account = self._account
        body = _QUERY.format(
            start=start.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
            end=end.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
        )

        client = self._client or httpx.AsyncClient(
            timeout=account.timeout_s, verify=account.verify_tls
        )
        try:
            response = await client.request(
                "REPORT",
                account.url,
                content=body.encode(),
                headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
                auth=(account.username, account.password) if account.username else None,
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"calendar unreachable: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code == 401:
            raise CalendarError("calendar rejected the credentials")
        if response.is_error:
            raise CalendarError(f"calendar returned {response.status_code}")

        events: list[Event] = []
        for match in _CALENDAR_DATA.finditer(response.text):
            ics = _unescape(match.group(1)).strip()
            if not ics:
                continue
            # Expand here rather than after merging: recurrence rules belong to
            # the document they came from.
            events.extend(expand(parse_events(ics, tz=account.tz()), start=start, end=end, ics=ics))

        # A server may return the same recurring event under several hrefs.
        unique: dict[tuple, Event] = {}
        for event in events:
            unique[(event.uid, event.start)] = event
        return sorted(unique.values(), key=lambda e: e.start)


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=1)
