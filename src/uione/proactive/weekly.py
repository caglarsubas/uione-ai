"""The weekly review.

Until now this job existed in name only: it ran the morning brief and changed
the greeting to "Here is your week". That is worse than not having it, because
the label promises a different kind of thinking and delivers the same list of
today's unread mail.

A week is not a longer day. A morning brief answers "what needs me now"; a
weekly review answers "what changed, and is any of it unusual" — which is a
question about *history*, and history is the one thing the connectors cannot
answer. That is what the daily census is for.

**The numbers come first and the model comes second.** Every figure in the
report is computed here, by arithmetic, and handed to the model as text it may
rephrase but not recalculate. Asking a model to compare this week's incident
count with last week's is asking for a plausible number, and the recurring
finding in `docs/EVALS.md` is that open-weight models invent field values with
complete confidence. So the model writes the prose; it never does the sums.

**Silence about a metric is stated, not implied.** A week with too little
history says so per metric. "Nothing unusual" over three metrics while eight
were skipped is the sentence that ends trust the day somebody checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog

from uione.agent.language import with_proactive_language
from uione.analysis.anomaly import Detector, Finding, Point, Report
from uione.modelplane import ChatMessage, ModelPlaneClient, ModelPlaneError, TaskClass
from uione.modelplane.admission import Priority

log = structlog.get_logger(__name__)

WEEKLY_SYSTEM_PROMPT = """You are UiOne, writing a colleague's weekly review.

You are given figures that have already been computed. Your job is to explain \
them, not to recalculate them.

