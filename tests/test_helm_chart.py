"""The Helm chart — F12.1.

A chart that renders is not a chart that deploys. What these assert is the part
Kubernetes cannot check for itself: the combinations that schedule cleanly, pass
every probe, and are quietly broken.

Skipped when `helm` is absent, so a laptop without it still runs the suite. CI
installs it, so the assertions are real there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "uione"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

#: The two values a release cannot start without, so every invocation below is
#: otherwise minimal. `UIONE_AUTH_MODE` is here because the chart refuses to
#: render a production environment with dev auth — see the test at the bottom.
BASE = [
    "--set", "modelPlane.url=http://engine.svc:8080/v1",
    "--set", "config.UIONE_AUTH_MODE=proxy",
]  # fmt: skip

DISTRIBUTED = [
    *BASE,
    "--set", "replicaCount=3",
    "--set", "scheduler.separate=true",
    "--set", "database.existingSecret=uione-db",
    "--set", "persistence.enabled=false",
]  # fmt: skip


def render(*args: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "release", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def refuse(*args: str) -> str:
    """Render, expecting failure. Returns the message an operator would see."""
    result = subprocess.run(
        ["helm", "template", "release", str(CHART), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "chart rendered a configuration it should have refused"
    return result.stderr


def of_kind(docs: list[dict], kind: str, component: str | None = None) -> list[dict]:
    found = [d for d in docs if d.get("kind") == kind]
    if component:
        found = [
            d
            for d in found
            if (d["metadata"].get("labels") or {}).get("app.kubernetes.io/component") == component
        ]
    return found


def env_of(deployment: dict) -> dict[str, str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"] if "value" in e}


# -- it lints --------------------------------------------------------------


def test_the_chart_lints() -> None:
    result = subprocess.run(["helm", "lint", str(CHART), *BASE], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# -- the guards, which are the point ---------------------------------------


def test_a_model_plane_url_is_required() -> None:
    """No default, per the rule at the top of config.py: an on-premise product
    must never point at an endpoint nobody chose."""
    assert "modelPlane.url is required" in refuse()


def test_sqlite_with_several_replicas_is_refused() -> None:
    """Two pods sharing one SQLite file over RWX gives a database that passes
    every health check and is quietly corrupt. Kubernetes cannot see it."""
    assert "SQLite is a single writer" in refuse(*BASE, "--set", "replicaCount=3")


def test_sqlite_with_a_separate_scheduler_is_refused() -> None:
    assert "second pod cannot share a SQLite file" in refuse(
        *BASE, "--set", "scheduler.separate=true"
    )


def test_a_read_write_once_volume_shared_by_many_pods_is_refused() -> None:
    assert "cannot be mounted by more than one pod" in refuse(
        *BASE,
        "--set", "replicaCount=3",
        "--set", "database.existingSecret=uione-db",
    )  # fmt: skip


def test_a_service_monitor_without_a_token_is_refused() -> None:
    """/metrics is 404 without a token, so the ServiceMonitor would scrape
    nothing while reporting itself perfectly healthy."""
    assert "needs metrics.existingSecret" in refuse(
        *BASE, "--set", "metrics.serviceMonitor.enabled=true"
    )


# -- the appliance profile -------------------------------------------------


def test_the_appliance_profile_is_one_pod_that_schedules_its_own_briefs() -> None:
    docs = render(*BASE)

    web = of_kind(docs, "Deployment", "web")
    assert len(web) == 1
    assert web[0]["spec"]["replicas"] == 1
    assert env_of(web[0])["UIONE_SCHEDULER_ENABLED"] == "true"
    assert not of_kind(docs, "Deployment", "scheduler")


def test_the_database_volume_survives_an_uninstall() -> None:
    """It holds the audit log, the approvals and the undo journal. Deleting an
    audit trail as a side effect of `helm uninstall` is not a thing to do quietly.
    """
    claim = of_kind(render(*BASE), "PersistentVolumeClaim")[0]
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


# -- the distributed profile -----------------------------------------------


def test_exactly_one_scheduler_runs_and_the_web_pods_do_not() -> None:
    """Two schedulers generate every brief twice. This is the whole reason the
    two Deployments exist rather than one with a replica count."""
    docs = render(*DISTRIBUTED)

    scheduler = of_kind(docs, "Deployment", "scheduler")[0]
    web = of_kind(docs, "Deployment", "web")[0]

    assert scheduler["spec"]["replicas"] == 1
    assert env_of(scheduler)["UIONE_SCHEDULER_ENABLED"] == "true"

    assert web["spec"]["replicas"] == 3
    assert env_of(web)["UIONE_SCHEDULER_ENABLED"] == "false"


def test_the_scheduler_never_rolls_two_at_once() -> None:
    """A RollingUpdate briefly runs two schedulers, which is exactly what
    pinning the replica count to 1 was for."""
    scheduler = of_kind(render(*DISTRIBUTED), "Deployment", "scheduler")[0]
    assert scheduler["spec"]["strategy"]["type"] == "Recreate"


def test_user_traffic_never_reaches_the_scheduler() -> None:
    """It serves the same HTTP surface but is sized for background work; taking
    requests would put people behind brief generation."""
    service = of_kind(render(*DISTRIBUTED), "Service")[0]
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "web"


def test_the_database_url_comes_from_a_secret_not_a_value() -> None:
    """A connection string carries a password, and values.yaml ends up in Git,
    in `helm get values`, and in the release ConfigMap."""
    web = of_kind(render(*DISTRIBUTED), "Deployment", "web")[0]
    container = web["spec"]["template"]["spec"]["containers"][0]
    url = next(e for e in container["env"] if e["name"] == "UIONE_DATABASE_URL")

    assert "value" not in url
    assert url["valueFrom"]["secretKeyRef"]["name"] == "uione-db"


# -- migrations ------------------------------------------------------------


def test_postgres_migrates_with_a_hook_and_no_pod_races() -> None:
    """Many pods, so UIONE_DB_AUTO_UPGRADE on would have every replica racing to
    migrate the same database. A pre-upgrade Job runs once, and a failed
    migration fails the release instead of half-migrating under live traffic."""
    docs = render(*DISTRIBUTED)

    job = of_kind(docs, "Job")[0]
    assert job["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "python",
        "-m",
        "uione.storage.cli",
        "upgrade",
    ]

    for deployment in of_kind(docs, "Deployment"):
        assert env_of(deployment)["UIONE_DB_AUTO_UPGRADE"] == "false"


def test_the_migration_job_mounts_no_volume() -> None:
    """It is a pre-install hook, so it runs *before* Helm creates the chart's
    ordinary resources — a claim it mounted would not exist yet. It only exists
    on PostgreSQL, where the database is elsewhere entirely, so it needs none."""
    container = of_kind(render(*DISTRIBUTED), "Job")[0]["spec"]["template"]["spec"]["containers"][0]
    assert [m["mountPath"] for m in container["volumeMounts"]] == ["/tmp"]


def test_sqlite_migrates_in_process_with_no_job() -> None:
    """SQLite implies exactly one pod — every configuration with more is refused
    above — so the race the Job exists to prevent cannot happen, and the pod that
    owns the file migrates it."""
    docs = render(*BASE)

    assert not of_kind(docs, "Job")
    assert env_of(of_kind(docs, "Deployment", "web")[0])["UIONE_DB_AUTO_UPGRADE"] == "true"


def test_the_migration_job_name_changes_per_revision() -> None:
    """Jobs are immutable. A fixed name makes the second upgrade fail with
    "field is immutable" instead of migrating."""
    first = of_kind(render(*DISTRIBUTED), "Job")[0]["metadata"]["name"]
    assert first.endswith("-1")  # Release.Revision


# -- probes, and the trap ---------------------------------------------------


def test_probes_use_health_and_never_ready() -> None:
    """/ready reports 503 when the *model plane* is unreachable. As a readiness
    probe that empties the Service the moment a shared dependency fails, turning
    a partial outage everyone can see into a total one nobody can diagnose — the
    opposite of gap G8. The workspace, approvals and brief all render without a
    model.
    """
    for deployment in of_kind(render(*DISTRIBUTED), "Deployment"):
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
            assert container[probe]["httpGet"]["path"] == "/health", probe


# -- hardening --------------------------------------------------------------


def test_no_pod_mounts_a_kubernetes_token() -> None:
    """The application calls no Kubernetes API. Mounting a token hands every
    prompt-injection attempt a cluster credential to aim at."""
    account = of_kind(render(*BASE), "ServiceAccount")[0]
    assert account["automountServiceAccountToken"] is False


def test_containers_run_unprivileged_with_a_read_only_root() -> None:
    """Defaults that satisfy a restricted PodSecurity namespace without edits —
    "works only with the policy relaxed" is a conversation that delays installs.
    """
    for deployment in of_kind(render(*DISTRIBUTED), "Deployment"):
        pod = deployment["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True
        container = pod["containers"][0]["securityContext"]
        assert container["allowPrivilegeEscalation"] is False
        assert container["readOnlyRootFilesystem"] is True
        assert container["capabilities"]["drop"] == ["ALL"]


def test_nothing_is_exposed_outside_the_cluster_by_default() -> None:
    """The premise of the product is not being on the internet."""
    docs = render(*BASE)
    assert not of_kind(docs, "Ingress")
    assert of_kind(docs, "Service")[0]["spec"]["type"] == "ClusterIP"


def test_the_chart_pulls_nothing_from_a_chart_repository() -> None:
    """`helm dep update` reaches the internet, which is the one thing an
    air-gapped install cannot do — and it would fail during the customer's
    deployment rather than ours."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert "dependencies" not in chart


