"""Claim management.

The product brief names claim management alongside tickets and incidents. It is
also the one category in the brief with no free access of any kind — Guidewire,
Duck Creek and their peers sell through enterprise agreements with no trial and
no developer edition. So this connector is written against published Cloud API
conventions and verified against a mock, and `docs/CONNECTORS.md` says exactly
that rather than implying more.

What transfers to a real system regardless of field names:

**Read before write.** Every update sends back the `checksum` from the read that
preceded it, so an edit made while a colleague was editing the same claim is
refused rather than silently overwriting them. Two adjusters on one claim is not
hypothetical, and an assistant that quietly clobbers a human's change is how a
pilot ends.

**Money is never a float.** Amounts arrive as decimal strings with a currency and
are passed through untouched. Parsing `"6120.50"` into a float and formatting it
back loses cents on some values, and cents in a claims system are a regulatory
matter, not a rounding preference.

**Nothing here moves money.** A real ClaimCenter exposes payments, reserves and
settlement. This connector deliberately exposes none of them, and that is a
product decision rather than an unfinished one: an assistant that can issue a
payment is an assistant one prompt injection away from issuing a payment. Notes
and status are the useful part; the cheque is a person's job.
"""

from __future__ import annotations

from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 25

#: Statuses this product will set. "closed" is absent on purpose: closing a claim
#: releases reserves and can start regulatory clocks, which is not a decision to
#: be reached by summarising an email thread.
SETTABLE_STATUSES = {"open", "reopened"}


def claims_config(
    base_url: str, token: str, *, verify_tls: bool = True, timeout_s: float = 20.0
) -> VendorConfig:
    return VendorConfig(
        name="claims",
        base_url=base_url.rstrip("/"),
        auth=Auth(scheme="bearer", secret=token) if token else Auth(scheme="none"),
        verify_tls=verify_tls,
        timeout_s=timeout_s,
        extra_headers={"Content-Type": "application/json"},
    )


def attributes(record: dict) -> dict:
    """Unwrap the envelope. The fields are a level deeper than they look."""
    return record.get("attributes") or {}


def money(value: Any) -> str:
    """Render an amount without ever turning it into a number.

    `{"amount": "6120.50", "currency": "eur"}` → `"6120.50 EUR"`. No float, no
    locale guessing, no thousands separator invented by us — the string the
    system stated, and the currency it stated it in.
    """
    if not isinstance(value, dict):
        return str(value or "")
    amount = str(value.get("amount", "")).strip()
    currency = str(value.get("currency", "")).strip().upper()
    return f"{amount} {currency}".strip()


def status_code(record_attributes: dict) -> str:
    """The stable code, not the localised name.

    Keying on `name` works until an instance is used in another language, at
    which point every comparison silently stops matching.
    """
    status = record_attributes.get("status")
    if isinstance(status, dict):
        return str(status.get("code", ""))
    return str(status or "")


class ClaimsBackend:
    def __init__(self, config: VendorConfig, *, user: str = "", **kwargs: Any) -> None:
        self._client = VendorClient(config, **kwargs)
        self._user = user

    async def aclose(self) -> None:
        await self._client.aclose()

    async def my_claims(
        self, *, limit: int = DEFAULT_LIMIT, status: str = "open", user: str = ""
    ) -> list[dict]:
        params: dict[str, Any] = {"pageSize": limit}
        if who := user or self._user:
            params["assignedUser"] = who
        if status:
            params["status"] = status
        payload = await self._client.get("/cc/v1/claims", params=params)
        return list(payload.get("data") or [])

    async def claim(self, claim_id: str) -> dict:
        payload = await self._client.get(f"/cc/v1/claims/{claim_id}")
        return payload.get("data") or {}

    async def notes(self, claim_id: str, *, limit: int = 10) -> list[dict]:
        payload = await self._client.get(f"/cc/v1/claims/{claim_id}/notes")
        return list(payload.get("data") or [])[-limit:]

    async def add_note(self, claim_id: str, body: str) -> dict:
        payload = await self._client.post(
            f"/cc/v1/claims/{claim_id}/notes", json_body={"body": body}
        )
        return payload.get("data") or {}

    async def set_status(self, claim_id: str, status: str) -> dict:
        """Change a claim's status, refusing to overwrite a concurrent edit.

        Reads first, purely to obtain the current checksum. That extra call is
        the whole point: without it the write either fails or, on a system with
        weaker locking, silently discards whatever a colleague just did.
        """
        current = await self.claim(claim_id)
        checksum = current.get("checksum")
        if not checksum:
            raise VendorError("claim has no checksum; refusing to write blind")

        payload = await self._client.patch(
            f"/cc/v1/claims/{claim_id}",
            params={"checksum": checksum},
            json_body={"status": status},
        )
        return payload.get("data") or {}

    async def find_by_number(self, number: str) -> dict | None:
        """Claims are referred to by CLM-004401, never by the internal id."""
        for status in ("open", "reopened", "closed", "draft", ""):
            for record in await self.my_claims(limit=100, status=status, user=""):
                if attributes(record).get("claimNumber", "").upper() == number.upper():
                    return record
        return None


# -- rendering -------------------------------------------------------------


