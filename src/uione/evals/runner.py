"""Eval case definition and execution.

A case is a scenario plus the assertions that must hold. Cases run against a real
model, so this is not part of the unit suite — CI must stay GPU-free. It runs as
a gate before a model or prompt change ships (gap **G6**).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from uione.evals.assertions import Assertion, AssertionResult, EvalOutput

log = structlog.get_logger(__name__)

Scenario = Callable[[str], Awaitable[EvalOutput]]
"""Takes a model name, produces the output to judge."""


@dataclass
class EvalCase:
    name: str
    description: str
    scenario: Scenario
    assertions: list[Assertion] = field(default_factory=list)
    suite: str = "default"


@dataclass
class CaseResult:
    case: EvalCase
    model: str
    results: list[AssertionResult] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(r.passed for r in self.results)

    @property
    def score(self) -> str:
        if self.error:
            return "ERROR"
        return f"{sum(r.passed for r in self.results)}/{len(self.results)}"

    @property
    def failures(self) -> list[AssertionResult]:
        return [r for r in self.results if not r.passed]


@dataclass
class SuiteResult:
    model: str
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    @property
    def summary(self) -> str:
        ok = sum(1 for c in self.cases if c.passed)
        return f"{ok}/{len(self.cases)} cases passed"

    @property
    def total_duration(self) -> float:
        return sum(c.duration_s for c in self.cases)


async def run_case(case: EvalCase, model: str) -> CaseResult:
    """Execute one case.

    A scenario that raises becomes a failed case rather than an aborted run: one
    broken connector must not hide the results of every other case.
    """
    started = time.perf_counter()
    try:
        output = await case.scenario(model)
    except Exception as exc:  # noqa: BLE001
        log.warning("evals.scenario_failed", case=case.name, error=str(exc))
        return CaseResult(
            case=case,
            model=model,
            duration_s=time.perf_counter() - started,
            error=f"{type(exc).__name__}: {exc}",
        )

    results = [assertion.check(output) for assertion in case.assertions]
    return CaseResult(
        case=case, model=model, results=results, duration_s=time.perf_counter() - started
    )


async def run_suite(cases: list[EvalCase], model: str) -> SuiteResult:
    suite = SuiteResult(model=model)
    for case in cases:
        result = await run_case(case, model)
        suite.cases.append(result)
        log.info("evals.case", case=case.name, model=model, score=result.score)
    return suite


async def run_repeated(cases: list[EvalCase], model: str, *, times: int) -> list[SuiteResult]:
    """Run the whole suite `times` over.

    Added because a single run persuaded this project's own documentation to
    conclude that a model had fixed two defects. It had not; the cases are
    intermittent, and runs two and three reproduced both. One run is a smoke
    test — see docs/EVALS.md.
    """
    return [await run_suite(cases, model) for _ in range(times)]


def render_rates(runs: list[SuiteResult]) -> str:
    """Pass *rate* per case across runs, worst first.

    Ordered by rate rather than by name on purpose. A case that passes 2 of 3
    times is the one worth reading, and sorting alphabetically buries it among
    the ones that always pass.
    """
    if not runs:
        return ""

    tally: dict[str, list[bool]] = {}
    for run in runs:
        for case in run.cases:
            tally.setdefault(case.case.name, []).append(case.passed)

    lines = [
        "",
        f"Model: {runs[0].model}   ({len(runs)} runs)",
        "=" * 78,
    ]
    for name, outcomes in sorted(tally.items(), key=lambda kv: (sum(kv[1]) / len(kv[1]), kv[0])):
        passes = sum(outcomes)
        total = len(outcomes)
        marks = "".join("." if ok else "X" for ok in outcomes)
        flag = "" if passes == total else ("  FLAKY" if passes else "  FAILING")
        lines.append(f"[{passes}/{total}] {name:<52} {marks}{flag}")

    lines.append("=" * 78)
    stable = sum(1 for o in tally.values() if all(o))
    flaky = sum(1 for o in tally.values() if any(o) and not all(o))
    failing = sum(1 for o in tally.values() if not any(o))
    lines.append(
        f"{stable} always passed, {flaky} intermittent, {failing} always failed"
        f"   ({sum(r.total_duration for r in runs):.0f}s total)"
    )
    if flaky:
        lines.append(
            "\nAn intermittent case is worse than a red one: it produces a green run "
            "\nwhenever somebody is looking for permission to ship."
        )
    return "\n".join(lines)


def render(suite: SuiteResult, *, verbose: bool = False) -> str:
    lines = [
        "",
        f"Model: {suite.model}",
        "=" * 78,
    ]
    for case in suite.cases:
        status = "PASS" if case.passed else "FAIL"
        lines.append(f"[{status}] {case.case.name:<34} {case.score:>7}  {case.duration_s:5.1f}s")
        if case.error:
            lines.append(f"         ERROR: {case.error}")
        # Failures always shown; passes only on request. A wall of green hides
        # the two red lines that matter.
        for result in case.results if verbose else case.failures:
            lines.append(f"         {result}")
    lines.append("=" * 78)
    lines.append(f"{suite.summary}   ({suite.total_duration:.1f}s total)")
    return "\n".join(lines)


def render_comparison(suites: list[SuiteResult]) -> str:
    """Side-by-side model comparison.

    The shape a model swap decision actually needs: not "is the new model good"
    but "which cases does it break that the current one passes".
    """
    if not suites:
        return "no results"

    names = [c.case.name for c in suites[0].cases]
    width = max(len(n) for n in names) + 2
    header = "CASE".ljust(width) + "".join(s.model[:18].ljust(20) for s in suites)
    lines = ["", header, "-" * len(header)]

    for index, name in enumerate(names):
        row = name.ljust(width)
        for suite in suites:
            case = suite.cases[index]
            row += f"{('PASS' if case.passed else 'FAIL')} {case.score}".ljust(20)
        lines.append(row)

    lines.append("-" * len(header))
    lines.append("TOTAL".ljust(width) + "".join(s.summary.ljust(20) for s in suites))
    return "\n".join(lines)
