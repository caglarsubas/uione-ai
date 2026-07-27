"""A Grafana-shaped alerting API.

Like the Gitea mock and unlike the ServiceNow and claims ones, this was
**checked against a real instance** — Grafana 11.6 with a rule genuinely firing.
The payloads are the shapes that instance returned, including the parts that a
reasonable person would have guessed wrong:

* an alert's name is `labels.alertname`, not a `name` field;
* its state is `status.state`, not a top-level `state`;
* Grafana injects its own labels (`__alert_rule_uid__`, `grafana_folder`,
  `__name__`) alongside the ones a human wrote;
* the endpoint returns resolved alerts too, and nothing outside `status.state`
  says so.

Rule *health* comes from a different endpoint in a different schema
(`/api/prometheus/grafana/api/v1/rules`), which is why the connector calls both:
a rule in `error` never appears in the alert list, and that is exactly the state
where nobody is being told anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Query, Request

EPOCH = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _stamp(minutes: int = 0) -> str:
    return (EPOCH + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class State:
    def __init__(self) -> None:
        self.alerts: list[dict] = []
        self.rules: list[dict] = []
        self.dashboards: list[dict] = []

    def add_alert(
        self,
        name: str,
        *,
        summary: str = "",
        severity: str = "warning",
        state: str = "active",
        team: str = "",
        runbook: str = "",
        minutes: int = 0,
        folder: str = "Payments",
    ) -> dict:
        labels = {
            "alertname": name,
            "severity": severity,
            # Grafana's own bookkeeping, mixed in with the human's labels.
            "__alert_rule_uid__": f"uid-{len(self.alerts) + 1}",
            "__name__": "A-series",
            "grafana_folder": folder,
        }
        if team:
            labels["team"] = team

        annotations = {"__orgId__": "1"}
        if summary:
            annotations["summary"] = summary
        if runbook:
            annotations["runbook_url"] = runbook

        alert = {
            "labels": labels,
            "annotations": annotations,
            "startsAt": _stamp(minutes),
            "endsAt": _stamp(minutes + 240),
            "updatedAt": _stamp(minutes),
            "fingerprint": f"{len(self.alerts) + 1:016x}",
            "status": {"state": state, "silencedBy": [], "inhibitedBy": []},
            "receivers": [{"name": "grafana-default-email"}],
            "generatorURL": (
                f"http://grafana.local/alerting/grafana/uid-{len(self.alerts) + 1}/view"
            ),
        }
        self.alerts.append(alert)
        return alert

    def add_rule(self, name: str, *, health: str = "ok", state: str = "inactive", error: str = ""):
        rule = {
            "name": name,
            "health": health,
            "state": state,
            "lastError": error,
            "type": "alerting",
        }
        self.rules.append(rule)
        return rule

    def add_dashboard(self, title: str, *, folder: str = "Payments") -> dict:
        dashboard = {
            "uid": f"dash-{len(self.dashboards) + 1}",
            "title": title,
            "url": f"/d/dash-{len(self.dashboards) + 1}/{title.lower().replace(' ', '-')}",
            "folderTitle": folder,
            "type": "dash-db",
        }
        self.dashboards.append(dashboard)
        return dashboard


def build_grafana_mock(state: State | None = None) -> FastAPI:
    app = FastAPI(title="mock-grafana")
    app.state.data = state or State()

    def data(request: Request) -> State:
        return request.app.state.data

    @app.get("/api/health")
    async def health() -> dict:
        return {"database": "ok", "version": "11.6.0-mock"}

    @app.get("/api/alertmanager/grafana/api/v2/alerts")
    async def alerts(request: Request) -> list[dict]:
        # Returns resolved alerts alongside active ones, exactly as a real
        # instance does. Filtering is the caller's job, and forgetting it means
        # describing a past crisis in the present tense.
        return data(request).alerts

    @app.get("/api/prometheus/grafana/api/v1/rules")
    async def rules(request: Request) -> dict:
        return {
            "status": "success",
            "data": {
                "groups": [{"name": "payments", "file": "Payments", "rules": data(request).rules}]
            },
        }

    @app.get("/api/search")
    async def search(
        request: Request, query: str = Query(""), limit: int = Query(20), type: str = Query("")
    ) -> list[dict]:
        found = data(request).dashboards
        if query:
            found = [d for d in found if query.lower() in d["title"].lower()]
        return found[:limit]

    return app


def seed_grafana(state: State | None = None) -> State:
    """One real fire, one lesser problem, one already-resolved, one broken rule."""
    store = state or State()

    store.add_alert(
        "Settlement failure rate above threshold",
        summary="Settlement failures exceeded 5% for the last 10 minutes",
        severity="critical",
        team="payments",
        runbook="http://wiki.local/runbooks/settlement",
        minutes=-25,
    )
    store.add_alert(
        "Refund latency p99 degraded",
        summary="p99 above 2s for 15 minutes",
        severity="warning",
        team="payments",
        minutes=-90,
    )
    store.add_alert(
        "Disk space low on reporting-02",
        summary="Below 15% free",
        severity="warning",
        state="resolved",
        minutes=-600,
    )
    store.add_rule("Settlement failure rate above threshold", health="ok", state="firing")
    store.add_rule("Refund latency p99 degraded", health="ok", state="firing")
    store.add_rule(
        "Chargeback ratio by acquirer",
        health="error",
        error="datasource 'acquirer-metrics' not found",
    )
    store.add_dashboard("Payments overview")
    store.add_dashboard("Settlement health")
    store.add_dashboard("Chargebacks", folder="Risk")
    return store
