from __future__ import annotations

import pytest

from uione.connectors.demo import build_all
from uione.governance import Governor
from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
)
from uione.modelplane import Completion, ModelPlaneUnavailable
from uione.proactive import BriefGenerator, BriefSource

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")


class StubModel:
    def __init__(self, content: str = "Your morning brief.") -> None:
        self.content = content
        self.prompts: list[str] = []

    async def chat(self, messages, **kwargs):
        self.prompts.append(messages[-1].content or "")
        return Completion(content=self.content, model="stub-model")


class DeadModel:
    async def chat(self, messages, **kwargs):
        raise ModelPlaneUnavailable("engine down")


async def build_gateway(failing: set[str] | None = None) -> McpGateway:
    gateway = McpGateway(
        policy=ToolPolicy(
            [
                Grant(
                    role="analyst",
                    tools=frozenset({"mail.*", "tasks.*", "incidents.*", "calendar.*"}),
                    max_risk=RiskClass.READ,
                )
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
        governor=Governor(),
    )
    for source in build_all(failing=failing):
        await gateway.register(source)
    return gateway


@pytest.fixture
async def gateway() -> McpGateway:
    return await build_gateway()


# -- the happy path --------------------------------------------------------


async def test_brief_is_generated_from_all_sources(gateway: McpGateway) -> None:
    model = StubModel()

    brief = await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    assert brief.body == "Your morning brief."
    assert brief.complete
    assert len(brief.sections) == 4
    assert all(s.ok for s in brief.sections)


async def test_every_section_records_its_source(gateway: McpGateway) -> None:
    """A claim the user cannot trace is a claim they must verify by hand."""
    brief = await BriefGenerator(model=StubModel(), gateway=gateway).generate(ALICE)

    assert brief.provenance == {
        "incidents": "incidents.active",
        "mail": "mail.list_unread",
        "calendar": "calendar.today",
        "tasks": "tasks.my_open_issues",
    }


async def test_retrieved_data_reaches_the_prompt(gateway: McpGateway) -> None:
    model = StubModel()

    await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    prompt = model.prompts[0]
    assert "INC-4471" in prompt
    assert "PAY-1182" in prompt
    assert "Q3 budget review" in prompt


# -- honest degradation ----------------------------------------------------


async def test_a_dead_connector_does_not_stop_the_brief() -> None:
    model = StubModel()
    gateway = await build_gateway(failing={"tasks"})

    brief = await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    assert brief.body
    assert [s.section for s in brief.sections if s.ok] == ["incidents", "mail", "calendar"]


async def test_degradation_is_reported_not_hidden() -> None:
    """Silently omitting a section is worse than the outage it hides."""
    gateway = await build_gateway(failing={"incidents"})

    brief = await BriefGenerator(model=StubModel(), gateway=gateway).generate(ALICE)

    assert not brief.complete
    assert brief.degraded_sources == ["incidents"]


async def test_the_model_is_told_which_systems_are_down() -> None:
    model = StubModel()
    gateway = await build_gateway(failing={"incidents", "tasks"})

    await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    prompt = model.prompts[0]
    assert "SYSTEMS UNAVAILABLE" in prompt
    assert "Active incidents" in prompt
    assert "could not be checked" in prompt


async def test_failed_section_carries_the_reason() -> None:
    gateway = await build_gateway(failing={"mail"})

    brief = await BriefGenerator(model=StubModel(), gateway=gateway).generate(ALICE)

    mail = next(s for s in brief.sections if s.section == "mail")
    assert not mail.ok
    assert "unreachable" in (mail.error or "")


async def test_all_connectors_down_still_produces_a_brief() -> None:
    gateway = await build_gateway(failing=set(ALL := {"mail", "tasks", "incidents", "calendar"}))

    brief = await BriefGenerator(model=StubModel(), gateway=gateway).generate(ALICE)

    assert brief.degraded_sources == ["incidents", "mail", "calendar", "tasks"]
    assert len(ALL) == 4


async def test_model_outage_falls_back_to_raw_data(gateway: McpGateway) -> None:
    """The facts were the point; a brief without prose beats an error page."""
    brief = await BriefGenerator(model=DeadModel(), gateway=gateway).generate(ALICE)

    assert "INC-4471" in brief.body
    assert brief.error and "summary unavailable" in brief.error


# -- containment -----------------------------------------------------------


async def test_external_mail_is_quarantined_in_the_brief_prompt(gateway: McpGateway) -> None:
    model = StubModel()

    brief = await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    assert "trust=untrusted" in model.prompts[0]
    assert brief.taint.tainted


async def test_internal_sources_are_not_marked_untrusted(gateway: McpGateway) -> None:
    model = StubModel()

    await BriefGenerator(model=model, gateway=gateway).generate(ALICE)

    prompt = model.prompts[0]
    calendar_block = prompt.split("Today's schedule")[1].split("##")[0]
    assert "trust=untrusted" not in calendar_block
    assert "trust=internal" in calendar_block


async def test_permissions_apply_to_the_brief() -> None:
    """A brief must not become a way around the tool policy."""
    gateway = McpGateway(
        policy=ToolPolicy([Grant(role="analyst", tools=frozenset({"calendar.*"}))]),
        audit=AuditLog(InMemoryAuditSink()),
    )
    for source in build_all():
        await gateway.register(source)

    brief = await BriefGenerator(model=StubModel(), gateway=gateway).generate(ALICE)

    assert [s.section for s in brief.sections if s.ok] == ["calendar"]


# -- work graph integration ------------------------------------------------


async def test_cross_system_links_reach_the_prompt() -> None:
    """The join the model previously had to notice by luck is now given to it."""
    from uione.knowledge import ExtractionRules

    model = StubModel()
    gateway = await build_gateway()
    generator = BriefGenerator(
        model=model,
        gateway=gateway,
        extraction_rules=ExtractionRules(
            ticket_prefixes=frozenset({"PAY"}),
            incident_prefixes=frozenset({"INC"}),
            reference_prefixes=frozenset({"INV"}),
        ),
    )

    brief = await generator.generate(ALICE)

    prompt = model.prompts[0]
    assert "CONNECTIONS" in prompt
    assert "INC-4471" in brief.connections
    assert "INV-88213" in brief.connections


async def test_connections_are_absent_when_nothing_links() -> None:
    """No spurious connections block when the systems share nothing."""
    from uione.knowledge import ExtractionRules

    model = StubModel()
    gateway = await build_gateway()
    generator = BriefGenerator(
        model=model,
        gateway=gateway,
        # Every prefix set cleared, so nothing is recognised as an identifier.
        # INC/INV are defaults because those conventions are near-universal.
        extraction_rules=ExtractionRules(
            ticket_prefixes=frozenset(),
            incident_prefixes=frozenset(),
            reference_prefixes=frozenset(),
            extract_people=False,
        ),
    )

    brief = await generator.generate(ALICE)

    assert brief.connections == []
    assert "CONNECTIONS" not in model.prompts[0]


async def test_unavailable_sections_are_not_indexed() -> None:
    """A failed section has no content; it must not appear as a phantom link."""
    from uione.knowledge import ExtractionRules

    gateway = await build_gateway(failing={"incidents"})
    generator = BriefGenerator(
        model=StubModel(),
        gateway=gateway,
        extraction_rules=ExtractionRules(incident_prefixes=frozenset({"INC"})),
    )

    brief = await generator.generate(ALICE)

    # INC-4471 still appears via the alert email, but the incidents section
    # itself contributed nothing.
    assert "incidents" not in brief.provenance


# -- which sources a deployment actually has -------------------------------


async def test_a_connector_this_deployment_lacks_does_not_degrade_the_brief() -> None:
    """ "Degraded" has to mean *a system we have is down*.

    If it also means "we never had a claims system", every brief in every
    deployment carries a warning banner — and a banner that is always on is a
    banner nobody reads.
    """
    generator = BriefGenerator(
        model=StubModel(),
        gateway=await build_gateway(),
        sources=(
            BriefSource("mail", "mail.list_unread", {"limit": 5}, heading="Unread mail"),
            BriefSource("claims", "claims.my_claims", heading="Your claims"),
        ),
    )

    brief = await generator.generate(ALICE)

    assert brief.complete
    assert [s.section for s in brief.sections] == ["mail"]


async def test_a_section_falls_back_to_the_tool_this_deployment_has() -> None:
    """A fixture connector and the real one do not agree on tool names.

    Without alternatives, configuring a real incident system makes the incidents
    section vanish from everyone's brief — quietly, because the tool the brief
    names simply no longer exists.
    """
    generator = BriefGenerator(
        model=StubModel(),
        gateway=await build_gateway(),
        sources=(
            BriefSource(
                "incidents",
                "incidents.does_not_exist",
                heading="Active incidents",
                alternatives=("incidents.active",),
            ),
        ),
    )

    brief = await generator.generate(ALICE)

    assert brief.complete
    assert brief.sections[0].tool == "incidents.active"


async def test_the_preferred_tool_wins_when_both_exist() -> None:
    generator = BriefGenerator(
        model=StubModel(),
        gateway=await build_gateway(),
        sources=(
            BriefSource(
                "incidents",
                "incidents.active",
                heading="Active incidents",
                alternatives=("incidents.detail",),
            ),
        ),
    )

    brief = await generator.generate(ALICE)

    assert brief.sections[0].tool == "incidents.active"


async def test_a_connector_that_is_present_but_failing_still_degrades() -> None:
    """The distinction the two tests above depend on."""
    generator = BriefGenerator(
        model=StubModel(),
        gateway=await build_gateway(failing={"incidents"}),
        sources=(BriefSource("incidents", "incidents.active", heading="Active incidents"),),
    )

    brief = await generator.generate(ALICE)

    assert not brief.complete
    assert brief.degraded_sources == ["incidents"]
