"""Proactive intelligence — briefs, schedules, and the work that runs unasked."""

from uione.proactive.brief import (
    BRIEF_SYSTEM_PROMPT,
    DEFAULT_SOURCES,
    Brief,
    BriefGenerator,
    BriefSource,
    SectionResult,
)
from uione.proactive.schedule import WEEKDAYS, JobKind, Schedule, ScheduledJob
from uione.proactive.scheduler import BriefStore, Scheduler, SchedulerStats, StoredBrief
from uione.proactive.weekly import (
    Movement,
    WeeklyReport,
    WeeklyReviewGenerator,
    compare_weeks,
)

__all__ = [
    "Movement",
    "WeeklyReport",
    "WeeklyReviewGenerator",
    "compare_weeks",
    "BRIEF_SYSTEM_PROMPT",
    "DEFAULT_SOURCES",
    "WEEKDAYS",
    "Brief",
    "BriefGenerator",
    "BriefSource",
    "BriefStore",
    "JobKind",
    "Schedule",
    "ScheduledJob",
    "Scheduler",
    "SchedulerStats",
    "SectionResult",
    "StoredBrief",
]
