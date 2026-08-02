"""ServiceNow incidents.

The incident connector the product brief asks for, against the system most
enterprises actually run. ServiceNow is reachable for free — a Personal
Developer Instance costs nothing — but it needs an account, and creating one is
the operator's decision rather than this repo's. So this is written from the
published Table API documentation, tested against a mock that reproduces the
documented behaviour, and labelled accordingly in `docs/CONNECTORS.md`.

**The design problem this connector exists to solve.**

ServiceNow returns the same field in three different shapes depending on a query
parameter. `state` is `"2"`, or `"In Progress"`, or
`{"display_value": "In Progress", "value": "2"}`. Reference fields like
`assigned_to` are `{"link", "value"}`, or a display name, or both.

A connector that picks one shape and hard-codes it does not crash when it meets
another — it reports every incident's state as the wrong thing, silently, until
somebody notices the assistant saying "resolved" about an open outage. So:

* every request asks for `sysparm_display_value=all`, which is the only form
  that carries *both* the machine value and the human label;
* every read goes through `field_value` / `field_label`, which accept all three
  shapes, because an instance can be configured to override the default and a
  connector should not be one setting away from lying.

**Priority is not urgency.** ServiceNow computes `priority` from `impact` and
`urgency` through a configurable matrix. This connector reads `priority` and
never recomputes it: reproducing that matrix here would mean a second source of
truth that disagrees with the instance the first time someone edits it.

**Work notes append, they never replace.** The field is a journal. A connector
that treats it as a string to overwrite destroys the incident's history, and no
single call reveals it.
"""

from __future__ import annotations

from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult, VerificationPlan

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 20

#: The states that mean somebody is still expected to do something.
ACTIVE_STATES = frozenset({"1", "2", "3"})

STATE_LABELS = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}

#: What a person is allowed to ask for by name. Deliberately excludes "Closed"
#: and "Canceled": closing an incident in ServiceNow can trigger downstream
#: workflow — surveys, SLA clocks, change records — and that is a decision for a
#: person with the context, not a summarisation loop.
SETTABLE_STATES = {
    "in_progress": "2",
    "on_hold": "3",
    "resolved": "6",
}


def servicenow_config(
    instance_url: str,
    username: str,
    password: str,
    *,
    verify_tls: bool = True,
    timeout_s: float = 20.0,
) -> VendorConfig:
    """Basic auth, which is what a PDI gives you.

    OAuth is the right answer for production and is a configuration change here,
    not a code change: the spine takes whatever `Auth` it is handed.
    """
    return VendorConfig(
        name="servicenow",
        base_url=instance_url.rstrip("/"),
        auth=Auth(scheme="basic", username=username, secret=password),
        verify_tls=verify_tls,
        timeout_s=timeout_s,
        extra_headers={"Content-Type": "application/json"},
    )


def field_value(field: Any) -> str:
    """The machine value, whichever of the three shapes arrived.

    Not defensive programming for its own sake: an instance's default display
    mode is a configuration setting, so a connector that handles one shape is
    one administrator away from misreporting every record it reads.
    """
    if isinstance(field, dict):
        return str(field.get("value", ""))
    return "" if field is None else str(field)


def field_label(field: Any, labels: dict[str, str] | None = None) -> str:
    """The human label, whichever shape arrived.

    Falls back to our own table only when the instance sent a bare value, so an
    instance that has customised its choice list wins over this file.
    """
    if isinstance(field, dict):
        if display := field.get("display_value"):
            return str(display)
        return (labels or {}).get(str(field.get("value", "")), str(field.get("value", "")))
    text = "" if field is None else str(field)
    return (labels or {}).get(text, text)


