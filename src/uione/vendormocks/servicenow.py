"""A ServiceNow Table API, faked.

Written from the published Table API documentation. **Not verified against a real
instance** — a ServiceNow Personal Developer Instance is free but requires an
account, and this repo does not create accounts on anyone's behalf. Every
behaviour below that could not be confirmed is marked `ASSUMPTION`.

The one thing worth reproducing exactly is the trap that makes ServiceNow
connectors wrong in production:

**The same field has three different shapes depending on a query parameter.**

    sysparm_display_value=false   state → "2"                  (the default)
    sysparm_display_value=true    state → "In Progress"
    sysparm_display_value=all     state → {"display_value": "In Progress",
                                           "value": "2"}

and reference fields like `assigned_to` are a `{"link", "value"}` object in the
first form, a plain display name in the second, and both in the third. A
connector written against one and run against another does not crash — it
reports every incident's state as the wrong thing, quietly, forever. So the mock
implements all three and the connector is tested against all three.

ASSUMPTION: the encoded-query grammar here handles only `field=value` joined by
`^`, plus `^ORDERBYDESC`. Real `sysparm_query` supports a much larger operator
set (`LIKE`, `IN`, `BETWEEN`, `javascript:` expressions). Anything this product
sends is in the supported subset, and a query using more would silently return
everything here while filtering correctly in production — the dangerous
direction, so the mock refuses unknown operators instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

EPOCH = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)

#: ServiceNow's incident state choice list. The numbers are what the API returns
#: by default, and the labels are what a person has ever seen.
STATES = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}

PRIORITIES = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
    "5": "5 - Planning",
}

#: States that mean "somebody is still expected to do something".
ACTIVE_STATES = frozenset({"1", "2", "3"})


def _stamp(minutes: int = 0) -> str:
    # ServiceNow returns "2026-07-27 08:00:00" — space-separated, no timezone,
    # in the instance's configured timezone. Not ISO 8601, and a connector that
    # assumes ISO gets a parse error on the first call.
    return (EPOCH + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


class Reference:
    """A pointer to another record, which is what most interesting fields are."""

    def __init__(self, table: str, sys_id: str, display: str) -> None:
        self.table = table
        self.sys_id = sys_id
        self.display = display

    def render(self, mode: str, base: str) -> dict | str:
        link = f"{base}/api/now/table/{self.table}/{self.sys_id}"
        if mode == "true":
            return self.display
        if mode == "all":
            return {"display_value": self.display, "value": self.sys_id, "link": link}
        return {"link": link, "value": self.sys_id}


class Choice:
    """A field backed by a choice list — a number over the wire, a word to a human."""

    def __init__(self, value: str, labels: dict[str, str]) -> None:
        self.value = value
        self.labels = labels

    def render(self, mode: str) -> dict | str:
        if mode == "true":
            return self.labels.get(self.value, self.value)
        if mode == "all":
            return {"display_value": self.labels.get(self.value, self.value), "value": self.value}
        return self.value


class State:
    def __init__(self) -> None:
        self.incidents: dict[str, dict] = {}
        self.counter = 10000

    def add_incident(
        self,
        short_description: str,
        *,
        description: str = "",
        state: str = "2",
        priority: str = "3",
        assigned_to: str = "uione",
        caller: str = "service.desk",
        category: str = "software",
        minutes: int = 0,
        work_notes: str = "",
    ) -> dict:
        self.counter += 1
        sys_id = f"{self.counter:032x}"
        record = {
            "sys_id": sys_id,
            "number": f"INC{self.counter:07d}",
            "short_description": short_description,
            "description": description,
            "state": Choice(state, STATES),
            "priority": Choice(priority, PRIORITIES),
            "urgency": Choice(priority, PRIORITIES),
            "impact": Choice(priority, PRIORITIES),
            "category": category,
            "assigned_to": Reference("sys_user", f"u-{assigned_to}", assigned_to),
            "caller_id": Reference("sys_user", f"u-{caller}", caller),
            "assignment_group": Reference("sys_user_group", "g-payments", "Payments Support"),
            "opened_at": _stamp(minutes),
            "sys_updated_on": _stamp(minutes),
            "work_notes": work_notes,
            "close_notes": "",
        }
        self.incidents[sys_id] = record
        return record


def _render(record: dict, mode: str, base: str) -> dict:
    out: dict = {}
    for key, value in record.items():
        if isinstance(value, Reference):
            out[key] = value.render(mode, base)
        elif isinstance(value, Choice):
            out[key] = value.render(mode)
        else:
            out[key] = value
    return out


class Patch(BaseModel):
    state: str | None = None
    work_notes: str | None = None
    close_notes: str | None = None
    assigned_to: str | None = None


def build_servicenow_mock(state: State | None = None, *, base: str = "http://snow.mock") -> FastAPI:
    app = FastAPI(title="mock-servicenow")
    app.state.data = state or State()
    app.state.base = base

    def data(request: Request) -> State:
        return request.app.state.data

    @app.get("/api/now/table/{table}")
    async def query_table(
        request: Request,
        table: str,
        sysparm_query: str = Query(""),
        sysparm_limit: int = Query(10),
        sysparm_offset: int = Query(0),
        sysparm_display_value: str = Query("false"),
    ) -> dict:
        if table != "incident":
            # A real instance has hundreds of tables. Answering an empty result
            # for one we do not model would look like "no records", which is a
            # different thing from "not implemented".
            raise HTTPException(status_code=400, detail=f"mock implements incident, not {table}")

        records = list(data(request).incidents.values())
        for clause in filter(None, sysparm_query.split("^")):
            if clause.upper().startswith("ORDERBY"):
                continue
            field, separator, wanted = clause.partition("=")
            if not separator:
                # ServiceNow's other operators are suffixes on the field name —
                # `stateIN1,2`, `short_descriptionLIKEoutage`. None are
                # implemented here, and refusing beats matching everything.
                #
                # Checked this way rather than by searching the clause for
                # operator names: `number=INC0010001` contains "IN", and a
                # substring test rejects every incident lookup in the product.
                raise HTTPException(status_code=400, detail=f"mock does not implement: {clause}")
            if wanted.startswith("javascript:"):
                raise HTTPException(status_code=400, detail=f"mock does not implement: {clause}")
            if field == "active":
                keep = wanted == "true"
                records = [r for r in records if (r["state"].value in ACTIVE_STATES) == keep]
            elif field == "assigned_to":
                records = [r for r in records if r["assigned_to"].display == wanted]
            elif field == "state":
                records = [r for r in records if r["state"].value == wanted]
            elif field == "number":
                # How a person refers to an incident, and therefore how the
                # connector looks one up. The mock refusing this is what caught
                # its absence — a mock that answered "no records" instead would
                # have looked like an empty queue.
                records = [r for r in records if r["number"] == wanted]
            else:
                raise HTTPException(status_code=400, detail=f"unknown field {field!r}")

        if "ORDERBYDESCsys_updated_on" in sysparm_query:
            records.sort(key=lambda r: r["sys_updated_on"], reverse=True)

        window = records[sysparm_offset : sysparm_offset + sysparm_limit]
        return {
            "result": [_render(r, sysparm_display_value, request.app.state.base) for r in window]
        }

    @app.get("/api/now/table/{table}/{sys_id}")
    async def get_record(
        request: Request,
        table: str,
        sys_id: str,
        sysparm_display_value: str = Query("false"),
    ) -> dict:
        record = data(request).incidents.get(sys_id)
        if record is None:
            # ServiceNow answers 404 with this exact envelope.
            raise HTTPException(status_code=404, detail="No Record found")
        return {"result": _render(record, sysparm_display_value, request.app.state.base)}

    @app.patch("/api/now/table/{table}/{sys_id}")
    async def patch_record(
        request: Request,
        table: str,
        sys_id: str,
        patch: Patch,
        sysparm_display_value: str = Query("false"),
    ) -> dict:
        record = data(request).incidents.get(sys_id)
        if record is None:
            raise HTTPException(status_code=404, detail="No Record found")

        if patch.state is not None:
            if patch.state not in STATES:
                # ASSUMPTION: a real instance may accept an out-of-range choice
                # and store it, depending on dictionary configuration. Refusing
                # is the stricter reading and the one that surfaces a bug.
                raise HTTPException(status_code=400, detail=f"invalid state {patch.state}")
            record["state"] = Choice(patch.state, STATES)
        if patch.work_notes is not None:
            # Work notes are a journal field: writes append, they do not replace.
            # A connector that expects to overwrite them is wrong in a way no
            # single call reveals.
            existing = record["work_notes"]
            record["work_notes"] = f"{existing}\n{patch.work_notes}".strip()
        if patch.close_notes is not None:
            record["close_notes"] = patch.close_notes
        if patch.assigned_to is not None:
            record["assigned_to"] = Reference(
                "sys_user", f"u-{patch.assigned_to}", patch.assigned_to
            )

        record["sys_updated_on"] = _stamp(1)
        return {"result": _render(record, sysparm_display_value, request.app.state.base)}

    return app


def seed_servicenow(state: State | None = None, *, user: str = "uione") -> State:
    """A support queue that looks like a real morning.

    Deliberately mixed: one thing genuinely on fire, one waiting on someone else,
    one that is only noise, and one already resolved — so a brief that leads with
    the critical incident has actually chosen rather than just listed.
    """
    store = state or State()

    store.add_incident(
        "Card settlement delayed for 2,300 transactions",
        description="Acquirer soft-declines are not being retried. Revenue impact accruing.",
        state="2",
        priority="1",
        assigned_to=user,
        minutes=-25,
        work_notes="07:35 — bridge open with the acquirer.",
    )
    store.add_incident(
        "Refund API returning 500 for merchant 4471",
        description="Intermittent since the 06:00 deploy.",
        state="3",
        priority="2",
        assigned_to=user,
        minutes=-150,
        work_notes="Waiting on the merchant to confirm the payload they send.",
    )
    store.add_incident(
        "Printer on floor 3 offline",
        description="Reported by facilities.",
        state="1",
        priority="5",
        assigned_to=user,
        minutes=-300,
    )
    store.add_incident(
        "Nightly reconciliation job timed out",
        description="Ran long, completed on retry.",
        state="6",
        priority="3",
        assigned_to=user,
        minutes=-720,
    )
    return store
