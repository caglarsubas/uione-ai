# UiOne AI

**The sovereign enterprise super-assistant.** Fully on-premise, open-weight models only, MCP-native.

UiOne AI unifies the enterprise tools employees juggle all day — email, chat, tasks, incidents, claims, BI, reports, documents, meetings — into one governed conversational workspace. Employees start the day with **"good morning"** and get a triaged, actionable brief; their assistants act across systems with graduated autonomy and full audit, and collaborate with each other over A2A.

Three claims that define the product:

1. **It acts** — write-actions across enterprise systems, not just search.
2. **It's proactive** — morning briefs, anomaly alerts, recurring reports, watchdogs.
3. **It's governed** — approval workflows, immutable audit, on-prem/air-gap deployment, open-weight models.
4. **Assistants collaborate** — one employee's assistant can ask another's, bounded by a disclosure contract the *subject* controls.

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
make test
```

Run the service against any OpenAI-compatible model endpoint (the
[inference engine](https://github.com/caglarsubas/llm_inference_engine) in
production, Ollama in development):

```bash
UIONE_MODEL_PLANE_URL=http://127.0.0.1:11434/v1 make run
```

Then open **http://127.0.0.1:8000/** for the workspace.

See the signature moment with a real open-weight model:

```bash
python scripts/demo_brief.py --model ministral-3:8b
```

## What works today

| Capability | Where |
|---|---|
| Model plane client, task-tier routing, escalation | `src/uione/modelplane/` |
| Governed MCP gateway: deny-by-default policy, audit tap, rate limits, circuit breaking | `src/uione/mcphub/` |
| Real IMAP/SMTP mail connector | `src/uione/connectors/mail/` |
| Real CalDAV calendar connector | `src/uione/connectors/calendar/` |
| File share connector with POSIX ACL derivation | `src/uione/connectors/files/` |
| Agent loop with tool-call repair and name resolution | `src/uione/agent/` |
| Graduated autonomy, approvals, undo journal, injection containment | `src/uione/governance/` |
| Read-after-write: a write is read back and the verdict audited | `src/uione/governance/verification.py` |
| Work graph: deterministic cross-system entity resolution | `src/uione/knowledge/` |
| Permission-aware retrieval: ACL-filtered BM25 index | `src/uione/knowledge/` |
| Ingestion with ACL derivation and permission re-sync | `src/uione/knowledge/` |
| Eval harness: fixture-exact golden tasks gating model changes | `src/uione/evals/` |
| Real MCP client over stdio, with third-party servers governed by our policy | `src/uione/mcphub/` |
| Durable audit, approvals, undo journal, autonomy, schedules, contracts and documents | `src/uione/storage/` |
| Scheduler: briefs pre-generated ahead of the working day | `src/uione/proactive/` |
| Web workspace: brief, chat, Approval Center, transparency page | `src/uione/web/` |
| Identity: OIDC bearer validation, fail-closed auth | `src/uione/identity/` |
| A2A: assistant collaboration with disclosure contracts | `src/uione/a2a/` |
| Morning Brief with provenance, links and honest degradation | `src/uione/proactive/` |
| HTTP API: chat, brief, approval queue, transparency page | `src/uione/api/` |
| Prometheus metrics and OTel tracing, aggregate-only | `src/uione/observability/` |

Mail is a real IMAP/SMTP connector (configure `UIONE_MAIL_IMAP_HOST`, otherwise a
fixture is used); calendar is real CalDAV (`UIONE_CALENDAR_URL`), read and write — it proposes meetings; tasks are real
Gitea or Forgejo issues (`UIONE_GITEA_URL` + `UIONE_GITEA_TOKEN`); a file share is
indexed when `UIONE_FILES_ROOT` is set, with permissions read from the
filesystem. Incidents speak the ServiceNow Table API (`UIONE_SERVICENOW_URL`) and
claims a Guidewire-shaped Cloud API (`UIONE_CLAIMS_URL`) — both verified against
mocks rather than the vendors, which no one can reach without a contract. Chat is
real Mattermost (`UIONE_MATTERMOST_URL`) and BI is real Grafana
(`UIONE_GRAFANA_URL`).

### Run the whole thing without a single vendor account

```bash
make estate
```

Starts a real Gitea and a real Grafana in Docker, provisions them, seeds a
plausible working morning, and writes `.env.estate`. Then `make mocks` in another
shell for the systems nobody can reach without a contract, and:

```bash
set -a; . ./.env.estate; set +a
make run
```

Half the estate is genuinely real and half is mocked, and
[ESTATE.md](docs/ESTATE.md) says which is which rather than blurring it.

Everything a user configures survives a restart — their brief schedule, what
their assistant may disclose about them, and the indexed corpus with its
permissions. Set `UIONE_INGEST_ON_STARTUP=1` to sweep configured sources at
startup; the first run over a large share should be a decision rather than a
surprise on boot.

Once running, two loops keep the corpus current: content every 15 minutes, and
permissions every 2 — the second far more often because being late on a
revocation is a leak, not a stale page. A source whose permissions cannot be
verified for an hour is quarantined and its content dropped. Freshness per source
is at `/system/health`.

## Documents

- [Product strategy, backlog & gap analysis](docs/PRODUCT_STRATEGY_AND_BACKLOG.md) — thesis, reference architecture, 12-epic backlog, 18 strategic gaps, competitive matrix, roadmap.
- [Architecture](docs/ARCHITECTURE.md) — component map, layering rules, trust boundaries.
- [Security model](docs/SECURITY_MODEL.md) — prompt injection, the lethal trifecta, the seven layers, and the known limits.
- [Read-after-write](docs/VERIFICATION.md) — why a vendor's `200` is not evidence, and which writes are checked.
- [Model trials](docs/MODEL_TRIALS.md) — measured tool-calling reliability across eight open-weight models.
- [Morning Brief](docs/MORNING_BRIEF.md) — implementation notes, observed output, and two honest defects.
- [Work graph](docs/WORK_GRAPH.md) — deterministic cross-system entity resolution, and why v1 avoids fuzzy matching.
- [Retrieval](docs/RETRIEVAL.md) — permission-aware search, and why filtering must precede ranking.
- [Evals](docs/EVALS.md) — including the language suite and what it measured. — the golden-task gate, current results, and the two failures it caught.
- [Docker](docs/DOCKER.md) — `make up`, and what is deliberately not in the image.
- [Operations](docs/OPERATIONS.md) — backup, restore, metrics, and what is still missing for production.
- [Migrations](docs/MIGRATIONS.md) — upgrading a database that has data worth keeping.
- [Storage](docs/STORAGE.md) — what durability protects, and how it was verified across processes.
- [Scheduler](docs/SCHEDULER.md) — how the brief becomes proactive, and the 10s → 2ms measurement.
- [Workspace](docs/WORKSPACE.md) — the UI, and why it ships with no build step.
- [Identity](docs/IDENTITY.md) — OIDC, the auth modes, and why every one fails closed.
- [Login](docs/LOGIN.md) — the authorization-code flow, PKCE, and revocable sessions.
- [A2A](docs/A2A.md) — assistant-to-assistant collaboration, and the disclosure contracts that make it safe.
- [The demo estate](docs/ESTATE.md) — one command to a running product, half real and half mocked.
- [Documents](docs/DOCUMENTS.md) — writing to the share, and reporting who can actually read the result.
- [Weekly review](docs/WEEKLY.md) — the daily census, and two things a real model taught the report.
- [BI and anomalies](docs/BI.md) — Grafana alerts, and why the detector is tuned against firing.
- [Vendor access](docs/VENDOR_ACCESS.md) — which systems can be integrated against for free, in what order, and what a mock may claim.
- [MCP](docs/MCP.md) — the real client, and why a server may raise its own risk but never lower it.
- [Connectors](docs/CONNECTORS.md) — the mail connector, and what every connector must declare.

## Status

Early development. The vertical slice — model plane → governed gateway → agent
loop → governance → brief → API — is working and tested end to end against real
open-weight models. Authentication is real OIDC with a working login flow, and
governance state is durable.

Every connector listed above has a real backend, and falls back to a fixture when
its system is not configured — so a fresh checkout runs with no vendor at all.
What separates them is not fixture-versus-real but **what each has been verified
against**: real servers for mail, calendar, files, Gitea, Grafana and Mattermost;
our own mocks for ServiceNow and Guidewire, which no one can reach without an
account or a contract. [CONNECTORS.md](docs/CONNECTORS.md) has the table, and the
distinction matters more than the label.

Not production-ready. The named gaps are in
[OPERATIONS.md](docs/OPERATIONS.md#what-is-still-missing) — no multi-tenancy, no
HA, no metrics endpoint — and the connector-side one is that a source system
still sees one service account for every user, not the calling user (F3.2).
