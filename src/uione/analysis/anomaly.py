"""Deciding whether a number is worth waking someone up about.

The brief asks for alerting on BI-triggered anomalies. The hard part is not
detection — it is *restraint*. An assistant that reports every fluctuation as an
anomaly gets muted in a week, and a muted assistant is worse than no assistant,
because now nobody is watching and everyone believes somebody is.

So the design goal here is a low false-positive rate at the cost of sensitivity,
and the three choices that follow from it:

**Robust statistics, not the mean.** A single catastrophic day drags the mean
toward itself and inflates the standard deviation, so the very outlier we are
looking for makes the test less likely to fire — and the *next* day, back to
normal, may then look anomalous. The median and the median absolute deviation
have a breakdown point of 50%: half the history can be garbage before they move.

**Weekly seasonality is subtracted, not averaged over.** Payment volume on a
Sunday is not payment volume on a Tuesday. Compared against a flat baseline,
every Monday morning is an anomaly and every Saturday is a crisis. Points are
compared against the same weekday when there is enough history to do so.

**A minimum absolute change.** A metric that sits at 3 and moves to 6 is a 100%
increase and statistically enormous. It is also three. Percentage-only thresholds
are how monitoring systems end up paging about a counter that ticked.

What this deliberately is not: a forecasting model. There is no ARIMA, no
Prophet, no learned seasonality beyond day-of-week. Those need tuning, retraining
and someone who owns them, and an on-premise product that ships an unowned model
ships an unowned model that will be wrong in a year with nobody watching.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import structlog

log = structlog.get_logger(__name__)

#: Points needed before any judgement is made. Below this the honest answer is
#: "I don't know yet", which is a different thing from "nothing is wrong".
MIN_HISTORY = 7

#: Points needed per weekday before seasonal comparison is used. Three Tuesdays
#: is a thin baseline but enough to beat comparing a Tuesday against a Sunday.
MIN_SEASONAL = 3

#: Robust z-score threshold. 3.5 on a MAD-scaled score is the conventional
#: outlier cut and corresponds to roughly a 3.5-sigma event on normal data —
#: deliberately conservative, because the cost of crying wolf is being ignored.
THRESHOLD = 3.5

#: Scale factor making MAD a consistent estimator of the standard deviation for
#: normally distributed data. Without it every score is inflated by ~1.48 and the
#: threshold means something different from what it says.
MAD_TO_SIGMA = 0.6745


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


@dataclass(frozen=True)
class Point:
    at: datetime
    value: float

    @property
    def weekday(self) -> int:
        return self.at.weekday()


@dataclass
class Finding:
    """One metric's verdict, with the numbers that produced it."""

    metric: str
    value: float
    baseline: float
    score: float
    direction: Direction
    anomalous: bool
    reason: str
    seasonal: bool = False
    sample_size: int = 0

    #: When the assessed point was recorded. Carried so a report can state the
    #: day rather than leave a model to infer one — watched against a real
    #: model, "the same weekday baseline" with no date became "the drop on
    #: Friday", which was a confident invention about a day never mentioned.
    at: datetime | None = None
    #: Populated only when a judgement could not be made. Distinct from
    #: `anomalous=False`, which is a real "this looks normal".
    undetermined: str = ""

    @property
    def change_pct(self) -> float:
        if self.baseline == 0:
            return 0.0
        return round((self.value - self.baseline) / abs(self.baseline) * 100, 1)

    def render(self) -> str:
        if self.undetermined:
            return f"{self.metric}: {self.value:g} — not assessed ({self.undetermined})"
        if not self.anomalous:
            return f"{self.metric}: {self.value:g} (normal, baseline {self.baseline:g})"

        arrow = "up" if self.direction is Direction.UP else "down"
        when = f" on {self.at:%A %d %B}" if self.at else ""
        basis = (
            f"other {self.at:%A}s"
            if (self.seasonal and self.at)
            else ("same weekday" if self.seasonal else "recent history")
        )
        return (
            f"{self.metric}{when}: {self.value:g} — {arrow} {abs(self.change_pct):g}% "
            f"against a baseline of {self.baseline:g} from {basis} (score {self.score:g})"
        )


