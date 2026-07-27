"""Grafana alerts, and deciding what counts as unusual.

The connector runs against a mock in CI and against a real Grafana when
`UIONE_TEST_GRAFANA_URL` and `UIONE_TEST_GRAFANA_TOKEN` are set. The detector
needs neither: it is arithmetic, and the interesting cases are the ones where
naive arithmetic gets it wrong.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from uione.analysis import Detector, Direction, Point, Report
from uione.connectors.bi import (
    GrafanaBI,
    alert_name,
    build_grafana_source,
    grafana_config,
    sort_alerts,
    visible_labels,
)
from uione.mcphub import RiskClass
from uione.vendormocks.grafana import build_grafana_mock, seed_grafana

REAL_URL = os.environ.get("UIONE_TEST_GRAFANA_URL", "")
REAL_TOKEN = os.environ.get("UIONE_TEST_GRAFANA_TOKEN", "")

MONDAY = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def bi() -> GrafanaBI:
    app = build_grafana_mock(seed_grafana())
    return GrafanaBI(
        grafana_config("http://grafana.mock", "token"),
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://grafana.mock"
        ),
    )


def series(values: list[float], *, start: datetime = MONDAY, step_days: int = 1) -> list[Point]:
    return [Point(at=start + timedelta(days=i * step_days), value=v) for i, v in enumerate(values)]


# -- alerts ----------------------------------------------------------------


async def test_firing_alerts_are_reported(bi: GrafanaBI) -> None:
    result = await build_grafana_source(bi).call("firing_alerts", {})

    assert result.ok
    assert "Settlement failure rate" in result.content
    assert result.structured["count"] == 2


async def test_a_resolved_alert_is_not_reported_as_current(bi: GrafanaBI) -> None:
    """The endpoint returns resolved alerts too, and only `status.state` says so.

    Reporting one at 07:30 describes a past crisis in the present tense to
    somebody who then acts on it.
    """
    result = await build_grafana_source(bi).call("firing_alerts", {})

    assert "Disk space low" not in result.content


async def test_the_most_severe_alert_is_first(bi: GrafanaBI) -> None:
    """Whatever is first is what gets read. Sorting by time alone puts a disk
    warning above a payments outage because it started earlier."""
    result = await build_grafana_source(bi).call("firing_alerts", {})

    assert result.structured["severities"][0] == "critical"
    assert result.content.index("Settlement failure") < result.content.index("Refund latency")


async def test_a_runbook_link_survives_to_the_reader(bi: GrafanaBI) -> None:
    """The single most useful thing to hand somebody at 07:30."""
    result = await build_grafana_source(bi).call("firing_alerts", {})

    assert "http://wiki.local/runbooks/settlement" in result.content


def test_grafanas_internal_labels_are_not_shown() -> None:
    """In a person's summary they are noise; in a model's context they are
    tokens spent on nothing."""
    alert = {
        "labels": {
            "alertname": "x",
            "team": "payments",
            "__alert_rule_uid__": "abc",
            "grafana_folder": "Payments",
        }
    }

    assert visible_labels(alert) == {"alertname": "x", "team": "payments"}


def test_an_alerts_name_comes_from_its_labels() -> None:
    """Not a `name` field, which is where a first guess puts it."""
    assert alert_name({"labels": {"alertname": "Settlement failing"}}) == "Settlement failing"
    assert alert_name({}) == "(unnamed alert)"


def test_alerts_without_severity_sort_last_rather_than_crashing() -> None:
    ordered = sort_alerts(
        [
            {"labels": {"alertname": "b"}, "startsAt": "2026-01-01"},
            {"labels": {"alertname": "a", "severity": "critical"}, "startsAt": "2026-01-02"},
        ]
    )

    assert alert_name(ordered[0]) == "a"


# -- the silent failure ----------------------------------------------------


async def test_a_rule_that_stopped_evaluating_is_surfaced(bi: GrafanaBI) -> None:
    """The state where nobody is being told anything and everyone assumes they
    would be. A rule in `error` never appears in the alert list at all."""
    result = await build_grafana_source(bi).call("firing_alerts", {})

    assert "Chargeback ratio by acquirer" in result.content
    assert "datasource 'acquirer-metrics' not found" in result.content
    assert result.structured["unhealthy_rules"] == 1


async def test_no_alerts_but_a_broken_rule_does_not_say_all_clear() -> None:
    """ "Nothing is firing" is a different claim from "nothing is wrong"."""
    from uione.vendormocks.grafana import State

    state = State()
    state.add_rule("Chargeback ratio", health="error", error="datasource missing")
    app = build_grafana_mock(state)
    quiet = GrafanaBI(
        grafana_config("http://grafana.mock", "t"),
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://grafana.mock"
        ),
    )

    result = await build_grafana_source(quiet).call("firing_alerts", {})

    assert "No alerts are firing" in result.content
    assert "not evaluating" in result.content
    assert result.structured["unhealthy_rules"] == 1


# -- what BI deliberately cannot do ----------------------------------------


async def test_there_is_no_write_tool_at_all(bi: GrafanaBI) -> None:
    """Grafana's write surface is silences, dashboard edits and rule changes —
    each of which either hides a problem or alters what everyone else sees."""
    specs = await build_grafana_source(bi).list_tools()

    assert {s.tool for s in specs} == {"firing_alerts", "list_dashboards"}
    assert all(s.risk is RiskClass.READ for s in specs)
    assert all(s.returns_untrusted_content for s in specs)


# -- the detector: the cases naive arithmetic gets wrong --------------------


def test_a_genuine_spike_is_caught() -> None:
    detector = Detector()

    finding = detector.assess("failures", series([10, 11, 9, 10, 12, 10, 11, 10, 95]))

    assert finding.anomalous
    assert finding.direction is Direction.UP


def test_a_drop_to_zero_is_caught() -> None:
    """The one people forget. A payment volume of zero is not a quiet day."""
    finding = Detector().assess("volume", series([500, 520, 480, 505, 495, 510, 500, 490, 0]))

    assert finding.anomalous
    assert finding.direction is Direction.DOWN


def test_ordinary_variation_is_not_an_anomaly() -> None:
    """The property that decides whether anyone still reads these in a month."""
    finding = Detector().assess("volume", series([500, 520, 480, 505, 495, 510, 500, 490, 515]))

    assert not finding.anomalous


def test_one_bad_day_does_not_hide_the_next_one() -> None:
    """With mean and standard deviation, a single catastrophic day inflates the
    spread so much that the *next* anomaly falls inside it. The median and MAD
    do not move."""
    history = series([10, 11, 9, 10, 400, 10, 11, 9, 10, 380])

    finding = Detector().assess("failures", history)

    assert finding.anomalous, "a second spike must still register after the first"


def test_a_weekend_is_not_an_anomaly() -> None:
    """Compared against a flat baseline every Saturday looks like a crisis."""
    points: list[Point] = []
    start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # a Monday
    for week in range(4):
        for day in range(7):
            at = start + timedelta(days=week * 7 + day)
            # Weekdays ~500, weekends ~80.
            points.append(Point(at=at, value=500 if at.weekday() < 5 else 80))
    # The next Saturday, entirely normal for a Saturday.
    points.append(Point(at=start + timedelta(days=26), value=82))

    finding = Detector().assess("volume", points)

    assert not finding.anomalous
    assert finding.seasonal, "the comparison must be against other Saturdays"


def test_a_weekday_collapse_is_still_caught_under_seasonality() -> None:
    points: list[Point] = []
    start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    for week in range(4):
        for day in range(7):
            at = start + timedelta(days=week * 7 + day)
            points.append(Point(at=at, value=500 if at.weekday() < 5 else 80))
    # A Monday at weekend levels — normal for a Saturday, alarming for a Monday.
    points.append(Point(at=start + timedelta(days=28), value=80))

    finding = Detector().assess("volume", points)

    assert finding.anomalous
    assert finding.seasonal


def test_a_tiny_counter_does_not_page_anybody() -> None:
    """3 to 6 is a 100% increase and statistically enormous. It is also three."""
    finding = Detector(min_absolute_change=10).assess(
        "signups", series([3, 3, 4, 3, 3, 4, 3, 3, 6])
    )

    assert not finding.anomalous


def test_a_perfectly_flat_metric_does_not_produce_infinite_significance() -> None:
    """A MAD of zero makes every deviation infinitely significant — the classic
    way these detectors generate nonsense."""
    finding = Detector().assess("constant", series([7, 7, 7, 7, 7, 7, 7, 7, 7]))

    assert not finding.anomalous
    assert finding.score != float("inf")


def test_a_flat_metric_that_finally_moves_is_reported() -> None:
    finding = Detector().assess("errors", series([0, 0, 0, 0, 0, 0, 0, 0, 25]))

    assert finding.anomalous


def test_too_little_history_is_undetermined_not_normal() -> None:
    """ "I don't know yet" is a different answer from "nothing is wrong", and
    conflating them is how a new metric goes unwatched."""
    finding = Detector().assess("new-metric", series([5, 6, 7]))

    assert not finding.anomalous
    assert finding.undetermined
    assert "needs" in finding.undetermined


def test_points_out_of_order_are_sorted_before_judging() -> None:
    ordered = series([10, 11, 9, 10, 12, 10, 11, 10, 95])
    shuffled = [ordered[4], ordered[0], ordered[8], *ordered[1:4], *ordered[5:8]]

    assert Detector().assess("failures", shuffled).anomalous


# -- the report ------------------------------------------------------------


def test_the_report_leads_with_what_matters() -> None:
    detector = Detector()
    findings = detector.assess_all(
        {
            "quiet": series([10, 10, 11, 10, 10, 11, 10, 10, 10]),
            "spiking": series([10, 11, 9, 10, 12, 10, 11, 10, 200]),
        }
    )

    assert findings[0].metric == "spiking"


def test_a_clean_report_says_how_many_were_checked() -> None:
    report = Report(
        findings=Detector().assess_all({"a": series([10, 10, 11, 10, 10, 11, 10, 10, 10])})
    )

    assert "Nothing unusual across 1" in report.render()


def test_unassessed_metrics_are_named_rather_than_quietly_dropped() -> None:
    """ "Nothing unusual" while silently skipping half the metrics is the
    sentence that destroys trust the day somebody notices."""
    report = Report(
        findings=Detector().assess_all(
            {
                "established": series([10, 10, 11, 10, 10, 11, 10, 10, 10]),
                "brand-new": series([5, 6]),
            }
        )
    )

    rendered = report.render()

    assert "brand-new" in rendered
    assert "could not be assessed" in rendered


def test_an_anomaly_report_states_the_numbers_behind_it() -> None:
    """No claim in the output that did not come from the arithmetic — the
    defence against a model inventing a percentage."""
    report = Report(
        findings=Detector().assess_all({"failures": series([10, 11, 9, 10, 12, 10, 11, 10, 95])})
    )

    rendered = report.render()

    assert "95" in rendered
    assert "baseline" in rendered


# -- against a real Grafana, when there is one -----------------------------

real_grafana = pytest.mark.skipif(
    not (REAL_URL and REAL_TOKEN),
    reason="set UIONE_TEST_GRAFANA_URL and UIONE_TEST_GRAFANA_TOKEN to run against a real instance",
)


@pytest.fixture
async def live_bi():
    client = GrafanaBI(grafana_config(REAL_URL, REAL_TOKEN))
    yield client
    await client.aclose()


@real_grafana
async def test_real_grafana_is_reachable(live_bi: GrafanaBI) -> None:
    assert (await live_bi.health()).get("database") == "ok"


@real_grafana
async def test_real_grafana_returns_the_alert_shape_the_mock_claims(live_bi: GrafanaBI) -> None:
    """The assertion the mock cannot make about itself: name in labels, state
    nested under status."""
    alerts = await live_bi.alerts(active_only=False)
    if not alerts:
        pytest.skip("the live instance has no alerts")

    alert = alerts[0]
    assert "alertname" in alert["labels"]
    assert "state" in alert["status"]
    assert alert_name(alert)


@real_grafana
async def test_a_viewer_token_is_enough(live_bi: GrafanaBI) -> None:
    """Nothing in this connector needs more than Viewer, and a higher role would
    make a class of mistake possible that currently is not."""
    assert await live_bi.rules() is not None
    assert await live_bi.dashboards() is not None
