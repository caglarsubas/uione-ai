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
*(Rate limiting was on this list. It is now [admission control](#admission-control), below.)*
* **No high availability.** The scheduler assumes it is the only one running;
  two replicas would both generate briefs. `UIONE_SCHEDULER_ENABLED=false` on
  the extra replicas is the workaround, not a design.
* **No metrics endpoint.** Structured logs, an audit table, and the health
  endpoint — but nothing for Prometheus to scrape.


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
