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
* **No rate limiting.** A user who asks fifty questions at once will saturate
  the model plane. Scheduler concurrency is bounded; interactive traffic is not.
* **No high availability.** The scheduler assumes it is the only one running;
  two replicas would both generate briefs. `UIONE_SCHEDULER_ENABLED=false` on
  the extra replicas is the workaround, not a design.
* **No metrics endpoint.** Structured logs, an audit table, and the health
  endpoint — but nothing for Prometheus to scrape.
