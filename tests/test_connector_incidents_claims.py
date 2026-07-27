"""Incidents and claims — the two connectors that exist only against mocks.

Both are honest about that. What these tests can prove is that the connector
handles the *shapes* correctly, and the shapes are the part that transfers: a
three-way-polymorphic field, an optimistic-locking checksum, money that must
never become a float. Field names may need adjusting against a real instance;
these behaviours will not.
"""

from __future__ import annotations

import httpx
import pytest

from uione.connectors.claims import (
    ClaimsBackend,
    build_claims_source,
    claims_config,
    money,
    status_code,
)
from uione.connectors.incidents import (
    ServiceNowIncidents,
    build_servicenow_source,
    field_label,
    field_value,
    servicenow_config,
)
from uione.mcphub import RiskClass
from uione.vendormocks.claims import build_claims_mock, seed_claims
from uione.vendormocks.servicenow import build_servicenow_mock, seed_servicenow


def _asgi(app, base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)


@pytest.fixture
def incidents() -> ServiceNowIncidents:
    app = build_servicenow_mock(seed_servicenow())
    return ServiceNowIncidents(
        servicenow_config("http://snow.mock", "admin", "pw"),
        user="uione",
        client=_asgi(app, "http://snow.mock"),
    )


@pytest.fixture
def claims() -> ClaimsBackend:
    app = build_claims_mock(seed_claims())
    return ClaimsBackend(
        claims_config("http://claims.mock", ""),
        user="uione",
        client=_asgi(app, "http://claims.mock"),
    )


# -- the three-shaped field ------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected_value", "expected_label"),
    [
        # sysparm_display_value=false — the default
        ("2", "2", "In Progress"),
        # sysparm_display_value=true
        ("In Progress", "In Progress", "In Progress"),
        # sysparm_display_value=all
        ({"display_value": "In Progress", "value": "2"}, "2", "In Progress"),
        # a reference field in its default shape
        ({"link": "http://x/api/now/table/sys_user/u-1", "value": "u-1"}, "u-1", "u-1"),
    ],
)
def test_every_servicenow_field_shape_is_understood(field, expected_value, expected_label) -> None:
    """The failure this prevents is silent: a connector meeting an unexpected
    shape reports the wrong state rather than crashing."""
    labels = {"2": "In Progress"}

    assert field_value(field) == expected_value
    assert field_label(field, labels) == expected_label


def test_an_instance_label_beats_our_table() -> None:
    """An instance that renamed its choice list wins over a constant in our
    source, which is why display_value is requested at all."""
    field = {"display_value": "Awaiting Vendor", "value": "3"}

    assert field_label(field, {"3": "On Hold"}) == "Awaiting Vendor"


def test_a_missing_field_is_empty_rather_than_none() -> None:
    assert field_value(None) == ""
    assert field_label(None) == ""


async def test_an_incident_number_is_not_mistaken_for_an_operator(
    incidents: ServiceNowIncidents,
) -> None:
    """`INC0010001` contains `IN`, which is a real ServiceNow operator.

    The mock refuses operators it does not implement — correctly, since matching
    everything would make the connector look right while filtering nothing. But
    the first version searched the whole clause for operator names, so every
    incident lookup in the product was rejected as unimplemented.
    """
    found = await incidents.find_by_number("INC0010001")

    assert found is not None
    assert field_value(found.get("number")) == "INC0010001"


# -- reading incidents -----------------------------------------------------


async def test_the_queue_shows_active_incidents(incidents: ServiceNowIncidents) -> None:
    result = await build_servicenow_source(incidents).call("my_incidents", {})

    assert result.ok
    assert result.structured["count"] == 3
    assert "Card settlement delayed" in result.content


async def test_resolved_incidents_are_not_in_the_queue(incidents: ServiceNowIncidents) -> None:
    result = await build_servicenow_source(incidents).call("my_incidents", {})

    assert "Nightly reconciliation" not in result.content


async def test_the_queue_carries_machine_values_for_ranking(
    incidents: ServiceNowIncidents,
) -> None:
    """Labels can be renamed per instance; the numbers cannot."""
    result = await build_servicenow_source(incidents).call("my_incidents", {})

    assert "1" in result.structured["priorities"]


async def test_states_render_as_words_not_numbers(incidents: ServiceNowIncidents) -> None:
    """Nobody has ever seen "state 2" in the ServiceNow UI."""
    result = await build_servicenow_source(incidents).call("my_incidents", {})

    assert "In Progress" in result.content
    assert "[2]" not in result.content


