# Running this in production

## Backing up

```bash
python -m uione.storage.cli backup /backups/uione-$(date +%F).db
```

**You cannot back up a live SQLite database by copying the file.** A `cp` taken
mid-transaction captures pages from two different states and produces a file
that opens, queries, and is wrong — the worst kind of backup, because nobody
finds out until they need it.

`VACUUM INTO` is SQLite's own answer. It runs inside a read transaction and
writes a complete, defragmented database while the service keeps serving. The
result is a normal SQLite file any tool can open.

It refuses to overwrite an existing file. Backups are taken on a schedule and
named by date, and silently replacing one is how a week of them turns out to be
the same day.

**PostgreSQL is refused, by name, before anything else happens.** `pg_dump`
already does this properly with options this would have to reinvent badly, and
an operator running Postgres has a backup policy that does not need us. The
check happens before the engine is built, because constructing it loads the
driver — and `ModuleNotFoundError: asyncpg` is not the answer to "can I back
this up".

### What is not in the backup

The **file share** (`UIONE_FILES_ROOT`) — documents the assistant wrote live
there, and they are files, so back them up the way you back up files.

Nothing else. The document index and the embedding cache are inside the
database; the inverted index is rebuilt at startup from the stored documents.

## Restoring

```bash
docker compose stop app
cp /backups/uione-2026-07-28.db ./data/uione.db
python -m uione.storage.cli status
docker compose start app
```

There is deliberately **no `restore` command**. Restoring is copying a file into
place while the service is stopped — something an operator can see, verify and
undo. Wrapping it would mean this process deciding when it is safe to overwrite
the database it is connected to, which it cannot know.

`status` between the two is the point of the sequence. A backup from an older
build carries an older schema, and the startup check refuses to run against it
rather than failing in a query hours later. Run `upgrade` if it says to.

## Resetting the demo estate

```bash
docker compose down -v     # -v also drops the volumes, so all data goes
make up
make provision
```

## What is still missing

Named rather than implied, because an operator planning a deployment needs to
know before they commit:

* **No multi-tenancy.** One organisation per deployment. Permissions are
  per-user within it, but there is no tenant boundary above that.
* **No high availability.** The scheduler assumes it is the only one running;
  two replicas would both generate briefs. `UIONE_SCHEDULER_ENABLED=false` on
  the extra replicas is the workaround, not a design.
* **No per-user credentials.** Every connector authenticates with one service
  account per system, so a source system sees a single identity for all users
  and its own permissions cannot tell them apart (F3.2).

