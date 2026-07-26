"""The assertions are themselves tested.

An eval harness with buggy assertions is worse than no harness: it produces
confident green while the defects it was built to catch sail through. These run
in CI; the suites they support need a real model and do not.
"""

from __future__ import annotations

import pytest

from uione.evals import (
    Absent,
    ActionHeld,
    Contains,
    EvalCase,
    EvalOutput,
    FactMatches,
    NoInventedIdentifiers,
    NoWritesExecuted,
    ReportsUnavailability,
    ToolCalled,
    ToolNotCalled,
    render,
    render_comparison,
    run_case,
    run_suite,
)
from uione.knowledge import ExtractionRules

RULES = ExtractionRules(
    ticket_prefixes=frozenset({"PAY"}),
    incident_prefixes=frozenset({"INC"}),
    reference_prefixes=frozenset({"INV"}),
)
KNOWN = frozenset({"PAY-1182", "PAY-1190", "INC-4471", "INV-88213"})


def out(text: str = "", **kwargs) -> EvalOutput:
    return EvalOutput(text=text, **kwargs)


# -- Contains / Absent -----------------------------------------------------


def test_contains_is_case_insensitive() -> None:
    assert Contains("inc-4471").check(out("Incident INC-4471 is open")).passed


def test_contains_fails_when_missing() -> None:
    result = Contains("INC-4471").check(out("nothing here"))
    assert not result.passed
    assert "not present" in result.detail


def test_absent_passes_when_missing() -> None:
    assert Absent("evil.example").check(out("all clear")).passed


def test_absent_fails_when_present() -> None:
    assert not Absent("evil.example").check(out("sent to collector@evil.example")).passed


# -- FactMatches: the due-date assertion -----------------------------------


def test_fact_matches_accepts_the_fixture_value() -> None:
    text = "- [PAY-1182] Reconcile invoice (due 2026-07-31)"
    assert FactMatches("PAY-1182", r"\d{4}-\d{2}-\d{2}", "2026-07-31").check(out(text)).passed


def test_fact_matches_catches_the_observed_hallucination() -> None:
    """The exact defect from docs/MORNING_BRIEF.md."""
    text = "- [PAY-1182] Reconcile invoice (due 2026-07-28)"

    result = FactMatches("PAY-1182", r"\d{4}-\d{2}-\d{2}", "2026-07-31").check(out(text))

    assert not result.passed
    assert "2026-07-28" in result.detail
    assert "2026-07-31" in result.detail


def test_fact_matches_only_looks_near_the_anchor() -> None:
    """A document-wide search would pass whenever the value appears anywhere."""
    text = "- [PAY-1182] due 2026-07-28\n" + "filler\n" * 40 + "- [PAY-1190] due 2026-07-31"

    result = FactMatches("PAY-1182", r"\d{4}-\d{2}-\d{2}", "2026-07-31", window=60).check(out(text))

    assert not result.passed


def test_omitting_the_fact_is_allowed() -> None:
    """A brief may legitimately not state a due date; stating a wrong one is the failure."""
    result = FactMatches("PAY-1182", r"\d{4}-\d{2}-\d{2}", "2026-07-31").check(
        out("- [PAY-1182] Reconcile the supplier invoice")
    )

    assert result.passed
    assert "not stated" in result.detail


def test_missing_anchor_fails() -> None:
    result = FactMatches("PAY-1182", r"\d{4}", "2026").check(out("nothing about that ticket"))
    assert not result.passed
    assert "not mentioned" in result.detail


# -- NoInventedIdentifiers -------------------------------------------------


def test_known_identifiers_pass() -> None:
    text = "See PAY-1182 and INC-4471 regarding INV-88213."
    assert NoInventedIdentifiers(known=KNOWN, rules=RULES).check(out(text)).passed


def test_invented_identifier_is_caught() -> None:
    """A fabricated ticket key is indistinguishable from a real one to the user."""
    result = NoInventedIdentifiers(known=KNOWN, rules=RULES).check(
        out("Also see PAY-9999 for context.")
    )

    assert not result.passed
    assert "PAY-9999" in result.detail


def test_case_differences_are_not_treated_as_inventions() -> None:
    assert NoInventedIdentifiers(known=KNOWN, rules=RULES).check(out("see pay-1182")).passed


def test_unrelated_hyphenated_tokens_are_not_identifiers() -> None:
    """ISO-9001 must not be reported as a hallucinated ticket."""
    result = NoInventedIdentifiers(known=KNOWN, rules=RULES).check(
        out("Per ISO-9001 and COVID-19 policy.")
    )
    assert result.passed


def test_empty_output_invents_nothing() -> None:
    assert NoInventedIdentifiers(known=KNOWN, rules=RULES).check(out("")).passed


# -- tool and governance assertions ---------------------------------------


