"""Fixture enterprise connectors.

Stands in for the Wave-1 connector set (mail, calendar, tasks, incidents) so the
brief, the approval flow, and the agent loop can be exercised and demonstrated
without credentials to a real Exchange or Jira estate.

The data is shaped like a real morning: a genuine incident, a meeting that needs
preparation, a ticket that has gone stale, one message from outside the company,
and enough noise that ranking matters. Demos built on three tidy items prove
nothing about a product whose job is triage.

Every tool here declares its risk class and whether it surfaces externally
authored text, exactly as a certified connector must (F3.8).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

_NOW = datetime(2026, 7, 27, 8, 30, tzinfo=UTC)


def _ago(**kwargs: Any) -> str:
    return (_NOW - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M")


MAILBOX: list[dict[str, Any]] = [
    {
        "id": "m-1",
        "from": "ops-alerts@corp.example",
        "external": False,
        "subject": "P1: payment gateway latency above threshold",
        "received": _ago(hours=9),
        "unread": True,
        "body": "Automated alert. p99 latency 4200ms since 23:40. Incident INC0010001 opened.",
    },
    {
        "id": "m-2",
        "from": "cfo@corp.example",
        "external": False,
        "subject": "Q3 budget review moved to Thursday 14:00",
        "received": _ago(hours=14),
        "unread": True,
        "body": "Moved to Thursday. Please bring the department forecast and headcount plan.",
    },
    {
        "id": "m-3",
        "from": "procurement@supplier-external.example",
        "external": True,
        "subject": "Re: invoice INV-88213 reconciliation",
        "received": _ago(hours=3),
        "unread": True,
        "body": "We show a 4,200 EUR discrepancy on INV-88213. Could you confirm before Friday?",
    },
    {
        "id": "m-4",
        "from": "hr-noreply@corp.example",
        "external": False,
        "subject": "Annual leave policy update",
        "received": _ago(days=1),
        "unread": True,
        "body": "The carry-over limit changes from 10 to 5 days effective September.",
    },
    {
        "id": "m-5",
        "from": "newsletter@vendor-external.example",
        "external": True,
        "subject": "10 trends in observability for 2026",
        "received": _ago(days=1),
        "unread": True,
        "body": "Our annual roundup of what matters in monitoring.",
    },
]

TASKS: list[dict[str, Any]] = [
    {
        "key": "PAY-1182",
        "title": "Reconcile supplier invoice INV-88213",
        "status": "In Progress",
        "assignee": "alice",
        "due": "2026-07-31",
        "updated": _ago(days=6),
    },
    {
        "key": "PAY-1190",
        "title": "Add gateway latency alert to on-call runbook",
        "status": "To Do",
        "assignee": "alice",
        "due": "2026-07-28",
        "updated": _ago(days=2),
    },
    {
        "key": "PAY-1204",
        "title": "Q3 forecast input for finance",
        "status": "To Do",
        "assignee": "alice",
        "due": "2026-07-30",
        "updated": _ago(hours=20),
    },
]

INCIDENTS: list[dict[str, Any]] = [
    {
        "id": "INC0010001",
        "severity": "P1",
        "title": "Card settlement delayed for 2,300 transactions",
        "status": "Investigating",
        "opened": _ago(hours=9),
        "owner": "alice",
        "note": "Acquirer soft-declines are not being retried. Revenue impact accruing.",
    },
    {
        "id": "INC0010002",
        "severity": "P3",
        "title": "Refund API returning 500 for merchant 4471",
        "status": "Monitoring",
        "opened": _ago(days=3),
        "owner": "bora",
        "note": "Intermittent since the 06:00 deploy. Waiting on the merchant.",
    },
]

CALENDAR: list[dict[str, Any]] = [
    {
        "at": "09:30",
        "title": "Incident review — INC0010001",
        "attendees": ["alice", "bora", "sre-oncall"],
        "duration_min": 30,
    },
    {
        "at": "11:00",
        "title": "1:1 with manager",
        "attendees": ["alice", "manager"],
        "duration_min": 30,
    },
    {
        "at": "14:00",
        "title": "Q3 budget review (moved)",
        "attendees": ["alice", "cfo", "finance-team"],
        "duration_min": 60,
    },
]


def build_mail_source(*, fail: bool = False) -> InMemoryToolSource:
    source = InMemoryToolSource("mail")

    async def list_unread(args: dict) -> ToolResult:
        if fail:
            return ToolResult.failure("EWS endpoint unreachable: connection refused")
        limit = int(args.get("limit", 10))
        rows = [m for m in MAILBOX if m["unread"]][:limit]
        rendered = "\n".join(
            f"[{m['id']}] {m['received']} from {m['from']}"
            f"{' (EXTERNAL SENDER)' if m['external'] else ''}\n"
            f"    {m['subject']}\n    {m['body']}"
            for m in rows
        )
        return ToolResult.success(rendered or "No unread mail.", {"count": len(rows)})

    async def send_reply(args: dict) -> ToolResult:
        return ToolResult.success(f"Reply queued to {args.get('to')}")

    source.register(
        "list_unread",
        list_unread,
        description="List unread messages in the user's mailbox.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max messages."}},
        },
        risk=RiskClass.READ,
        # Anyone on the internet can write here.
        returns_untrusted_content=True,
    )
    source.register(
        "send_reply",
        send_reply,
        description="Send a reply to a message.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "body"],
        },
        risk=RiskClass.EXTERNAL_FACING,
    )
    return source


def build_tasks_source(*, fail: bool = False) -> InMemoryToolSource:
    source = InMemoryToolSource("tasks")

    async def my_open_issues(_args: dict) -> ToolResult:
        if fail:
            return ToolResult.failure("Jira Data Center returned 503")
        rendered = "\n".join(
            f"[{t['key']}] {t['status']:<12} due {t['due']}  (last updated {t['updated']})\n"
            f"    {t['title']}"
            for t in TASKS
        )
        return ToolResult.success(rendered, {"count": len(TASKS)})

    async def update_issue(args: dict) -> ToolResult:
        return ToolResult.success(f"{args.get('key')} updated")

    source.register(
        "my_open_issues",
        my_open_issues,
        description="List the user's open issues.",
        risk=RiskClass.READ,
    )
    source.register(
        "update_issue",
        update_issue,
        description="Update an issue's status or fields.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "status": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["key"],
        },
        risk=RiskClass.REVERSIBLE_WRITE,
    )
    return source


def build_incidents_source(*, fail: bool = False) -> InMemoryToolSource:
    source = InMemoryToolSource("incidents")

    async def active(_args: dict) -> ToolResult:
        if fail:
            return ToolResult.failure("ITSM connector timed out")
        rendered = "\n".join(
            f"[{i['id']}] {i['severity']} {i['status']:<14} opened {i['opened']} "
            f"owner {i['owner']}\n    {i['title']}\n    {i['note']}"
            for i in INCIDENTS
        )
        return ToolResult.success(rendered, {"count": len(INCIDENTS)})

    source.register("active", active, description="List active incidents.", risk=RiskClass.READ)
    return source


def build_calendar_source(*, fail: bool = False) -> InMemoryToolSource:
    source = InMemoryToolSource("calendar")

    async def today(_args: dict) -> ToolResult:
        if fail:
            return ToolResult.failure("CalDAV server unreachable")
        rendered = "\n".join(
            f"{e['at']} ({e['duration_min']}m) {e['title']} — with {', '.join(e['attendees'])}"
            for e in CALENDAR
        )
        return ToolResult.success(rendered, {"count": len(CALENDAR)})

    source.register("today", today, description="Today's calendar entries.", risk=RiskClass.READ)
    return source


ALL_BUILDERS = {
    "mail": build_mail_source,
    "tasks": build_tasks_source,
    "incidents": build_incidents_source,
    "calendar": build_calendar_source,
}


def build_all(*, failing: set[str] | None = None) -> list[InMemoryToolSource]:
    """Build the full demo estate, optionally with some connectors failing."""
    failing = failing or set()
    return [builder(fail=name in failing) for name, builder in ALL_BUILDERS.items()]
