# UiOne AI — System Architecture

> Companion to [PRODUCT_STRATEGY_AND_BACKLOG.md](PRODUCT_STRATEGY_AND_BACKLOG.md). That document says *what* and *why*; this one says *how it is built*.

## 1. Component map

```
┌───────────────────────────────────────────────────────────────┐
│ uione (this repo)                                             │
│                                                               │
│  api/          FastAPI surface: chat, brief, action queue     │
│  agent/        plan → act → verify runtime + reliability layer │
│  mcphub/       MCP client host + governed gateway              │
│  governance/   risk classes, approvals, audit, containment     │
│  knowledge/    retrieval, work graph, memory                   │
│  proactive/    scheduler, brief generators, watchers           │
│  models/       model-plane client + task-tier router           │
└───────────────────────────┬───────────────────────────────────┘
                            │ OpenAI-compatible HTTP
                            ▼
┌───────────────────────────────────────────────────────────────┐
│ llm_inference_engine (separate repo, reused as-is)            │
│  /v1/chat/completions · /v1/embeddings · /v1/rerank · /v1/models│
│  adapters: vLLM · llama.cpp · MLX · Ollama                      │
└───────────────────────────────────────────────────────────────┘
```

## 2. Why the model plane is a separate repo

Epic **E1 (Inference & Model Platform)** in the backlog is already implemented by
[`caglarsubas/llm_inference_engine`](https://github.com/caglarsubas/llm_inference_engine):
an OpenAI-compatible, backend-agnostic serving layer with vLLM/llama.cpp/MLX adapters,
model routing, auth, OTel tracing, an eval runner, and hardened container/Helm packaging
(UBI images, SBOMs, cosign signatures, air-gap transfer docs).

UiOne therefore **consumes it over HTTP** and does not vendor or fork it. Consequences:

- **`UIONE_MODEL_PLANE_URL` is the only coupling.** Anything speaking the OpenAI API works
  — the engine in production, a local Ollama in development, a stub in tests.
- **Model choice stays configuration, never code** (architectural commitment in §3 of the
  strategy doc). This is what lets us ride the open-weight release curve.
- **No model runtime code lives here.** Bugs in serving, quantization, or batching are
  fixed upstream and inherited.

## 3. Layering rules

Dependencies point downward only. Enforced by review, and by keeping imports one-directional:

```
api  →  agent  →  mcphub  →  (connectors, over MCP)
         ↓          ↓
      models    governance  ←  every mutating path, no exceptions
         ↓
    knowledge
```

Two rules carry most of the safety weight:

1. **No agent code calls a connector directly.** Everything goes through `mcphub`, which is
   the single chokepoint for authentication, tool allow-listing, rate limiting, and the
   audit tap. A direct connector import from `agent/` is a review blocker.
2. **No mutating action bypasses `governance`.** Risk classification and the approval ladder
   wrap every write path, including scheduled jobs and A2A-initiated work — there are no
   side doors for "internal" callers.

## 4. Trust boundaries

| Boundary | Rule |
|---|---|
| Tool output → model context | Untrusted. Never interpreted as instructions (gap G2). |
| External message content (email, chat) | Quarantined as data; carries a taint flag through the context. |
| Model output → tool call | Schema-validated and repaired before execution; never executed raw. |
| Any write → source system | Risk-classified, approval-gated, journaled for undo, and read back where the connector registered a check — see [VERIFICATION.md](VERIFICATION.md) for which writes those are. |

## 5. Persistence

SQLite by default (single-file PoC, zero-dependency tests), PostgreSQL in production via
the same SQLAlchemy async layer — `UIONE_DATABASE_URL` selects. The audit log is
append-only at the application layer and expected to ship to the customer's SIEM; it is
never the only copy of the record.

## 6. Development

```bash
uv sync --extra dev          # or: pip install -e ".[dev]"
make test                    # pytest
make lint                    # ruff
make run                     # uvicorn on :8000
```

Tests never require a GPU, a model, or a network: the model plane is stubbed at the HTTP
layer, so CI runs on any machine.
