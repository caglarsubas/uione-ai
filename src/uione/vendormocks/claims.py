"""A claim-management API in the Guidewire ClaimCenter Cloud API style.

**This mock has never been checked against a real system, and cannot be.**
Guidewire, Duck Creek and their peers are sold through enterprise agreements with
no trial, no developer edition and no sandbox. Claim management is named in the
product brief, so the choice is between a mock with its limits stated and no
claims capability at all.

Written from published Cloud API conventions. Three of those conventions are
worth reproducing because each one breaks a connector written without them:

**The JSON:API-style envelope.** Collections come back as
`{"count": n, "data": [{"attributes": {...}}]}` and a single record as
`{"data": {"attributes": {...}}}`. The fields are one level deeper than they look.

**Optimistic locking by checksum.** Every record carries a `checksum`, and an
update must send it back. A stale checksum is a `409`, not a silent overwrite.
This is the single most important behaviour here: it forces read-before-write,
which is exactly what an assistant editing a legal record should do.

**Money is a string with a currency, never a float.** `{"amount": "1200.00",
"currency": "usd"}`. A connector that parses it as a float and formats it back
loses cents on some values, and cents in a claims system are a regulatory matter.

ASSUMPTION, marked because it matters: the exact field names, the status code
vocabulary and the checksum semantics here follow the documented conventions but
have not been confirmed against a running instance. Anyone connecting this to a
real ClaimCenter should expect to adjust `claims/gwclaims.py` at the field-name
level, and should treat the *shape* — envelope, locking, money — as the part
that transfers.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

EPOCH = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)

STATUSES = {
    "open": "Open",
    "draft": "Draft",
    "closed": "Closed",
    "reopened": "Reopened",
}


def _stamp(minutes: int = 0) -> str:
    return (EPOCH + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _checksum(record: dict) -> str:
    """Derived from content, so any change invalidates a held checksum.

    A real system uses an opaque version token. Deriving it here means the mock
    cannot accidentally accept a stale one, which is the behaviour under test.
    """
    material = f"{record['attributes'].get('status')}|{record['attributes'].get('assignedUser')}"
    material += f"|{len(record.get('notes', []))}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


class State:
    def __init__(self) -> None:
        self.claims: dict[str, dict] = {}
        self.counter = 4400

    def add_claim(
        self,
        *,
        insured: str,
        loss_cause: str,
        status: str = "open",
        assigned_user: str = "uione",
        incurred: str = "0.00",
        currency: str = "eur",
        loss_date: date | None = None,
        description: str = "",
        policy: str = "",
        minutes: int = 0,
    ) -> dict:
        self.counter += 1
        claim_id = f"cc:{self.counter}"
        record = {
            "id": claim_id,
            "attributes": {
                "claimNumber": f"CLM-{self.counter:06d}",
                "insured": {"displayName": insured},
                "policyNumber": policy or f"POL-{self.counter:06d}",
                "lossDate": (loss_date or date(2026, 7, 20)).isoformat(),
                "lossCause": loss_cause,
                "description": description,
                # A code/name pair, not a bare string: the code is stable and
                # the name is localised, and a connector should key on the code.
                "status": {"code": status, "name": STATUSES.get(status, status)},
                "assignedUser": assigned_user,
                # String amount, deliberately. Cents matter and floats lose them.
                "totalIncurred": {"amount": incurred, "currency": currency},
                "createdDate": _stamp(minutes),
                "updatedDate": _stamp(minutes),
            },
            "notes": [],
        }
        record["checksum"] = _checksum(record)
        self.claims[claim_id] = record
        return record

    def add_note(self, claim_id: str, body: str, author: str) -> dict:
        claim = self.claims[claim_id]
        note = {
            "id": f"note:{len(claim['notes']) + 1}",
            "attributes": {
                "body": body,
                "author": {"displayName": author},
                "createdDate": _stamp(),
            },
        }
        claim["notes"].append(note)
        claim["checksum"] = _checksum(claim)
        return note


def _envelope_one(record: dict) -> dict:
    return {
        "data": {
            "id": record["id"],
            "attributes": record["attributes"],
            "checksum": record["checksum"],
        }
    }


def _envelope_many(records: list[dict]) -> dict:
    return {
        "count": len(records),
        "data": [
            {"id": r["id"], "attributes": r["attributes"], "checksum": r["checksum"]}
            for r in records
        ],
    }


class ClaimPatch(BaseModel):
    status: str | None = None
    assignedUser: str | None = None  # noqa: N815 — the vendor's field name


class NoteBody(BaseModel):
    body: str


def build_claims_mock(state: State | None = None) -> FastAPI:
    app = FastAPI(title="mock-claimcenter")
    app.state.data = state or State()

    def data(request: Request) -> State:
        return request.app.state.data

    def require(request: Request, claim_id: str) -> dict:
        claim = data(request).claims.get(claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="claim not found")
        return claim

    @app.get("/cc/v1/claims")
    async def list_claims(
        request: Request,
        assignedUser: str = Query(""),  # noqa: N803 — the vendor's parameter name
        status: str = Query(""),
        pageSize: int = Query(25),  # noqa: N803
    ) -> dict:
        claims = list(data(request).claims.values())
        if assignedUser:
            claims = [c for c in claims if c["attributes"]["assignedUser"] == assignedUser]
        if status:
            claims = [c for c in claims if c["attributes"]["status"]["code"] == status]
        claims.sort(key=lambda c: c["attributes"]["updatedDate"], reverse=True)
        return _envelope_many(claims[:pageSize])

    @app.get("/cc/v1/claims/{claim_id}")
    async def get_claim(request: Request, claim_id: str) -> dict:
        return _envelope_one(require(request, claim_id))

    @app.get("/cc/v1/claims/{claim_id}/notes")
    async def list_notes(request: Request, claim_id: str) -> dict:
        claim = require(request, claim_id)
        return {"count": len(claim["notes"]), "data": claim["notes"]}

    @app.post("/cc/v1/claims/{claim_id}/notes", status_code=201)
    async def create_note(request: Request, claim_id: str, note: NoteBody) -> dict:
        require(request, claim_id)
        created = data(request).add_note(claim_id, note.body, "uione")
        return {"data": created}

    @app.patch("/cc/v1/claims/{claim_id}")
    async def patch_claim(
        request: Request,
        claim_id: str,
        patch: ClaimPatch,
        checksum: str = Query(""),
    ) -> dict:
        claim = require(request, claim_id)

        # The behaviour worth having: a write without the current checksum is
        # refused. Two adjusters editing the same claim is not a hypothetical,
        # and an assistant overwriting a colleague's change is exactly the kind
        # of quiet damage that ends a pilot.
        if not checksum:
            raise HTTPException(status_code=400, detail="checksum is required for updates")
        if checksum != claim["checksum"]:
            raise HTTPException(
                status_code=409,
                detail="the claim changed since it was read; re-read it and retry",
            )

        if patch.status is not None:
            if patch.status not in STATUSES:
                raise HTTPException(status_code=400, detail=f"unknown status {patch.status}")
            claim["attributes"]["status"] = {
                "code": patch.status,
                "name": STATUSES[patch.status],
            }
        if patch.assignedUser is not None:
            claim["attributes"]["assignedUser"] = patch.assignedUser

        claim["attributes"]["updatedDate"] = _stamp(1)
        claim["checksum"] = _checksum(claim)
        return _envelope_one(claim)

    return app


def seed_claims(state: State | None = None, *, user: str = "uione") -> State:
    """A small book of claims for one adjuster."""
    store = state or State()

    store.add_claim(
        insured="Northgate Logistics BV",
        loss_cause="collision",
        description="Fleet vehicle collision on the A12. Third-party liability disputed.",
        incurred="18450.00",
        assigned_user=user,
        loss_date=date(2026, 7, 18),
        minutes=-45,
    )
    store.add_claim(
        insured="Marta Ilves",
        loss_cause="water damage",
        description="Burst pipe, ground floor. Contractor estimate received.",
        incurred="6120.50",
        assigned_user=user,
        loss_date=date(2026, 7, 11),
        minutes=-200,
    )
    store.add_claim(
        insured="Delacroix Patisserie",
        loss_cause="business interruption",
        description="Power outage, four days of spoilage. Awaiting supplier invoices.",
        incurred="2310.00",
        assigned_user=user,
        loss_date=date(2026, 6, 29),
        minutes=-1500,
    )
    store.add_claim(
        insured="Habour Freight Ltd",
        loss_cause="cargo loss",
        status="closed",
        description="Settled and paid.",
        incurred="9800.00",
        assigned_user=user,
        loss_date=date(2026, 5, 3),
        minutes=-8000,
    )
    store.add_note(
        "cc:4401", "Third party's insurer has not responded to the liability letter.", "uione"
    )
    return store
