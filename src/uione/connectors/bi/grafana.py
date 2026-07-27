"""Grafana — dashboards, and the alerts that fire off them.

The brief asks for BI-triggered anomaly alerting. Grafana is where that lives in
most estates, it is open source and self-hostable, and it is therefore tier A:
verified against a real instance rather than a fixture. The payload shapes below
were taken from a Grafana 11.6 container with a rule actually firing.

**Read-only, and enforced by the credential.** The service account this connector
needs is a *Viewer*. Grafana's own permission model then makes a mistake here
structurally impossible — no code path exists that could silence an alert,
because the token cannot. Silencing an alert is exactly the sort of "helpful"
action that must never be automatable: it makes the symptom disappear while the
problem continues.

**Alerts arrive in Alertmanager's schema, not Grafana's.** Grafana embeds an
Alertmanager, so a firing alert is `{labels, annotations, status, startsAt}` —
the alert's name is `labels.alertname`, its summary is `annotations.summary`, and
its state is `status.state`, not a top-level field. Every one of those is a level
deeper than a first guess would put it.

**A resolved alert is not a firing one.** The endpoint returns both unless asked
otherwise, and the difference is invisible unless you read `status.state`. An
assistant that reports resolved alerts as current is describing a past crisis in
the present tense, at 07:30, to someone who then acts on it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 20

#: Label keys Grafana adds for its own bookkeeping. Shown to a person they are
#: noise, and in a model's context they are tokens spent on nothing.
INTERNAL_LABELS = frozenset(
    {"__alert_rule_uid__", "__name__", "__alert_rule_namespace_uid__", "grafana_folder"}
)

SEVERITY_ORDER = {"critical": 0, "high": 1, "error": 1, "warning": 2, "info": 3, "none": 4}


def grafana_config(
    base_url: str, token: str, *, verify_tls: bool = True, timeout_s: float = 20.0
) -> VendorConfig:
    """A Grafana service-account token, which is a Bearer credential.

    Create it as a **Viewer**. Nothing in this connector needs more, and a
    higher role would make a class of mistake possible that is currently not.
    """
    return VendorConfig(
        name="grafana",
        base_url=base_url.rstrip("/"),
        auth=Auth(scheme="bearer", secret=token),
        verify_tls=verify_tls,
        timeout_s=timeout_s,
    )


class GrafanaBI:
    def __init__(self, config: VendorConfig, **kwargs: Any) -> None:
        self._client = VendorClient(config, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict:
        return await self._client.get("/api/health")

    async def alerts(self, *, active_only: bool = True) -> list[dict]:
        """Alerts from Grafana's embedded Alertmanager.

        `active_only` filters on `status.state`, because the endpoint happily
        returns alerts that have already resolved and nothing at the top level
        distinguishes them.
        """
        payload = await self._client.get("/api/alertmanager/grafana/api/v2/alerts")
        alerts = list(payload or [])
        if active_only:
            alerts = [a for a in alerts if (a.get("status") or {}).get("state") == "active"]
        return alerts

    async def dashboards(self, *, limit: int = DEFAULT_LIMIT, query: str = "") -> list[dict]:
        params: dict[str, Any] = {"type": "dash-db", "limit": limit}
        if query:
            params["query"] = query
        return list(await self._client.get("/api/search", params=params) or [])

    async def rules(self) -> list[dict]:
        """Alert rules and their current evaluation state.

        Separate from `alerts` on purpose: a rule in `Error` or `NoData` is not
        firing, so it never appears in the alert list — and it is precisely the
        state where nobody is being told anything and everyone assumes they
        would be.
        """
        payload = await self._client.get("/api/prometheus/grafana/api/v1/rules")
        groups = (payload.get("data") or {}).get("groups") or []
        return [
            {**rule, "group": group.get("name", ""), "file": group.get("file", "")}
            for group in groups
            for rule in group.get("rules") or []
        ]


# -- rendering -------------------------------------------------------------


def alert_name(alert: dict) -> str:
    return (alert.get("labels") or {}).get("alertname", "(unnamed alert)")


def alert_severity(alert: dict) -> str:
    return (alert.get("labels") or {}).get("severity", "none")


def visible_labels(alert: dict) -> dict[str, str]:
    return {k: v for k, v in (alert.get("labels") or {}).items() if k not in INTERNAL_LABELS}


def render_alert(alert: dict) -> str:
    annotations = alert.get("annotations") or {}
    labels = visible_labels(alert)
    severity = alert_severity(alert)

    parts = [f"[{severity}] {alert_name(alert)}"]
    if summary := annotations.get("summary"):
        parts.append(f"  {summary}")
    if since := alert.get("startsAt"):
        parts.append(f"  firing since: {since}")
    if scope := ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname"):
        parts.append(f"  {scope}")
    if runbook := annotations.get("runbook_url"):
        # Carried through deliberately. The single most useful thing to hand
        # somebody at 07:30 is the document telling them what to do.
        parts.append(f"  runbook: {runbook}")
    return "\n".join(parts)


def sort_alerts(alerts: list[dict]) -> list[dict]:
    """Most severe first, then longest-firing.

    Whatever is first is what gets read. Sorting by time alone puts a disk-space
    warning above a payments outage because it started earlier.
    """
    return sorted(
        alerts,
        key=lambda a: (
            SEVERITY_ORDER.get(alert_severity(a).lower(), 5),
            a.get("startsAt", ""),
        ),
    )


def render_rule_health(rules: list[dict]) -> list[str]:
    """Rules that are not evaluating — the silent failure mode."""
    broken = []
    for rule in rules:
        health = str(rule.get("health", "")).lower()
        if health in {"error", "nodata"}:
            reason = rule.get("lastError") or health
            broken.append(f"{rule.get('name', '?')}: {health} ({reason})")
    return broken


# -- the governed tools ----------------------------------------------------


def build_grafana_source(bi: GrafanaBI, *, name: str = "bi") -> InMemoryToolSource:
    """Every tool here is READ, and there is no write tool at all.

    Not an omission. Grafana's write surface is silences, dashboard edits and
    rule changes — all of which either hide a problem or alter what everyone
    else is monitoring. None belongs in an assistant's reach.
    """
    source = InMemoryToolSource(name)

    async def firing_alerts(args: dict) -> ToolResult:
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            alerts = sort_alerts(await bi.alerts(active_only=True))[:limit]
            rules = await bi.rules()
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        broken = render_rule_health(rules)

        if not alerts:
            text = "No alerts are firing."
            if broken:
                # "Nothing is firing" is a different claim from "nothing is
                # wrong" when a rule has stopped evaluating. Saying so is the
                # whole difference between monitoring and the appearance of it.
                text += "\n\nBut these rules are not evaluating:\n" + "\n".join(
                    f"  {b}" for b in broken
                )
            return ToolResult.success(
                text, {"count": 0, "unhealthy_rules": len(broken), "checked_at": _now()}
            )

        text = "\n".join(render_alert(a) for a in alerts)
        if broken:
            text += "\n\nRules not evaluating:\n" + "\n".join(f"  {b}" for b in broken)

        return ToolResult.success(
            text,
            {
                "count": len(alerts),
                "names": [alert_name(a) for a in alerts],
                "severities": [alert_severity(a) for a in alerts],
                "unhealthy_rules": len(broken),
                "checked_at": _now(),
            },
        )

    async def list_dashboards(args: dict) -> ToolResult:
        query = str(args.get("query", "")).strip()
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            found = await bi.dashboards(limit=limit, query=query)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not found:
            return ToolResult.success(
                f"No dashboards matching {query!r}." if query else "No dashboards available.",
                {"count": 0},
            )

        return ToolResult.success(
            "\n".join(
                f"{d.get('title', '?')} — {d.get('folderTitle', 'General')} ({d.get('url', '')})"
                for d in found
            ),
            {"count": len(found), "titles": [d.get("title") for d in found]},
        )

    source.register(
        "firing_alerts",
        firing_alerts,
        description=(
            "Alerts currently firing in Grafana, most severe first, "
            "plus any alert rule that has stopped evaluating."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 20."}},
        },
        risk=RiskClass.READ,
        # Alert names, summaries and runbook URLs are authored by whoever wrote
        # the rule, and a rule can be written by anyone with edit rights.
        returns_untrusted_content=True,
    )
    source.register(
        "list_dashboards",
        list_dashboards,
        description="Find dashboards by title.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "1-50, default 20."},
            },
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    return source


def _now() -> str:
    return datetime.now(UTC).isoformat()