class ServiceNowIncidents:
    def __init__(self, config: VendorConfig, *, user: str = "", **kwargs: Any) -> None:
        self._client = VendorClient(config, **kwargs)
        self._user = user

    async def aclose(self) -> None:
        await self._client.aclose()

    async def my_incidents(self, *, limit: int = DEFAULT_LIMIT, user: str = "") -> list[dict]:
        """Active incidents assigned to this person, most recently touched first.

        `active=true` rather than enumerating states: ServiceNow maintains
        `active` itself, and an instance that has added a custom state keeps
        working. Listing state numbers here would quietly drop those incidents.
        """
        who = user or self._user
        query = "active=true"
        if who:
            query += f"^assigned_to={who}"
        query += "^ORDERBYDESCsys_updated_on"

        payload = await self._client.get(
            "/api/now/table/incident",
            params={
                "sysparm_query": query,
                "sysparm_limit": limit,
                # The only mode carrying both the value and the label.
                "sysparm_display_value": "all",
            },
        )
        return list(payload.get("result") or [])

    async def incident(self, sys_id: str) -> dict:
        payload = await self._client.get(
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_display_value": "all"},
        )
        return payload.get("result") or {}

    async def find_by_number(self, number: str) -> dict | None:
        """Look an incident up the way a person refers to it — INC0010001."""
        payload = await self._client.get(
            "/api/now/table/incident",
            params={
                "sysparm_query": f"number={number}",
                "sysparm_limit": 1,
                "sysparm_display_value": "all",
            },
        )
        results = payload.get("result") or []
        return results[0] if results else None

    async def update(
        self, sys_id: str, *, state: str | None = None, work_note: str | None = None
    ) -> dict:
        body: dict[str, str] = {}
        if state is not None:
            body["state"] = state
        if work_note is not None:
            # Appends. ServiceNow treats work_notes as a journal input, so this
            # adds an entry rather than replacing the history.
            body["work_notes"] = work_note
        if not body:
            raise VendorError("nothing to update")

        payload = await self._client.patch(
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_display_value": "all"},
            json_body=body,
        )
        return payload.get("result") or {}


# -- rendering -------------------------------------------------------------


def queue_row(record: dict) -> dict:
    """One incident as a structured row.

    Carries both the machine value and the label for state and priority, for the
    same reason every read here does: an instance's display mode is a
    configuration setting, a caller that ranks must compare values, and a caller
    that renders must show labels. Giving it one of the two guarantees the other
    gets reconstructed wrongly somewhere.
    """
    return {
        "key": field_value(record.get("number")),
        "title": record.get("short_description") or "(no description)",
        "state": field_value(record.get("state")),
        "state_label": field_label(record.get("state"), STATE_LABELS),
        "priority": field_value(record.get("priority")),
        "priority_label": field_label(record.get("priority")),
        "updated_at": field_value(record.get("sys_updated_on")),
    }


def render_incident(record: dict) -> str:
    number = field_value(record.get("number")) or "INC?"
    state = field_label(record.get("state"), STATE_LABELS)
    priority = field_label(record.get("priority"))
    line = f"{number} [{state}] {field_value(record.get('short_description'))}"
    parts = [line, f"  priority: {priority}"]
    if assignee := field_label(record.get("assigned_to")):
        parts.append(f"  assigned: {assignee}")
    if opened := field_value(record.get("opened_at")):
        parts.append(f"  opened: {opened}")
    return "\n".join(parts)


def is_active(record: dict) -> bool:
    return field_value(record.get("state")) in ACTIVE_STATES


# -- the governed tools ----------------------------------------------------