async def test_an_incident_can_be_read_by_the_number_a_person_types(
    incidents: ServiceNowIncidents,
) -> None:
    source = build_servicenow_source(incidents)
    number = (await source.call("my_incidents", {})).structured["numbers"][0]

    result = await source.call("get_incident", {"incident": number})

    assert result.ok
    assert result.structured["number"] == number
    assert result.structured["active"]


async def test_reading_an_incident_includes_its_work_notes(
    incidents: ServiceNowIncidents,
) -> None:
    source = build_servicenow_source(incidents)
    number = (await source.call("my_incidents", {})).structured["numbers"][0]

    result = await source.call("get_incident", {"incident": number})

    assert "bridge open with the acquirer" in result.content


async def test_an_unknown_incident_fails_clearly(incidents: ServiceNowIncidents) -> None:
    result = await build_servicenow_source(incidents).call(
        "get_incident", {"incident": "INC9999999"}
    )

    assert not result.ok


# -- writing incidents -----------------------------------------------------


async def test_a_work_note_appends_rather_than_replacing(
    incidents: ServiceNowIncidents,
) -> None:
    """work_notes is a journal. Treating it as a string to overwrite destroys
    the incident's history, and no single call reveals it."""
    source = build_servicenow_source(incidents)
    number = (await source.call("my_incidents", {})).structured["numbers"][0]

    await source.call("update_incident", {"incident": number, "work_note": "Acquirer confirmed."})
    read = await source.call("get_incident", {"incident": number})

    assert "bridge open with the acquirer" in read.content
    assert "Acquirer confirmed." in read.content


async def test_an_incident_can_be_moved_to_on_hold(incidents: ServiceNowIncidents) -> None:
    source = build_servicenow_source(incidents)
    number = (await source.call("my_incidents", {})).structured["numbers"][0]

    result = await source.call("update_incident", {"incident": number, "state": "on_hold"})

    assert result.ok
    assert result.structured["state"] == "3"


async def test_closing_an_incident_is_not_offered(incidents: ServiceNowIncidents) -> None:
    """Closing starts SLA and survey workflow that cannot be undone from here."""
    source = build_servicenow_source(incidents)
    number = (await source.call("my_incidents", {})).structured["numbers"][0]

    result = await source.call("update_incident", {"incident": number, "state": "closed"})

    assert not result.ok
    assert "left to a person" in (result.error or "")


async def test_an_update_with_nothing_to_change_is_refused(
    incidents: ServiceNowIncidents,
) -> None:
    result = await build_servicenow_source(incidents).call(
        "update_incident", {"incident": "INC0010001"}
    )

    assert not result.ok


async def test_incident_tools_are_classified_by_what_they_cost(
    incidents: ServiceNowIncidents,
) -> None:
    specs = {s.tool: s for s in await build_servicenow_source(incidents).list_tools()}

    assert specs["my_incidents"].risk is RiskClass.READ
    assert specs["my_incidents"].returns_untrusted_content
    # A work note is permanent and visible to everyone watching; a state change
    # moves an SLA clock. Writing again does not take either back.
    assert specs["update_incident"].risk is RiskClass.IRREVERSIBLE


# -- money -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"amount": "6120.50", "currency": "eur"}, "6120.50 EUR"),
        ({"amount": "18450.00", "currency": "usd"}, "18450.00 USD"),
        # The case a float would break: 0.1 + 0.2 arithmetic never touches this.
        ({"amount": "0.07", "currency": "gbp"}, "0.07 GBP"),
        ({}, ""),
    ],
)
def test_money_is_never_turned_into_a_number(value, expected) -> None:
    """Cents in a claims system are a regulatory matter, not a rounding
    preference."""
    assert money(value) == expected


async def test_amounts_reach_the_caller_as_strings(claims: ClaimsBackend) -> None:
    result = await build_claims_source(claims).call("my_claims", {})

    assert all(isinstance(a, str) for a in result.structured["incurred"])
    assert "18450.00 EUR" in result.structured["incurred"]


# -- reading claims --------------------------------------------------------


async def test_the_claim_list_shows_open_claims(claims: ClaimsBackend) -> None:
    result = await build_claims_source(claims).call("my_claims", {})

    assert result.ok
    assert result.structured["count"] == 3
    assert "Northgate Logistics" in result.content