Three things have left this list and are documented below rather than deleted,
so an operator reading an older revision can find where they went: rate limiting
is now [admission control](#admission-control), and observability is
[Metrics](#metrics) and [Tracing](#tracing).


## Metrics

```bash
UIONE_METRICS_TOKEN=$(openssl rand -hex 32)
```

```bash
curl -sH "Authorization: Bearer $UIONE_METRICS_TOKEN" http://127.0.0.1:8000/metrics
```

**Unset means no endpoint, and the response is 404 rather than 401.** These
series describe an organisation's operational profile — how big its approval
backlog is, how often its writes fail to confirm, how much GPU it burns — and a
deployment that never enabled metrics should not advertise that the endpoint is
there to be attacked.

### What is published

| Metric | Type | Why an operator wants it |
|---|---|---|
| `uione_tool_calls_total{server,tool,risk,outcome}` | counter | Every call, by how it ended. `outcome="denied"` climbing means a policy is wrong; `held_for_approval` climbing means the autonomy ladder is not learning. |
| `uione_tool_call_duration_seconds{server}` | summary | Which connector got slow. |
| `uione_mutating_actions_total` | counter | Writes that executed. |
| `uione_verified_actions_total` | counter | Writes read back and **confirmed** — the north-star numerator. The ratio against the line above is the metric §9 of the strategy actually names. |
| `uione_unconfirmed_actions_total` | counter | Writes the system contradicted. **Alert on any increase**: a system is accepting changes and discarding them. |
| `uione_model_tokens_total{model,kind}` | counter | Where the GPU went. |
| `uione_connector_up{server}` | gauge | 0 when the gateway has given up on a connector. |
| `uione_model_plane_queued` | gauge | Requests waiting for a slot — the number worth paging on, per [admission control](#admission-control). |
| `uione_approvals_pending` | gauge | The approval backlog. A rising line is a user who has stopped reviewing. |

### Two deliberate omissions

**Nothing is labelled by user.** Not a cardinality argument, though it is that
too: a metrics endpoint labelled by user id is a surveillance surface, and the
privacy stance (G15) promises admins aggregate-only analytics. "Which of my
reports used the assistant least this week" must not be answerable from a
Prometheus query. Per-user attribution lives in the audit log, which is
access-controlled and exists for auditors rather than managers.

**Durations are summaries, not histograms.** A histogram needs buckets chosen up
front, and badly chosen buckets answer the wrong question confidently and cannot
be re-cut afterwards. Count and sum give the average and the rate, which is what
"is it getting slower" needs. Percentiles can come when someone has an SLO to
hold the buckets to.

The counters are fed from the audit stream rather than a second instrumentation
path, so they cannot drift from the log they describe — they are the same events,
added up.

## Tracing

Metrics answer "how often" and "how slow on average". They cannot answer the
question you actually get asked — **"why was *this* brief slow?"** — where the
answer is one span at the bottom of a tree.

```bash
pip install '.[otel]'
```

```bash
UIONE_OTEL_ENDPOINT=http://tempo:4318/v1/traces
```

Empty disables it, and there is no default: traces carry an organisation's tool
names, model names and timings, and the rule at the top of `config.py` is that
nothing may point at the public internet.

A request produces one tree:

```
POST /chat
├── model workhorse            uione.tokens.prompt=2411  uione.tokens.completion=88
├── tool tasks.my_open_issues  uione.outcome=allowed
├── tool tasks.update_issue    uione.outcome=allowed  uione.verification=confirmed
│   └── tool tasks.get_issue   ← the read-back, nested under the write it checks
└── model workhorse            uione.tokens.prompt=3120
```

The model span opens **before** the admission gate deliberately. Time spent
queueing for a GPU slot is the most common reason a brief is slow, and a span
that opened after the wait would show a fast model call and no explanation for
the missing seconds.

### It is optional, and it degrades rather than crashes

| Install | Behaviour |
|---|---|
| No OpenTelemetry (the default) | `span()` is a context manager that does nothing |
| `opentelemetry-api` only | logs `tracing.sdk_missing`, stays off |
| SDK but no OTLP exporter | logs `tracing.exporter_missing`, **starts anyway** |
| Full `[otel]` extra + endpoint | exports |

The third row is a test, not a hope. `opentelemetry-exporter-otlp` is a separate
distribution from the SDK, so a partial install is easy to produce — and a
service that refuses to start because its *telemetry* is misconfigured has
inverted the priority. The operator wanted traces, not an outage.

### No user identifiers on spans

Same reasoning as the metrics endpoint. Traces land in a system operators and
often a vendor's backend can read, and G15 promises employees their assistant is
not a surveillance channel. Spans carry what the system did — tool, server, risk,
outcome, verification verdict, model, token counts. The audit log carries who,
under access control, for auditors.

## Conversations

Chat history is stored **server-side**, per principal, in `conversation_messages`.

That is a security decision rather than an architectural preference. History is
model context: a client that supplies its own can fabricate an assistant turn —
*"the user already approved sending this"* — and prime the model with it, which
is the same class of attack the containment layer exists to stop. Nothing the
browser says about what was said earlier is trusted.

**Taint is a property of the conversation, not of one turn.** Replaying history
puts the same untrusted text back into the context window, so a session that read
a poisoned email on turn one is still carrying it on turn three. The only way out
is `POST /chat/new`, which clears the history — the audit log keeps everything
that was said and done.

**Tool results are not replayed.** Only the prose of each turn is kept. Reading
back "5 unread" from an hour ago as though it were current is worse than spending
one more call to ask, and confident staleness is the failure mode this product is
built to avoid everywhere else.

History is trimmed to a character budget, oldest first, and the trim **never
orphans a tool result** — an OpenAI-compatible engine rejects a `tool` message
whose assistant parent is missing, so a naive "keep the last N" produces a 400
the first time the cut lands mid-pair.

## Admission control

### The measurement that shaped it

One 8B model, one machine, the same question asked N times at once:

| concurrent | wall clock | slowest reply |
|---|---|---|
| 1 | 1.2s | 1.2s |
| 3 | 3.0s | 3.0s |
| 6 | 7.0s | 7.0s |

Six requests take roughly six times one request. **Concurrency buys no
throughput** — the engine serialises internally, so sending more at once spreads
the same total time over more people and everybody waits for everybody.

That changes what the problem is. This is not a limiter protecting the engine
from overload; the engine protects itself perfectly well by queueing. Since the
total time is fixed, the only decision left is **who spends it**.

### Interactive work overtakes background work

The queue inside the engine is FIFO and priority-blind, so five morning briefs
submitted at 07:29 delay a question asked at 07:30. Holding background work in
our own queue instead lets interactive requests reach the engine first.

Measured, a question arriving behind five briefs:

| | interactive reply | background slowest |
|---|---|---|
| ungated | **6.4s** | 5.2s |
| gated | **3.0s** | 6.3s |

Ungated, the person waiting is served *last* — they arrived last into a FIFO
queue. Gated, they wait less than half as long, and the time lands on work with
nobody watching it. Total throughput is unchanged, because it was never the
variable.

### What it does not do

**Priority is not preemption.** A request already at the engine cannot be
recalled, so an interactive caller still waits for a slot to free — it just gets
the next one. That is why the gated interactive reply above is 3.0s rather than
1.2s.

### The defaults, and why

| setting | default | reasoning |
|---|---|---|
| `UIONE_MODEL_PLANE_CONCURRENCY` | 2 | The measurement says a third adds latency and no throughput. Raise it for an engine that genuinely batches — vLLM with continuous batching does. |
| `UIONE_MODEL_PLANE_QUEUE_TIMEOUT_S` | 30 | Ninety seconds of spinner teaches people the product is slow. A quick refusal teaches them it is loaded, which is recoverable. |

Background work waits far longer than interactive (300s), because being late
costs a brief nothing and a refusal costs the whole brief.

**Interactive is the default priority**, deliberately. A caller who forgets to
declare one is treated as user-facing; the inverse default would let a forgotten
annotation starve somebody's chat behind a batch job, and that is much the worse
mistake to make quietly.

### Watching it

`/ready` reports `in_flight` and `queued`. **`queued` is the number worth an
alert** — it says the engine is the bottleneck, which is the one problem no
tuning elsewhere fixes. The answer is more or faster hardware, and an operator
should learn that from a graph rather than from users complaining.

A full queue does **not** make the service unready. It is working exactly as
designed and is merely saturated; returning 503 would make a load balancer pull
a healthy instance for being busy, sending its traffic to the others and
saturating them too.
