# Persistence

Governance state is durable. SQLite by default so a fresh checkout needs no
infrastructure; PostgreSQL in production through the same code path, selected by
`UIONE_DATABASE_URL`.

## Why this is not optional

Four kinds of state were in memory, and each failed differently on a restart:

| State | Failure mode without durability |
|---|---|
| **Audit log** | A governed product whose local trail vanishes on restart is not credible in a room with an auditor. |
| **Pending approvals** | The user is not told the queue was discarded. Actions simply stop existing — which from their side is indistinguishable from the assistant having done them. |
| **Undo journal** | The window in which "I can put that back" is true silently closes. |
| **Autonomy records** | Every tool silently demotes to manual approval. The ladder then looks arbitrary, which is how an approval flow becomes noise people click through. |

## Design notes

**The audit sink exposes no update or delete path.** Not by convention — there
are no such methods. An audit trail with an edit method is one an auditor has to
take on trust. A test asserts the absence.

**Autonomy is cached in memory, written through on every decision.** `decide()`
runs on *every* mutating call, so a database round-trip inside the governance
check would put storage latency on the critical path of every action. Records
load once at startup; writes go through immediately, so a crash costs at most the
decision in flight.

**Arguments are still hashed by default.** Durability does not change the
redaction policy — a persistent audit log that is also a persistent PII spill is
worse than an ephemeral one.

**Credentials are stripped before a database URL is logged.**

## The interfaces came first

`ApprovalStore` and `ActionJournal` were defined as interfaces with in-memory
implementations several PRs before any database existed. Adding SQL versions was
therefore a wiring change, and `Governor` did not move.

One thing did change: the in-memory stores became `async` to match. They need no
I/O, but a sync/async split would force every caller to know which implementation
it holds — exactly the coupling an interface exists to prevent.

## Verified across process boundaries

Not just "read it back in the same test". One process wrote a pending action and
an autonomy record; a **separately started server process** read them:

```
PROCESS 1 wrote action: df64155546ce | autonomy progress: 3 approvals

PROCESS 2 (fresh server) GET /approvals
[{ "id": "df64155546ce",
   "tool": "tasks.update_issue",
   "preview": "tasks.update_issue — Update an issue\n  key: PAY-1190\n  status: Done" }]

PROCESS 2 GET /me/autonomy
{ "tasks.update_issue": { "auto": false, "approvals": 3, "toward_auto": 3 } }
```

The unit tests use the same shape: write, discard the store, build a new one over
the same file, assert the state is there. That is the only thing "durable" means,
and it is the only way to catch a store that quietly keeps everything in a dict.

## Schema migrations

`create_all` is adequate while the schema is additive and pre-release. A
migration tool becomes necessary the moment a customer has data worth preserving
across a schema change. Stated here rather than discovered later.
