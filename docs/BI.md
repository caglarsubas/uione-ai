# Business intelligence and anomalies

The brief asks the assistant to surface "BI-triggered anomalies". That splits
into two problems with very different shapes, so they are two components.

## 1. Alerts somebody already decided were worth alerting on

Grafana is where this lives in most estates, it is open source and self-hostable,
and it is therefore **tier A** — verified against a real instance rather than a
fixture. The payload shapes in `vendormocks/grafana.py` came from a Grafana 11.6
container with a rule genuinely firing.

Three things the connector gets right that a first attempt would not:

**A resolved alert is not a firing one.** The endpoint returns both, and nothing
outside `status.state` distinguishes them. An assistant reporting a resolved
alert at 07:30 is describing a past crisis in the present tense to somebody who
then acts on it.

**A rule that stopped evaluating is worse than one that is firing.** A rule in
`error` — a renamed datasource, a broken query — never appears in the alert list
at all. That is the state where nobody is being told anything and everyone
assumes they would be, so rule health is fetched from a *second* endpoint and
reported alongside. "No alerts are firing" is never said on its own when a rule
is broken.

**Most severe first.** Whatever is first is what gets read. Sorting by start time
puts a disk-space warning above a payments outage because it began earlier.

### Read-only, enforced by the credential

The service account is a **Viewer**. Grafana's own permission model then makes a
whole class of mistake structurally impossible: there is no code path that could
silence an alert, because the token cannot. Silencing is exactly the sort of
helpful-looking action that must never be automatable — it makes the symptom
disappear while the problem continues. There is no write tool in this connector
at all.

## 2. Numbers nobody wrote a rule for

`uione/analysis/anomaly.py`. The hard part is not detection, it is **restraint**:
an assistant that reports every fluctuation gets muted within a week, and a muted
assistant is worse than none, because now nobody is watching and everyone
believes somebody is.

So it is tuned for a low false-positive rate at the cost of sensitivity:

| Choice | Instead of | Because |
|---|---|---|
| Median and MAD | Mean and standard deviation | One catastrophic day inflates the spread so much that the *next* spike falls inside it. MAD has a 50% breakdown point. |
| Same-weekday baseline | A flat baseline | Payment volume on a Sunday is not payment volume on a Tuesday. Every Monday would be an anomaly and every Saturday a crisis. |
| A minimum absolute change | Percentage thresholds alone | A counter going 3 → 6 is a 100% rise and statistically enormous. It is also three. |
| "Not assessed" | "Normal" | Too little history is not the same claim as nothing being wrong, and conflating them is how a new metric goes unwatched. |

A flat metric is handled explicitly: a MAD of zero makes every deviation
infinitely significant, which is the classic way these detectors produce
nonsense.

**What it deliberately is not**: a forecasting model. No ARIMA, no Prophet, no
learned seasonality beyond day-of-week. Those need tuning, retraining and an
owner, and an on-premise product that ships an unowned model ships something
that will be quietly wrong in a year.

**Reports name what they skipped.** "Nothing unusual" while silently omitting
half the metrics is the sentence that destroys trust the day somebody notices.

## Configuration

```bash
UIONE_GRAFANA_URL=https://grafana.internal
UIONE_GRAFANA_TOKEN=glsa_...        # a Viewer service account
```
