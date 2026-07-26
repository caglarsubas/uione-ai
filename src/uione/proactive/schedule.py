"""Schedules for proactive work.

Deliberately not a cron implementation. The jobs this product runs are
"weekday mornings before I arrive" and "Friday afternoon", and a full cron parser
would be more expression than anyone needs plus a class of scheduling bug nobody
wants to debug at 07:00.

The one non-obvious feature is **jitter**. Five hundred employees all scheduled
for 08:00 would arrive at the GPU simultaneously, and the last of them would get
their morning brief around lunchtime. Spreading start times across a window is
the difference between a fleet that works and one that needs several times the
hardware.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class JobKind(StrEnum):
    MORNING_BRIEF = "morning_brief"
    EVENING_SUMMARY = "evening_summary"
    WEEKLY_REVIEW = "weekly_review"


#: Monday–Friday, as ``datetime.weekday()`` numbers.
WEEKDAYS = frozenset({0, 1, 2, 3, 4})


@dataclass(frozen=True)
class Schedule:
    """When a job should run, in the user's own timezone.

    Timezone matters more than it looks: a "morning" brief computed in UTC
    arrives in the middle of the night for half an organisation, and the whole
    premise is that it is ready when *that person* starts work.
    """

    at: time = time(7, 30)
    days: frozenset[int] = WEEKDAYS
    timezone: str = "UTC"

    #: Seconds of spread applied per user, so a fleet does not stampede.
    jitter_s: int = 900

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def next_run_after(self, now: datetime, *, user_id: str = "") -> datetime:
        """First scheduled moment strictly after ``now``.

        Jitter is derived from the user id rather than randomised, so a given
        user's brief lands at a consistent time each day. A brief that arrives at
        07:31 one morning and 07:52 the next is one people stop relying on.
        """
        local = now.astimezone(self.tz())
        offset = timedelta(seconds=self._jitter_for(user_id))

        for day_ahead in range(8):
            candidate_date = (local + timedelta(days=day_ahead)).date()
            candidate = datetime.combine(candidate_date, self.at, tzinfo=self.tz()) + offset
            if candidate.weekday() in self.days and candidate > local:
                return candidate

        raise ValueError("schedule has no valid days")

    def _jitter_for(self, user_id: str) -> int:
        if not self.jitter_s or not user_id:
            return 0
        digest = hashlib.sha256(user_id.encode()).digest()
        return int.from_bytes(digest[:4], "big") % self.jitter_s


@dataclass
class ScheduledJob:
    """One recurring piece of work for one user."""

    user_id: str
    kind: JobKind = JobKind.MORNING_BRIEF
    schedule: Schedule = field(default_factory=Schedule)
    enabled: bool = True
    last_run: datetime | None = None
    last_error: str | None = None
    runs: int = 0
    failures: int = 0

    @property
    def key(self) -> str:
        return f"{self.user_id}:{self.kind}"

    def next_run(self, now: datetime) -> datetime:
        return self.schedule.next_run_after(now, user_id=self.user_id)

    def is_due(self, now: datetime) -> bool:
        """Whether this job should run now.

        A job with no history is *not* immediately due. Otherwise deploying the
        service at 15:00 would fire everyone's morning brief on the spot, which
        is both a GPU spike and a confusing first impression.
        """
        if not self.enabled:
            return False
        reference = self.last_run or (now - timedelta(seconds=1))
        return self.schedule.next_run_after(reference, user_id=self.user_id) <= now