def build_servicenow_source(
    incidents: ServiceNowIncidents, *, name: str = "incidents"
) -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def _resolve(reference: str) -> tuple[str, dict] | None:
        """Accept either a sys_id or the INC number a person would type."""
        text = str(reference).strip()
        if not text:
            return None
        if text.upper().startswith("INC"):
            record = await incidents.find_by_number(text.upper())
        else:
            record = await incidents.incident(text)
        if not record:
            return None
        return field_value(record.get("sys_id")), record

    async def my_incidents(args: dict) -> ToolResult:
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            records = await incidents.my_incidents(limit=limit)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not records:
            return ToolResult.success("No active incidents assigned to you.", {"count": 0})

        return ToolResult.success(
            "\n".join(render_incident(r) for r in records),
            {
                "count": len(records),
                "numbers": [field_value(r.get("number")) for r in records],
                # The machine values, so a caller can rank without reparsing
                # labels that an instance may have renamed.
                "priorities": [field_value(r.get("priority")) for r in records],
                # Structured rows for callers that need records rather than
                # prose — the work queue above all. The prose stays because it
                # is what the *model* reads.
                "items": [queue_row(r) for r in records],
            },
        )

    async def get_incident(args: dict) -> ToolResult:
        try:
            found = await _resolve(args.get("incident", ""))
        except VendorError as exc:
            return ToolResult.failure(str(exc))
        if found is None:
            return ToolResult.failure(f"no incident {args.get('incident')!r} visible to you")

        _, record = found
        text = render_incident(record)
        if description := field_value(record.get("description")):
            text += f"\n\n{description}"
        if notes := field_value(record.get("work_notes")):
            text += f"\n\nWork notes:\n{notes}"

        return ToolResult.success(
            text,
            {
                "number": field_value(record.get("number")),
                "state": field_value(record.get("state")),
                "state_label": field_label(record.get("state"), STATE_LABELS),
                "active": is_active(record),
            },
        )

    async def update_incident(args: dict) -> ToolResult:
        wanted = str(args.get("state", "")).strip().lower().replace(" ", "_")
        note = str(args.get("work_note", "")).strip()

        if wanted and wanted not in SETTABLE_STATES:
            return ToolResult.failure(
                "state must be one of: "
                + ", ".join(SETTABLE_STATES)
                + ". Closing an incident is left to a person — it starts SLA and "
                "survey workflow that cannot be undone from here."
            )
        if not wanted and not note:
            return ToolResult.failure("provide a state, a work_note, or both")

        try:
            found = await _resolve(args.get("incident", ""))
        except VendorError as exc:
            return ToolResult.failure(str(exc))
        if found is None:
            return ToolResult.failure(f"no incident {args.get('incident')!r} visible to you")

        sys_id, _ = found
        try:
            record = await incidents.update(
                sys_id,
                state=SETTABLE_STATES.get(wanted),
                work_note=note or None,
            )
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        return ToolResult.success(
            f"{field_value(record.get('number'))} is now "
            f"{field_label(record.get('state'), STATE_LABELS)}."
            + (" Work note added." if note else ""),
            {
                "number": field_value(record.get("number")),
                "state": field_value(record.get("state")),
            },
        )

    incident_arg = {
        "type": "string",
        "description": "Incident number (INC0010001) or sys_id.",
    }

    source.register(
        "my_incidents",
        my_incidents,
        description="List active incidents assigned to the user, most recent first.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 20."}},
        },
        risk=RiskClass.READ,
        # Descriptions and work notes are written by callers, vendors and
        # whoever raised the ticket.
        returns_untrusted_content=True,
    )
    source.register(
        "get_incident",
        get_incident,
        description="Read one incident with its description and work notes.",
        parameters={
            "type": "object",
            "properties": {"incident": incident_arg},
            "required": ["incident"],
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "update_incident",
        update_incident,
        description=(
            "Move an incident to in_progress, on_hold or resolved, and/or add a work note. "
            "Closing an incident is not available here."
        ),
        parameters={
            "type": "object",
            "properties": {
                "incident": incident_arg,
                "state": {"type": "string", "enum": sorted(SETTABLE_STATES)},
                "work_note": {"type": "string"},
            },
            "required": ["incident"],
        },
        # A work note is a permanent journal entry visible to everyone watching
        # the incident, and a state change moves an SLA clock. Neither can be
        # taken back by writing again.
        risk=RiskClass.IRREVERSIBLE,
    )
    return source


def register_servicenow_verification(verifier) -> None:
    """Teach the verifier to read an incident's state back (F2.6).

    ServiceNow is the connector where this matters most, because it is the one
    with the largest gap between "the API accepted it" and "the record changed".
    Business rules, client scripts and workflow transitions all run *after* the
    Table API returns, and an instance configured to reject a transition answers
    the PATCH with the record as it now stands rather than an error. The connector
    reports what came back and is telling the truth about what it was told.

    The comparison is against the **code**, not the label. `update_incident`
    takes ``in_progress`` and sends ``2``; ``get_incident`` returns the code in
    ``state`` and the label separately. Comparing labels would depend on an
    instance's ``STATE_LABELS`` matching ours, which is a configuration setting
    rather than a fact.

    A work-note-only update has nothing to read back — the note is a journal
    entry, and ``get_incident`` does not return the journal — so it plans
    ``None`` and comes back unverifiable rather than wrongly confirmed.
    """

    def plan_for_update(arguments: dict, _result: ToolResult) -> VerificationPlan | None:
        wanted = str(arguments.get("state", "")).strip().lower().replace(" ", "_")
        expected = SETTABLE_STATES.get(wanted)
        if expected is None:
            return None

        reference = str(arguments.get("incident", "")).strip()
        if not reference:
            return None

        return VerificationPlan(
            tool="incidents.get_incident",
            arguments={"incident": reference},
            expect=lambda result: (result.structured or {}).get("state") == expected,
            describes=f"{reference} is {wanted.replace('_', ' ')}",
        )

    verifier.register("incidents.update_incident", plan_for_update)