async def test_a_settled_claim_is_not_in_the_open_list(claims: ClaimsBackend) -> None:
    result = await build_claims_source(claims).call("my_claims", {})

    assert "Habour Freight" not in result.content


def test_status_is_keyed_on_the_code_not_the_localised_name() -> None:
    """Keying on `name` works until the instance is used in another language,
    at which point every comparison silently stops matching."""
    assert status_code({"status": {"code": "open", "name": "Ouvert"}}) == "open"


async def test_a_claim_reads_with_its_notes(claims: ClaimsBackend) -> None:
    result = await build_claims_source(claims).call("get_claim", {"claim": "CLM-004401"})

    assert result.ok
    assert "liability letter" in result.content


async def test_a_note_can_be_added(claims: ClaimsBackend) -> None:
    source = build_claims_source(claims)

    added = await source.call(
        "add_note", {"claim": "CLM-004401", "note": "Chased the third party's insurer."}
    )
    read = await source.call("get_claim", {"claim": "CLM-004401"})

    assert added.ok
    assert "Chased the third party" in read.content


# -- optimistic locking ----------------------------------------------------


async def test_an_update_sends_the_checksum_from_the_read_that_preceded_it(
    claims: ClaimsBackend,
) -> None:
    result = await build_claims_source(claims).call(
        "set_status", {"claim": "CLM-004401", "status": "reopened"}
    )

    assert result.ok
    assert result.structured["status"] == "reopened"


async def test_a_concurrent_edit_is_refused_rather_than_overwritten() -> None:
    """The behaviour worth having.

    A colleague edits the claim between our read and our write. Silently
    discarding their change is exactly the quiet damage that ends a pilot, so
    the vendor's 409 is surfaced to the model, which can re-read and decide.
    """
    state = seed_claims()
    app = build_claims_mock(state)
    backend = ClaimsBackend(
        claims_config("http://claims.mock", ""),
        user="uione",
        client=_asgi(app, "http://claims.mock"),
    )

    stale = state.claims["cc:4401"]["checksum"]
    # Someone else touches the claim, which changes its checksum.
    state.add_note("cc:4401", "Adjuster's own note.", "colleague")

    with pytest.raises(Exception) as caught:
        await backend._client.patch(
            "/cc/v1/claims/cc:4401",
            params={"checksum": stale},
            json_body={"status": "reopened"},
        )

    assert "changed since it was read" in str(caught.value)


async def test_a_blind_write_is_refused(claims: ClaimsBackend) -> None:
    """No checksum at all is a 400, not a silent overwrite."""
    with pytest.raises(Exception, match="checksum"):
        await claims._client.patch("/cc/v1/claims/cc:4401", json_body={"status": "reopened"})


# -- what claims deliberately cannot do ------------------------------------


async def test_closing_a_claim_is_not_offered(claims: ClaimsBackend) -> None:
    """Closing releases reserves and can start regulatory clocks."""
    result = await build_claims_source(claims).call(
        "set_status", {"claim": "CLM-004401", "status": "closed"}
    )

    assert not result.ok
    assert "left to a person" in (result.error or "")


async def test_no_tool_here_can_move_money(claims: ClaimsBackend) -> None:
    """A real ClaimCenter exposes payments, reserves and settlement. None of
    them are here, and that is a product decision rather than a gap: an
    assistant that can issue a payment is one prompt injection away from
    issuing a payment."""
    names = {s.tool for s in await build_claims_source(claims).list_tools()}

    assert names == {"my_claims", "get_claim", "add_note", "set_status"}
    assert not any(
        word in name for name in names for word in ("pay", "settle", "reserve", "transfer")
    )


async def test_claim_tools_are_classified_by_what_they_cost(claims: ClaimsBackend) -> None:
    specs = {s.tool: s for s in await build_claims_source(claims).list_tools()}

    assert specs["my_claims"].risk is RiskClass.READ
    # Claim text comes from claimants, contractors, loss adjusters and opposing
    # insurers. None of them are trusted authors.
    assert specs["my_claims"].returns_untrusted_content
    assert specs["get_claim"].returns_untrusted_content
    # A note becomes part of the file disclosed in a dispute.
    assert specs["add_note"].risk is RiskClass.IRREVERSIBLE
    assert specs["set_status"].risk is RiskClass.IRREVERSIBLE