@dataclass
class Detector:
    """Robust, seasonal-aware, and biased against firing."""

    threshold: float = THRESHOLD

    #: Absolute movement below which nothing is reported however large the
    #: percentage. A counter going from 3 to 6 is not an incident.
    min_absolute_change: float = 1.0

    #: Ignore metrics whose whole history is essentially constant. A flat line
    #: has a MAD of zero, which makes every deviation infinitely significant —
    #: the classic way these detectors produce nonsense.
    min_variation: float = 1e-9

    def assess(self, metric: str, history: list[Point]) -> Finding:
        """Judge the most recent point against everything before it."""
        if len(history) < MIN_HISTORY + 1:
            return Finding(
                metric=metric,
                value=history[-1].value if history else 0.0,
                baseline=0.0,
                score=0.0,
                direction=Direction.FLAT,
                anomalous=False,
                reason="",
                sample_size=len(history),
                undetermined=f"needs {MIN_HISTORY + 1} points, has {len(history)}",
            )

        ordered = sorted(history, key=lambda p: p.at)
        current, past = ordered[-1], ordered[:-1]

        # Prefer the same weekday, fall back to everything. Stated on the
        # finding so a reader knows which comparison produced the number.
        seasonal_past = [p for p in past if p.weekday == current.weekday]
        seasonal = len(seasonal_past) >= MIN_SEASONAL
        sample = seasonal_past if seasonal else past

        values = [p.value for p in sample]
        baseline = statistics.median(values)
        deviations = [abs(v - baseline) for v in values]
        mad = statistics.median(deviations)

        if mad < self.min_variation:
            # A perfectly flat history. Dividing by this is how a detector
            # reports a 1% move as a twelve-sigma event.
            if abs(current.value - baseline) < max(self.min_absolute_change, 1e-9):
                return self._normal(
                    metric, current.value, baseline, seasonal, len(sample), at=current.at
                )
            return Finding(
                metric=metric,
                value=current.value,
                baseline=baseline,
                score=float("inf"),
                direction=_direction(current.value, baseline),
                anomalous=True,
                reason="the metric has been perfectly flat and has now moved",
                seasonal=seasonal,
                sample_size=len(sample),
                at=current.at,
            )

        score = abs(current.value - baseline) / (mad / MAD_TO_SIGMA)
        moved = abs(current.value - baseline)

        if moved < self.min_absolute_change:
            # Statistically significant, practically nothing. This is the check
            # that keeps a low-volume counter from paging anybody.
            return self._normal(
                metric, current.value, baseline, seasonal, len(sample), at=current.at
            )

        if score < self.threshold:
            return self._normal(
                metric, current.value, baseline, seasonal, len(sample), score, at=current.at
            )

        return Finding(
            metric=metric,
            value=current.value,
            baseline=baseline,
            score=round(score, 2),
            direction=_direction(current.value, baseline),
            anomalous=True,
            reason=(
                f"{moved:g} away from a baseline of {baseline:g}, "
                f"{score:.1f}x the typical variation"
            ),
            seasonal=seasonal,
            sample_size=len(sample),
            at=current.at,
        )

    def _normal(
        self,
        metric: str,
        value: float,
        baseline: float,
        seasonal: bool,
        size: int,
        score: float = 0.0,
        at: datetime | None = None,
    ) -> Finding:
        return Finding(
            metric=metric,
            value=value,
            baseline=baseline,
            score=round(score, 2),
            direction=_direction(value, baseline),
            anomalous=False,
            reason="",
            seasonal=seasonal,
            sample_size=size,
            at=at,
        )

    def assess_all(self, series: dict[str, list[Point]]) -> list[Finding]:
        """Judge several metrics, worst first.

        Ordering matters more than it looks: whatever is first is what gets read,
        and what gets read should be the thing most likely to need attention.
        """
        findings = [self.assess(metric, points) for metric, points in series.items()]
        return sorted(findings, key=lambda f: (-f.anomalous, -f.score))


def _direction(value: float, baseline: float) -> Direction:
    if value > baseline:
        return Direction.UP
    if value < baseline:
        return Direction.DOWN
    return Direction.FLAT


@dataclass
class Report:
    """What the brief actually says about the numbers."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def anomalies(self) -> list[Finding]:
        return [f for f in self.findings if f.anomalous]

    @property
    def undetermined(self) -> list[Finding]:
        return [f for f in self.findings if f.undetermined]

    def render(self) -> str:
        if not self.findings:
            return "No metrics were available."

        if not self.anomalies:
            checked = len(self.findings) - len(self.undetermined)
            text = f"Nothing unusual across {checked} metric(s)."
            if self.undetermined:
                # Said out loud. "Nothing unusual" while silently skipping half
                # the metrics is the sentence that destroys trust when found out.
                text += (
                    f" {len(self.undetermined)} could not be assessed: "
                    + ", ".join(f.metric for f in self.undetermined)
                    + "."
                )
            return text

        lines = [f.render() for f in self.anomalies]
        if self.undetermined:
            lines.append(f"Not assessed: {', '.join(f.metric for f in self.undetermined)}.")
        return "\n".join(lines)
