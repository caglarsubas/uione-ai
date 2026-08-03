# Upgrading the database

Until now the schema was created with `create_all`, and the docstring said what
that meant: *"a migration tool becomes necessary the moment a customer has data
worth preserving across a schema change."* Twelve tables later, that moment has
passed.

## Why `create_all` is not a migration

It creates tables that are missing. It never alters tables that exist. So adding
a column to a model does nothing at all on a database that already has that
table — and the failure does not happen at startup, where somebody would notice.
It happens later, in one query, as `no such column`, after the process has been
serving traffic.

## For an operator

```bash
python -m uione.storage.cli status    # what revision this database is at
python -m uione.storage.cli upgrade   # run everything outstanding
python -m uione.storage.cli sql       # print the SQL, change nothing
python -m uione.storage.cli stamp     # record as current WITHOUT running anything
```

`status` exits non-zero when the database is behind, so a deployment script can
gate on it.

`sql` is the mode a change-controlled datacentre actually uses: a DBA reviews the
statements before anything touches production.

`stamp` is for exactly one situation — a deployment that predates migrations and
whose schema already matches. It refuses to overwrite an existing revision,
because stamping a database that is genuinely behind marks it current while
leaving it broken, and the failure then appears somewhere else entirely.

**There is no downgrade command.** Alembic offers one; this deliberately does not
expose it. The initial revision's downgrade drops every table — the audit log,
the approval history, the undo journal — and a flag that destroys those is a flag
somebody eventually passes at 3am while trying to fix something else. Going back
means restoring a backup, which is a decision made deliberately.

## What startup does

| State of the database | What happens |
|---|---|
| Empty | Created and stamped. A first run should not require a migration against nothing. |
| At head | Starts. |
| Behind | **Refuses**, naming the command to run. |
| Tables but no migration record | **Refuses**, naming `stamp` — this predates migrations, and `upgrade` would try to create tables that already exist. |
| At a revision this build has never heard of | **Refuses**, saying the database was made by a newer version. |

That last row is worth its own line. It means somebody rolled the *application*
back below the database. Alembic's own error for it is a `ResolutionError` naming
a hex string, which tells an operator nothing — and the fix is the opposite of
the usual one: roll forward again, or restore a backup taken before the upgrade.

`UIONE_DB_AUTO_UPGRADE=true` migrates on startup instead of refusing. Off by
default, and the default is the interesting choice: auto-upgrade is convenient
for a single-node appliance and wrong for anything else, because two replicas
starting together both migrate and a bad migration reaches production before
anyone has read it. Refusing to start is loud, immediate and recoverable.

## The test that keeps this honest

The schema is described twice — by the SQLAlchemy models and by the migration
history — and nothing but `test_migrations_produce_the_same_schema_as_the_models`
keeps the two the same.

A column added to a model without a migration passes every other test, because
tests build tables from the models. It is missing on every real deployment,
because those build tables from migrations. Verified by adding a stray column and
watching the test fail with `columns differ on sync_watermarks`.

## Notes for whoever writes the next migration

**Batch mode is on globally.** SQLite cannot `ALTER` most things in place, so
Alembic rewrites the table. Enabled in `env.py` rather than per-migration,
because a migration author should not have to remember which backend the
operator chose.

**`compare_type` is on.** Without it, widening a column is silently not a
migration, and the mismatch shows up as a truncation months later.

**The URL comes from `UIONE_DATABASE_URL`,** not from `alembic.ini`. The same
image runs against SQLite on a laptop and Postgres in a datacentre, and a
connection string checked into a file is a connection string in a git history.
`-x url=...` overrides it, so a migration can be pointed at a restored backup
without exporting anything.

**Migrations ship inside the package,** at `src/uione/storage/migrations`. An
operator upgrading an air-gapped install has the image they were given and
nothing else; migrations that live only in the source tree are migrations they
cannot run.


## Who runs them, per deployment

| Path | Migrates | Why |
|---|---|---|
| `make up` / compose | the app, on start | one container, one volume, one SQLite file — provably one writer |
| Helm, SQLite profile | the pod, on start | same reasoning; every configuration with more than one pod is refused |
| Helm, PostgreSQL | a `pre-install,pre-upgrade` Job | many pods would race, so exactly one migrates before any of them start |
| bare `make run` | you | `UIONE_DB_AUTO_UPGRADE` is off by default and stays off |

The default is off, and it should be: two replicas starting together would both
migrate, and a migration that goes badly takes production with it before anybody
has read it. What each deployment does is decide whether that risk exists *for
it*, and say so explicitly rather than inherit a default that was chosen for a
different shape.

Compose did not, for a while. The result was that any `git pull` carrying a
migration turned `make up` into a crash loop:

```
uione-app-1   Restarting (3) 32 seconds ago
```

with the actual reason — `the database schema is at 31c0a9d5e318 but this build
needs 293397191fe9` — visible only to somebody who thought to run `docker logs`.
The startup check is right to refuse a schema it cannot run. It should not be
the first thing a developer meets after updating.