def test_a_file_share_on_the_read_only_root_is_refused() -> None:
    """docker-entrypoint.sh creates UIONE_FILES_ROOT at startup, deliberately —
    the directory lives on a volume so it cannot exist at build time. Under a
    read-only root that mkdir fails and the container never starts.

    CrashLoopBackOff with a permission error four levels down an entrypoint is a
    bad afternoon. This is a sentence.
    """
    assert "read-only root filesystem" in refuse(
        *BASE, "--set", "config.UIONE_FILES_ROOT=/srv/share"
    )


def test_a_file_share_without_a_volume_is_refused() -> None:
    """Every document the assistant wrote would vanish with the pod."""
    assert "ephemeral filesystem" in refuse(
        *BASE,
        "--set", "config.UIONE_FILES_ROOT=/data/share",
        "--set", "persistence.enabled=false",
    )  # fmt: skip


def test_a_file_share_on_the_volume_is_accepted() -> None:
    docs = render(*BASE, "--set", "config.UIONE_FILES_ROOT=/data/share")
    assert of_kind(docs, "Deployment", "web")


def test_a_production_environment_with_dev_auth_is_refused() -> None:
    """`UIONE_AUTH_MODE` defaults to dev, which accepts unauthenticated headers,
    and the identity layer refuses that outside a dev environment. The chart
    defaults the environment to production, so a release setting neither is a
    CrashLoopBackOff with the reason four screens into `kubectl logs`.

    Found by installing on a real cluster rather than rendering — which is the
    whole argument for the `cluster` CI job.
    """
    message = refuse("--set", "modelPlane.url=http://engine.svc:8080/v1")

    assert "accepts unauthenticated headers" in message
    assert "CrashLoopBackOff" in message


def test_a_dev_environment_may_use_dev_auth() -> None:
    """The refusal is about the combination, not about dev auth existing."""
    docs = render(
        "--set", "modelPlane.url=http://engine.svc:8080/v1",
        "--set", "config.UIONE_ENVIRONMENT=dev",
    )  # fmt: skip

    assert of_kind(docs, "Deployment", "web")
