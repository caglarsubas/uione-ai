"""Creating meetings against a real CalDAV server.

Radicale is a dev dependency for the same reason the MCP SDK is: writing
iCalendar is unforgiving, and a fixture that accepts our output proves only that
we agree with ourselves. It is pure Python, so this runs in CI rather than being
an opt-in nobody remembers to switch on.

The failure being guarded against is specific. Reading iCalendar is forgiving;
writing it is not, and a server's rejection is typically a bare `400` with no
body. A connector whose only evidence is a mock will produce that `400` on the
first real meeting somebody tries to book.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from uione.connectors.calendar import CalDavBackend, CalendarAccount, build_calendar_source
from uione.mcphub import RiskClass

radicale = pytest.importorskip("radicale", reason="radicale provides the real CalDAV server")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def caldav_url(tmp_path_factory) -> str:
    """A throwaway Radicale, with one calendar collection."""
    root = tmp_path_factory.mktemp("radicale")
    collections = root / "collections"
    collections.mkdir()
    port = _free_port()

    config = root / "config"
    config.write_text(
        "\n".join(
            [
                "[server]",
                f"hosts = 127.0.0.1:{port}",
                "[auth]",
                "type = none",
                "[storage]",
                f"filesystem_folder = {collections}",
                "",
            ]
        )
    )

    process = subprocess.Popen(
        # -u so the server's output is not buffered away if this needs debugging.
        [sys.executable, "-u", "-m", "radicale", "--config", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(base, timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:  # pragma: no cover — only on a very slow machine
        process.kill()
        pytest.skip("radicale did not start")

    url = f"{base}/uione/calendar/"
    created = httpx.request(
        "MKCOL",
        url,
        # Radicale derives the collection's owner from the credentials, even
        # with `auth type = none`. Without them the request is anonymous, the
        # path under /uione/ is never created, and every later call 404s —
        # which reads as "the connector is broken" rather than "the fixture is".
        auth=("uione", ""),
        headers={"Content-Type": "application/xml"},
        content=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:mkcol xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:set><D:prop>'
            "<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>"
            "</D:prop></D:set></D:mkcol>"
        ),
        timeout=5,
    )
    assert created.status_code in (201, 405), (
        f"could not create the calendar: {created.status_code}"
    )

    yield url

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


@pytest.fixture
def calendar(caldav_url: str):
    account = CalendarAccount(
        url=caldav_url, username="uione", password="", timezone="Europe/Amsterdam"
    )
    return build_calendar_source(
        CalDavBackend(account), timezone="Europe/Amsterdam", organizer="uione@corp.example"
    )


def _slot(days: int = 1, hour: int = 9) -> datetime:
    """A time on a future day, so tests do not fight the current calendar."""
    return (datetime.now(UTC) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


# -- the round trip --------------------------------------------------------


async def test_a_meeting_is_accepted_by_a_real_server(calendar) -> None:
    start = _slot(days=2, hour=9)

    result = await calendar.call(
        "propose_meeting",
        {
            "title": "Settlement incident review",
            "start": start.isoformat(),
            "minutes": 45,
            "attendees": ["bora@corp.example"],
        },
    )

    assert result.ok, result.error
    assert result.structured["uid"]


async def test_the_meeting_reads_back_with_its_details_intact(calendar) -> None:
    """The assertion a mock cannot make: the server parsed what we wrote."""
    start = _slot(days=3, hour=11)

    await calendar.call(
        "propose_meeting",
        {
            "title": "Budget review, headcount; and hiring",
            "start": start.isoformat(),
            "minutes": 30,
            "attendees": ["bora@corp.example", "alice@corp.example"],
            "location": "Room 4",
        },
    )
    upcoming = await calendar.call("upcoming", {"days": 5})

    # Escaping survived a real parser: the comma and semicolon are still in the
    # title rather than having split it into other properties.
    assert "Budget review, headcount; and hiring" in upcoming.content
    assert "Room 4" in upcoming.content
    assert "bora" in upcoming.content


async def test_a_clashing_meeting_is_refused(calendar) -> None:
    """Proposing on top of an existing meeting is the fastest way for somebody
    to stop trusting this."""
    start = _slot(days=4, hour=14)
    first = await calendar.call(
        "propose_meeting",
        {
            "title": "Existing commitment",
            "start": start.isoformat(),
            "minutes": 60,
            "attendees": ["bora@corp.example"],
        },
    )
    assert first.ok, first.error

    clash = await calendar.call(
        "propose_meeting",
        {
            "title": "Double booking",
            "start": (start + timedelta(minutes=15)).isoformat(),
            "minutes": 30,
            "attendees": ["bora@corp.example"],
        },
    )

    assert not clash.ok
    assert "not free" in (clash.error or "")


async def test_the_same_identifier_cannot_overwrite_an_existing_event(
    caldav_url: str, calendar
) -> None:
    """`If-None-Match: *` is the whole point of the PUT.

    Without it a UID collision — a retry, a duplicate submission, two assistants
    at once — silently replaces somebody's meeting, and the only evidence is
    that it is gone.
    """
    from uione.connectors.calendar.compose import build_event

    account = CalendarAccount(url=caldav_url, username="uione", password="")
    backend = CalDavBackend(account)
    start = _slot(days=5, hour=8)

    uid, ics = build_event(
        summary="First",
        start=start,
        end=start + timedelta(minutes=30),
        organizer="uione@corp.example",
        attendees=["bora@corp.example"],
    )
    await backend.create_event(uid, ics)

    _, second = build_event(
        summary="Replacement",
        start=start,
        end=start + timedelta(minutes=30),
        organizer="uione@corp.example",
        attendees=["bora@corp.example"],
        uid=uid,
    )

    from uione.connectors.calendar import CalendarError

    with pytest.raises(CalendarError, match="already exists"):
        await backend.create_event(uid, second)


# -- what it refuses -------------------------------------------------------


async def test_an_invented_address_is_refused_rather_than_dropped(calendar) -> None:
    """A model that hallucinates a colleague's address must not have a calendar
    server email it — and silently removing the bad one would send the meeting
    to a subset while reporting success."""
    result = await calendar.call(
        "propose_meeting",
        {
            "title": "Sync",
            "start": _slot(days=6).isoformat(),
            "minutes": 30,
            "attendees": ["bora@corp.example", "probably-alice"],
        },
    )

    assert not result.ok
    assert "probably-alice" in (result.error or "")


async def test_inviting_the_whole_company_is_refused(calendar) -> None:
    result = await calendar.call(
        "propose_meeting",
        {
            "title": "All hands",
            "start": _slot(days=7).isoformat(),
            "minutes": 30,
            "attendees": [f"person{i}@corp.example" for i in range(50)],
        },
    )

    assert not result.ok
    assert "limit" in (result.error or "")


async def test_an_absurd_duration_is_refused(calendar) -> None:
    """A parsing slip turning "30 minutes" into a nine-hour block appears in
    everyone's calendar, not just the caller's."""
    result = await calendar.call(
        "propose_meeting",
        {
            "title": "Marathon",
            "start": _slot(days=8).isoformat(),
            "minutes": 60 * 24,
            "attendees": ["bora@corp.example"],
        },
    )

    assert not result.ok


async def test_a_meeting_with_no_attendees_is_refused(calendar) -> None:
    result = await calendar.call(
        "propose_meeting",
        {"title": "Alone", "start": _slot(days=9).isoformat(), "attendees": []},
    )

    assert not result.ok


async def test_attendees_may_arrive_as_a_comma_separated_string(calendar) -> None:
    """Models pass a string about as often as a list; failing on that is failing
    on a formatting preference."""
    result = await calendar.call(
        "propose_meeting",
        {
            "title": "Flexible",
            "start": _slot(days=10).isoformat(),
            "minutes": 30,
            "attendees": "bora@corp.example, alice@corp.example",
        },
    )

    assert result.ok, result.error
    assert len(result.structured["attendees"]) == 2


async def test_a_bad_start_time_names_the_format(calendar) -> None:
    result = await calendar.call(
        "propose_meeting",
        {"title": "Whenever", "start": "next tuesday", "attendees": ["bora@corp.example"]},
    )

    assert not result.ok
    assert "ISO 8601" in (result.error or "")


# -- classification --------------------------------------------------------


async def test_proposing_a_meeting_is_external_facing(calendar) -> None:
    """It emails people and puts an entry in their calendar. Neither is taken
    back by deleting the event afterwards, so it is classified by what it does
    to *them* rather than by whether our row can be removed."""
    specs = {s.tool: s for s in await calendar.list_tools()}

    assert specs["propose_meeting"].risk is RiskClass.EXTERNAL_FACING


async def test_the_attendee_list_is_visible_to_the_egress_check(calendar) -> None:
    """Attendees are passed as a plain list precisely so the existing egress
    policy sees them without a special case."""
    from uione.governance import EgressPolicy

    policy = EgressPolicy(internal_domains=frozenset({"corp.example"}))

    inside = policy.check({"attendees": ["bora@corp.example"]})
    outside = policy.check({"attendees": ["someone@competitor.example"]})

    assert inside == []
    assert outside, "an invitation to an unapproved domain must be caught"