def render_claim(record: dict) -> str:
    a = attributes(record)
    insured = (a.get("insured") or {}).get("displayName", "?")
    parts = [
        f"{a.get('claimNumber', '?')} [{status_code(a)}] {insured} — {a.get('lossCause', '?')}",
        f"  loss date: {a.get('lossDate', '?')}  incurred: {money(a.get('totalIncurred'))}",
    ]
    if assigned := a.get("assignedUser"):
        parts.append(f"  assigned: {assigned}")
    return "\n".join(parts)


# -- the governed tools ----------------------------------------------------


def build_claims_source(backend: ClaimsBackend, *, name: str = "claims") -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def _resolve(reference: str) -> dict | None:
        text = str(reference).strip()
        if not text:
            return None
        if text.upper().startswith("CLM"):
            return await backend.find_by_number(text)
        record = await backend.claim(text)
        return record or None

    async def my_claims(args: dict) -> ToolResult:
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            records = await backend.my_claims(limit=limit)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not records:
            return ToolResult.success("No open claims assigned to you.", {"count": 0})

        return ToolResult.success(
            "\n".join(render_claim(r) for r in records),
            {
                "count": len(records),
                "numbers": [attributes(r).get("claimNumber") for r in records],
                # Amounts stay strings all the way to the caller. Nothing in
                # this path may turn a monetary value into a float.
                "incurred": [money(attributes(r).get("totalIncurred")) for r in records],
            },
        )

    async def get_claim(args: dict) -> ToolResult:
        try:
            record = await _resolve(args.get("claim", ""))
            if record is None:
                return ToolResult.failure(f"no claim {args.get('claim')!r} visible to you")
            notes = await backend.notes(record.get("id", ""))
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        a = attributes(record)
        text = render_claim(record)
        if description := a.get("description"):
            text += f"\n\n{description}"
        if notes:
            rendered = "\n".join(
                f"  {(attributes(n).get('author') or {}).get('displayName', '?')}: "
                f"{attributes(n).get('body', '')[:400]}"
                for n in notes
            )
            text += f"\n\nNotes:\n{rendered}"

        return ToolResult.success(
            text,
            {
                "number": a.get("claimNumber"),
                "status": status_code(a),
                "incurred": money(a.get("totalIncurred")),
                "id": record.get("id"),
            },
        )

    async def add_note(args: dict) -> ToolResult:
        body = str(args.get("note", "")).strip()
        if not body:
            return ToolResult.failure("note is required")

        try:
            record = await _resolve(args.get("claim", ""))
            if record is None:
                return ToolResult.failure(f"no claim {args.get('claim')!r} visible to you")
            await backend.add_note(record.get("id", ""), body)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        return ToolResult.success(
            f"Note added to {attributes(record).get('claimNumber')}.",
            {"number": attributes(record).get("claimNumber")},
        )

    async def set_status(args: dict) -> ToolResult:
        wanted = str(args.get("status", "")).strip().lower()
        if wanted not in SETTABLE_STATUSES:
            return ToolResult.failure(
                "status must be one of: "
                + ", ".join(sorted(SETTABLE_STATUSES))
                + ". Closing a claim releases reserves and can start regulatory "
                "clocks, so it is left to a person."
            )

        try:
            record = await _resolve(args.get("claim", ""))
            if record is None:
                return ToolResult.failure(f"no claim {args.get('claim')!r} visible to you")
            updated = await backend.set_status(record.get("id", ""), wanted)
        except VendorError as exc:
            # Includes the 409 from a concurrent edit, which is a useful thing
            # for the model to see: it can re-read and decide, rather than
            # having a colleague's change silently discarded on its behalf.
            return ToolResult.failure(str(exc))

        a = attributes(updated)
        return ToolResult.success(
            f"{a.get('claimNumber')} is now {status_code(a)}.",
            {"number": a.get("claimNumber"), "status": status_code(a)},
        )

    claim_arg = {"type": "string", "description": "Claim number (CLM-004401) or claim id."}

    source.register(
        "my_claims",
        my_claims,
        description="List open claims assigned to the user.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 25."}},
        },
        risk=RiskClass.READ,
        # Descriptions and notes carry text from claimants, contractors, loss
        # adjusters and opposing insurers. None of them are trusted authors.
        returns_untrusted_content=True,
    )
    source.register(
        "get_claim",
        get_claim,
        description="Read one claim with its description and notes.",
        parameters={"type": "object", "properties": {"claim": claim_arg}, "required": ["claim"]},
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "add_note",
        add_note,
        description="Add a note to a claim.",
        parameters={
            "type": "object",
            "properties": {"claim": claim_arg, "note": {"type": "string"}},
            "required": ["claim", "note"],
        },
        # A claim note is part of the file that gets disclosed in a dispute.
        # It cannot be unwritten.
        risk=RiskClass.IRREVERSIBLE,
    )
    source.register(
        "set_status",
        set_status,
        description="Set a claim to open or reopened. Closing is not available here.",
        parameters={
            "type": "object",
            "properties": {
                "claim": claim_arg,
                "status": {"type": "string", "enum": sorted(SETTABLE_STATUSES)},
            },
            "required": ["claim", "status"],
        },
        risk=RiskClass.IRREVERSIBLE,
    )
    return source
