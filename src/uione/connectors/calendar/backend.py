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

    async def create_event(self, uid: str, ics: str) -> str:
        """Store a new event. Returns the URL it was written to."""
        ...


@dataclass
class InMemoryCalendarBackend:
    """A calendar in a list. Used by tests and offline demos."""

    events: list[Event] = field(default_factory=list)
    fail_with: str | None = None

    async def events_between(self, start: datetime, end: datetime) -> list[Event]:
        if self.fail_with:
            raise CalendarError(self.fail_with)
        return sorted((e for e in self.events if e.overlaps(start, end)), key=lambda e: e.start)

    async def create_event(self, uid: str, ics: str) -> str:
        """Parse and store, rather than keeping the text.

        Parsing here is deliberate: it means a malformed VEVENT fails in the
        fixture exactly as it would against a server, instead of a demo working
        and the real thing rejecting it.
        """
        if self.fail_with:
            raise CalendarError(self.fail_with)
        if any(e.uid == uid for e in self.events):
            raise CalendarError("an event with that identifier already exists")

        parsed = parse_events(ics)
        if not parsed:
            raise CalendarError("no event found in the submitted calendar data")
        self.events.extend(parsed)
        return f"memory://{uid}.ics"


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

    async def create_event(self, uid: str, ics: str) -> str:
        """PUT a new event, refusing to overwrite an existing one.

        `If-None-Match: *` is the whole point. Without it a UID collision — from
        a retry, a duplicate submission, or two assistants acting at once —
        silently replaces somebody's existing meeting, and the only evidence is
        that it is no longer in their calendar.
        """
        account = self._account
        url = account.url.rstrip("/") + f"/{uid}.ics"

        client = self._client or httpx.AsyncClient(
            timeout=account.timeout_s, verify=account.verify_tls
        )
        try:
            response = await client.request(
                "PUT",
                url,
                content=ics.encode("utf-8"),
                headers={
                    "Content-Type": "text/calendar; charset=utf-8",
                    "If-None-Match": "*",
                },
                auth=(account.username, account.password) if account.username else None,
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"calendar unreachable: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code == 401:
            raise CalendarError("calendar rejected the credentials")
        if response.status_code == 412:
            # The precondition we set. Reported as a conflict rather than a
            # generic failure so a caller knows retrying with the same uid
            # cannot work.
            raise CalendarError("an event with that identifier already exists")
        if response.is_error:
            raise CalendarError(f"calendar refused the event ({response.status_code})")

        return url

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