Rules:
1. Never state a number that is not in the data you were given. Do not estimate, \
round, or infer a figure.
2. Lead with whatever is genuinely unusual. If nothing is, say so in one line \
and keep the review short — a quiet week deserves a short review.
3. Say plainly which metrics could not be assessed, if any.
4. Two or three concrete suggestions at the end, each tied to a number above.
5. No preamble. No "here is your weekly review".
"""


@dataclass
class Movement:
    """One metric, this week against last."""

    metric: str
    title: str
    current: float
    previous: float
    days_observed: int

    @property
    def change(self) -> float:
        return self.current - self.previous

    @property
    def change_pct(self) -> float:
        if self.previous == 0:
            return 0.0
        return round(self.change / abs(self.previous) * 100, 1)

    @property
    def direction(self) -> str:
        if self.change > 0:
            return "up"
        return "down" if self.change < 0 else "unchanged"

    def render(self) -> str:
        if self.change == 0:
            return f"{self.title}: {self.current:g}, unchanged from last week"
        # The percentage is omitted when last week was zero, because "up 100%
        # from 0" is a division nobody meant and reads as precision.
        percent = f" ({abs(self.change_pct):g}%)" if self.previous else ""
        return (
            f"{self.title}: {self.current:g}, {self.direction} "
            f"{abs(self.change):g}{percent} from {self.previous:g} last week"
        )


@dataclass
class WeeklyReport:
    principal_id: str
    generated_at: datetime
    body: str
    movements: list[Movement] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    model: str = ""
    error: str | None = None

    @property
    def anomalies(self) -> list[Finding]:
        return [f for f in self.findings if f.anomalous]

    @property
    def unassessed(self) -> list[str]:
        return [f.metric for f in self.findings if f.undetermined]

    @property
    def has_history(self) -> bool:
        return bool(self.movements or self.findings)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compare_weeks(
    series: dict[str, list[Point]], *, now: datetime, titles: dict[str, str] | None = None
) -> list[Movement]:
    """Average of the last seven days against the seven before.

    Averages rather than endpoints: comparing today's number with the number
    exactly seven days ago makes the whole review depend on which two days those
    happened to be. One bank holiday and every metric looks like a crisis.
    """
    this_week_from = now - timedelta(days=7)
    last_week_from = now - timedelta(days=14)

    movements: list[Movement] = []
    for metric, points in series.items():
        current = [p.value for p in points if p.at > this_week_from]
        previous = [p.value for p in points if last_week_from < p.at <= this_week_from]
        if not current or not previous:
            # No comparison is possible, and inventing one from a single day
            # would be the most confident number in the report.
            continue
        movements.append(
            Movement(
                metric=metric,
                title=(titles or {}).get(metric, metric.replace("_", " ")),
                current=round(_mean(current), 1),
                previous=round(_mean(previous), 1),
                days_observed=len(current),
            )
        )

    # Largest absolute movement first: what leads is what gets read.
    return sorted(movements, key=lambda m: -abs(m.change))


class WeeklyReviewGenerator:
    def __init__(
        self,
        *,
        model: ModelPlaneClient,
        store,
        detector: Detector | None = None,
        titles: dict[str, str] | None = None,
        system_prompt: str = WEEKLY_SYSTEM_PROMPT,
        locale: str = "en",
    ) -> None:
        self._model = model
        self._store = store
        self._detector = detector or Detector()
        self._titles = titles or {}
        self._system_prompt = with_proactive_language(system_prompt, locale)

    async def generate(self, principal, *, now: datetime | None = None) -> WeeklyReport:
        moment = now or datetime.now(UTC)
        series = await self._store.history(principal.user_id, days=45)

        if not series:
            # Said plainly rather than dressed up. A deployment that started
            # yesterday has nothing to review, and pretending otherwise is how
            # the first weekly review becomes the last one anybody opens.
            return WeeklyReport(
                principal_id=principal.user_id,
                generated_at=moment,
                body=(
                    "No history yet — the daily figures start accumulating from the first "
                    "full day this assistant runs. A review needs about two weeks before it "
                    "can compare anything."
                ),
            )

        movements = compare_weeks(series, now=moment, titles=self._titles)
        findings = self._detector.assess_all(series)
        report = Report(findings=findings)

        facts = _render_facts(movements, report, self._titles)
        prompt = (
            f"Figures for {principal.display_name or principal.user_id}, "
            f"week ending {moment:%Y-%m-%d}:\n\n{facts}\n\n"
            "Write the weekly review."
        )

        try:
            completion = await self._model.chat(
                [
                    ChatMessage(role="system", content=self._system_prompt),
                    ChatMessage(role="user", content=prompt),
                ],
                task=TaskClass.REASONING,
                priority=Priority.BACKGROUND,
            )
        except ModelPlaneError as exc:
            log.warning("weekly.model_unavailable", error=str(exc))
            # The figures are the report. Losing the prose is a degradation;
            # losing the numbers would be a failure.
            return WeeklyReport(
                principal_id=principal.user_id,
                generated_at=moment,
                body=facts,
                movements=movements,
                findings=findings,
                error=f"summary unavailable ({type(exc).__name__}); showing the figures",
            )

        return WeeklyReport(
            principal_id=principal.user_id,
            generated_at=moment,
            body=completion.content.strip(),
            movements=movements,
            findings=findings,
            model=completion.model,
        )


def _render_facts(movements: list[Movement], report: Report, titles: dict[str, str]) -> str:
    """Everything the model is allowed to know, as arithmetic it cannot redo."""
    lines: list[str] = []

    # The two blocks answer different questions and are labelled to say so.
    # Watched against a real model, an earlier version headed them "Week on
    # week" and "Unusual against this metric's own history", and the model
    # merged a week-average movement with a single-day anomaly into one
    # sentence — "dropped 57.1% week over week (3 vs 7)" — where 3 and 7 came
    # from the *other* comparison. Both numbers were real; the sentence was not.
    if movements:
        lines.append("COMPARISON A — this week's daily average against last week's:")
        lines += [f"  {m.render()}" for m in movements]
    else:
        lines.append("COMPARISON A — week on week: not enough history to compare.")

    anomalies = report.anomalies
    lines.append("")
    lines.append(
        "COMPARISON B — the single most recent day against this metric's own "
        "history. Different from A: A is an average over seven days, B is one day."
    )
    if anomalies:
        lines += [f"  {f.render()}" for f in anomalies]
    else:
        lines.append("  Nothing statistically unusual.")

    if unassessed := report.undetermined:
        lines.append("")
        lines.append(
            "Not assessed (too little history): "
            + ", ".join(titles.get(f.metric, f.metric) for f in unassessed)
        )

    return "\n".join(lines)
