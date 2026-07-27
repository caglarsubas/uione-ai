# Persistence

Governance state is durable. SQLite by default so a fresh checkout needs no
infrastructure; PostgreSQL in production through the same code path, selected by
`UIONE_DATABASE_URL`.

## Why this is not optional

Eight kinds of state were in memory, and each failed differently on a restart:

| State | Failure mode without durability |
|---|---|
| **Audit log** | A governed product whose local trail vanishes on restart is not credible in a room with an auditor. |
| **Pending approvals** | The user is not told the queue was discarded. Actions simply stop existing — which from their side is indistinguishable from the assistant having done them. |
| **Undo journal** | The window in which "I can put that back" is true silently closes. |
| **Autonomy records** | Every tool silently demotes to manual approval. The ladder then looks arbitrary, which is how an approval flow becomes noise people click through. |
| **Brief schedules** | Nobody is told. Their brief simply stops arriving and they conclude the feature does not work. |
| **Disclosure contracts** | Everyone reverts to the default, which is *narrower* — so a colleague's assistant starts refusing questions it answered yesterday, with nothing in any log connecting the two. |
| **Indexed documents** | Search returns nothing until a re-ingest, which for a file share is a long walk. |
| **Sync watermarks** | The dangerous one. An incremental sync after a restart either refetches everything, or resumes from a point that never covered the window the service was down for — so changes in that window are never seen at all. |

The second group shares a property the first does not: **none of these failures
announce themselves.** No error, no alert, no failed request — just a feature
that quietly stopped working. That is why they are worth a table each rather
than a line in a changelog.

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

**The document index is not stored — the documents are.** Postings are *derived*
data: they depend on the tokeniser and the stopword list, so a persisted index
would silently disagree with its own documents the first time either changed.
Rebuilding takes about a second for a corpus this size, and the alternative is a
schema migration every time retrieval is tuned.

**The ingestor is the single write path.** Anything it adds to the index is
stored; anything it removes is deleted, including a whole source on quarantine —
a restart must not restore content whose permissions could not be verified.
Persisting from a second place is how a store and an index start disagreeing
about who may read what.

**`last_run` is written through on every job, including failures.** A restored
job that has forgotten when it last ran is either due immediately — the whole
fleet generating at once on boot — or not due until tomorrow. Storing it on
failure too is what stops a failing job retrying on every tick after a restart.

**Unrecognised stored values narrow rather than widen.** An unknown disclosure
facet is dropped (a downgrade should disclose less, not refuse to start); an
unknown document visibility falls back to `restricted`, never organisation-wide;
an unparseable schedule time falls back to the default rather than failing the
whole load and taking everyone else's brief with it.

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

## Verified again, with a real file share

The same discipline for the second group. One process indexed a real directory
(real files, real `chmod`), set a schedule and widened a disclosure contract. A
**separately started process** — with ingestion switched off, so nothing could
refill the index — read all of it back:

```
PROCESS 1  (UIONE_INGEST_ON_STARTUP=1)
 schedule:    06:15 Europe/Istanbul
 disclosure:  bob -> [task_detail]
 indexed:     1     search "settlement escalation" -> file:runbooks/payments.md
 watermark:   2026-07-27 12:19:09.721550+00:00

PROCESS 2  (fresh, UIONE_INGEST_ON_STARTUP=0)
 schedule:    06:15 Europe/Istanbul
 disclosure:  bob -> [task_detail]
 indexed:     1     search "settlement escalation" -> file:runbooks/payments.md
 watermark:   2026-07-27 12:19:09.721550+00:00   <- process 1's, not a new one
```

Then the file was deleted at the source and a permission re-sync run:

```
ingest.permissions_changed  source=files changed=0 removed=1
PROCESS 3 (fresh)  indexed: 0   search -> []
```

The deletion is the half that is easy to get wrong. A store that only ever
inserts would restore the document on the next boot, and the symptom — a file
someone deleted still answering searches — is one nobody would think to test for.
