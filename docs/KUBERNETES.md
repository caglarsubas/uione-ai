# Kubernetes — F12.1 and F12.2

The objective in the backlog is "installable by a bank's infra team without your
engineers on site". That is mostly a documentation problem and partly a refusal
problem: the chart's real job is to reject the configurations that schedule
cleanly, pass every probe, and are quietly broken.

```bash
helm install uione deploy/helm/uione \
  --set modelPlane.url=http://engine.svc:8080/v1
```

That is the **appliance** profile: one pod, SQLite on a PersistentVolume, the
scheduler in-process. Right for a PoC and for a single-node install (F12.2).

## The two profiles

| | appliance | distributed |
|---|---|---|
| Web pods | 1 | N |
| Scheduler | in-process | its own Deployment, exactly 1 |
| Database | SQLite on a PVC | PostgreSQL |
| Set | *(default)* | `replicaCount`, `scheduler.separate=true`, `database.existingSecret` |

```bash
helm install uione deploy/helm/uione \
  --set modelPlane.url=http://engine.svc:8080/v1 \
  --set replicaCount=3 \
  --set scheduler.separate=true \
  --set database.existingSecret=uione-db \
  --set persistence.enabled=false
```

## Why the scheduler is a second Deployment

`OPERATIONS.md` has recorded this as a gap: the scheduler assumes it is the only
one running, and two of them generate every brief twice — so every employee gets
their morning mail in duplicate.

A single Deployment with a replica count cannot express that. So the chart ships
two: web pods with `UIONE_SCHEDULER_ENABLED=false` and N replicas, and a
scheduler pinned to **one** replica with **no value that changes it**. Exposing
`scheduler.replicas` would be handing an operator a footgun shaped like a scaling
knob.

Its update strategy is `Recreate` rather than `RollingUpdate` for the same
reason: a rolling update briefly runs two, which is the exact thing the pinned
replica count prevents.

The Service selects `component: web` only. The scheduler serves the same HTTP
surface, but it is sized for background work and taking user requests would put
people behind brief generation.

## The probes do not use `/ready`, and that is deliberate

`/ready` returns **503 when the model plane is unreachable**. That is the right
answer to the question it was written for — "should a load balancer send *chat*
here" — and the wrong answer for a Kubernetes readiness probe.

The model plane is a **shared** dependency, not a property of one pod. When it
goes down, every replica fails the probe at the same instant, Kubernetes empties
the Service, and users get a connection refused.

What they should get is the workspace: the brief, the approval queue, the
transparency page — none of which need a model — with an honest banner saying the
model plane has been unreachable since 06:12. That is gap G8, and a readiness
probe pointed at `/ready` converts a visible partial outage into an invisible
total one.

So all three probes use `/health`. Alert on `/ready`; do not probe with it.

## Migrations run once, as a hook

`UIONE_DB_AUTO_UPGRADE` is `false` in every pod, set by the chart rather than
left to the image default — an image built with a different default would
otherwise turn each replica into a competing migrator.

Instead a `pre-install,pre-upgrade` Job runs `python -m uione.storage.cli
upgrade` before any pod starts. A failed migration then fails the release,
rather than half-migrating under live traffic.

The Job's name carries the release revision, because Jobs are immutable and a
fixed name makes the *second* upgrade fail with `field is immutable` instead of
migrating.

## What the chart refuses

Each of these renders happily in a naive chart and then misbehaves in a way
Kubernetes cannot see. All are `helm template` errors, so they land in front of
the person doing the install.

| Configuration | Why it is refused |
|---|---|
| No `modelPlane.url` | No default may reach an endpoint nobody chose |
| SQLite + `replicaCount > 1` | One writer, one filesystem. Over RWX you get a database that passes every health check and is corrupt |
| SQLite + `scheduler.separate` | A second pod cannot share a SQLite file |
| RWO volume + several pods | Schedules, then does not mount |
| `serviceMonitor` without a token | `/metrics` is 404 without one; the ServiceMonitor would scrape nothing and report itself healthy |
| `UIONE_FILES_ROOT` off `/data` | The entrypoint creates that directory at startup and cannot on a read-only root — CrashLoopBackOff with a permission error four levels down |
| `UIONE_FILES_ROOT` without persistence | Every document the assistant writes vanishes with the pod |

`tests/test_helm_chart.py` asserts every one of them, and CI installs helm so
they are real there rather than skipped.

## Hardening

Defaults satisfy a **restricted** PodSecurity namespace with no edits, because
"works only with the policy relaxed" is a conversation with a platform team that
delays every install by a week.

- `runAsNonRoot`, uid 10001 — matching the `useradd --uid 10001` in the Dockerfile
- `readOnlyRootFilesystem`, with `/tmp` and `/data` as the only writable mounts
- all capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp
- `automountServiceAccountToken: false` — the application calls no Kubernetes
  API, and a mounted token is a cluster credential for every prompt-injection
  attempt to aim at

## Air gap

The chart has **no `dependencies:`**. That block sends `helm dep update` to a
chart repository over the internet, which an air-gapped install cannot do — and
it would fail during the customer's deployment rather than during ours.
PostgreSQL is expected to already exist.

Everything else is `image.repository` plus `image.pullSecrets`, so a mirrored
registry needs one value.

Signed offline bundles for images, models and connectors are **F12.3 and are not
built yet**. This chart is deployable from a private registry; it is not yet an
offline installer.

## What is still missing

- **No HorizontalPodAutoscaler.** Web pods scale on request concurrency, which
  is not CPU, and shipping a CPU-based HPA would scale on the wrong signal.
- **No PodDisruptionBudget.** Correct for the scheduler, which must never have
  two; wrong for the web pods, and that asymmetry needs deciding rather than
  defaulting.
- **No NetworkPolicy.** Should be written against a real customer's topology
  rather than guessed.
- **Not deployed against a real cluster in CI.** `helm template` proves what the
  manifests say; a kind cluster would prove the pods start. That is the next
  thing worth adding here.
