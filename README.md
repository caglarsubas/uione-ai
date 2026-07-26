# UiOne AI

**The sovereign enterprise super-assistant.** Fully on-premise, open-weight models only, MCP-native.

UiOne AI unifies the enterprise tools employees juggle all day — email, chat, tasks, incidents, claims, BI, reports, documents, meetings — into one governed conversational workspace. Employees start the day with **"good morning"** and get a triaged, actionable brief; their assistants act across systems with graduated autonomy and full audit, and collaborate with each other over A2A.

Three claims that define the product:

1. **It acts** — write-actions across enterprise systems, not just search.
2. **It's proactive** — morning briefs, anomaly alerts, recurring reports, watchdogs.
3. **It's governed** — approval workflows, permission-aware retrieval, immutable audit, on-prem/air-gap deployment, open-weight models.

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
| Agent loop with tool-call repair and name resolution | `src/uione/agent/` |
| Graduated autonomy, approvals, undo journal, injection containment | `src/uione/governance/` |
| Work graph: deterministic cross-system entity resolution | `src/uione/knowledge/` |
| Eval harness: fixture-exact golden tasks gating model changes | `src/uione/evals/` |
| Durable audit, approvals, undo journal and autonomy records | `src/uione/storage/` |
| Morning Brief with provenance, links and honest degradation | `src/uione/proactive/` |
| HTTP API: chat, brief, approval queue, transparency page | `src/uione/api/` |

Mail is a real IMAP/SMTP connector (configure `UIONE_MAIL_IMAP_HOST`, otherwise a
fixture is used). Calendar, tasks and incidents remain fixtures.

## Documents

- [Product strategy, backlog & gap analysis](docs/PRODUCT_STRATEGY_AND_BACKLOG.md) — thesis, reference architecture, 12-epic backlog, 18 strategic gaps, competitive matrix, roadmap.
- [Architecture](docs/ARCHITECTURE.md) — component map, layering rules, trust boundaries.
- [Security model](docs/SECURITY_MODEL.md) — prompt injection, the lethal trifecta, the seven layers, and the known limits.
- [Model trials](docs/MODEL_TRIALS.md) — measured tool-calling reliability across eight open-weight models.
- [Morning Brief](docs/MORNING_BRIEF.md) — implementation notes, observed output, and two honest defects.
- [Work graph](docs/WORK_GRAPH.md) — deterministic cross-system entity resolution, and why v1 avoids fuzzy matching.
- [Evals](docs/EVALS.md) — the golden-task gate, current results, and the two failures it caught.
- [Storage](docs/STORAGE.md) — what durability protects, and how it was verified across processes.
- [Connectors](docs/CONNECTORS.md) — the mail connector, and what every connector must declare.

## Status

Early development. The vertical slice — model plane → governed gateway → agent
loop → governance → brief → API — is working and tested end to end against real
open-weight models. Not production-ready: authentication is a header-based placeholder, and calendar,
tasks and incidents are still fixture connectors.