def test_tool_called_and_not_called() -> None:
    output = out(tools_called=["tasks.my_open_issues"])

    assert ToolCalled("tasks.my_open_issues").check(output).passed
    assert ToolNotCalled("mail.send_reply").check(output).passed
    assert not ToolCalled("mail.send_reply").check(output).passed


def test_tool_not_called_reports_what_was_called() -> None:
    result = ToolCalled("tasks.my_open_issues").check(out(tools_called=[]))
    assert "nothing" in result.detail


def test_no_writes_executed() -> None:
    assert NoWritesExecuted().check(out()).passed
    assert not NoWritesExecuted().check(out(executed_writes=["send:evil"])).passed


def test_action_held() -> None:
    assert ActionHeld().check(out(held_actions=["abc123"])).passed
    assert not ActionHeld().check(out()).passed


# -- ReportsUnavailability -------------------------------------------------


def test_unavailability_reported_explicitly_passes() -> None:
    text = "The incidents system was unavailable, so I could not check it."
    assert ReportsUnavailability("incident").check(out(text)).passed


def test_silently_dropping_a_section_fails() -> None:
    """The observed failure: the model omitted tasks rather than flagging Jira."""
    result = ReportsUnavailability("task").check(out("Here are your incidents and mail."))

    assert not result.passed
    assert "not mentioned" in result.detail


def test_mentioning_without_flagging_fails() -> None:
    result = ReportsUnavailability("task").check(out("Tasks: PAY-1182 is in progress."))

    assert not result.passed
    assert "not flagged" in result.detail


# -- runner ----------------------------------------------------------------


async def test_case_passes_when_all_assertions_hold() -> None:
    case = EvalCase(
        name="x",
        description="d",
        scenario=lambda _model: _echo("INC-4471 is open"),
        assertions=[Contains("INC-4471")],
    )

    result = await run_case(case, "test-model")

    assert result.passed
    assert result.score == "1/1"


async def test_a_failing_scenario_becomes_a_failed_case_not_an_abort() -> None:
    """One broken connector must not hide every other case's result."""

    async def explode(_model: str) -> EvalOutput:
        raise ConnectionError("engine down")

    case = EvalCase(name="x", description="d", scenario=explode, assertions=[Contains("y")])

    result = await run_case(case, "m")

    assert not result.passed
    assert result.score == "ERROR"
    assert "ConnectionError" in (result.error or "")


async def test_suite_reports_partial_failure() -> None:
    good = EvalCase(
        name="g", description="", scenario=lambda _m: _echo("ok"), assertions=[Contains("ok")]
    )
    bad = EvalCase(
        name="b", description="", scenario=lambda _m: _echo("ok"), assertions=[Contains("missing")]
    )

    suite = await run_suite([good, bad], "m")

    assert not suite.passed
    assert suite.summary == "1/2 cases passed"


async def test_render_shows_failures_but_not_every_pass() -> None:
    """A wall of green hides the two red lines that matter."""
    case = EvalCase(
        name="c",
        description="",
        scenario=lambda _m: _echo("hello"),
        assertions=[Contains("hello"), Contains("goodbye")],
    )
    suite = await run_suite([case], "m")

    quiet = render(suite)
    loud = render(suite, verbose=True)

    assert "goodbye" in quiet
    assert "hello" not in quiet
    assert "hello" in loud


async def test_comparison_table_lists_every_model() -> None:
    case = EvalCase(
        name="c", description="", scenario=lambda _m: _echo("x"), assertions=[Contains("x")]
    )
    suites = [await run_suite([case], "model-a"), await run_suite([case], "model-b")]

    table = render_comparison(suites)

    assert "model-a" in table and "model-b" in table


async def _echo(text: str) -> EvalOutput:
    return EvalOutput(text=text)


# -- the suites are well-formed -------------------------------------------


def test_every_case_has_assertions() -> None:
    from uione.evals.suites import ALL_CASES

    assert ALL_CASES
    for case in ALL_CASES:
        assert case.assertions, f"{case.name} asserts nothing"
        assert case.description, f"{case.name} has no description"


def test_case_names_are_unique() -> None:
    from uione.evals.suites import ALL_CASES

    names = [c.name for c in ALL_CASES]
    assert len(names) == len(set(names))


def test_known_ids_match_the_fixtures() -> None:
    """If fixtures change, the expected identifier set must change with them."""
    from uione.connectors.demo import INCIDENTS, TASKS
    from uione.evals.suites import KNOWN_IDS

    for task in TASKS:
        assert task["key"] in KNOWN_IDS
    for incident in INCIDENTS:
        assert incident["id"] in KNOWN_IDS


@pytest.mark.parametrize("suite", ["brief", "agent", "safety"])
def test_suites_are_registered(suite: str) -> None:
    from uione.evals.suites import SUITES

    assert SUITES[suite]
