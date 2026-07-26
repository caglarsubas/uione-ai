# The proactive engine

The product makes three claims. *It acts* and *it's governed* were true before
this; **it's proactive** was not. The brief only existed when someone asked for
it, and asking cost eight to ten seconds.

## What changed

Briefs are generated ahead of the working day and stored, so the signature moment
becomes "it's there" rather than "request and wait".

Measured over HTTP against `ministral-3:8b`:

| Request | Time |
|---|---|
| Cold — generates on demand | **10.09 s** |
| Pre-generated | **0.0017 s** |

The second number is the product. Nobody experiences a ten-second "good morning"
twice.

## Design decisions

**Jitter, derived from the user id.** Five hundred employees scheduled for 08:00
would arrive at the model plane simultaneously, and the last of them would get
their morning news around lunchtime. Start times spread across a window — but
*deterministically per user*, because a brief that lands at 07:31 one day and
07:52 the next is one people stop relying on. Alice's 06:45 schedule resolves to
06:55 every day.

**Timezones, not UTC.** A "morning" brief computed in UTC arrives overnight for
half an organisation. The whole premise is that it is ready when *that person*
starts work.

**A new job is not immediately due.** Otherwise deploying the service at 15:00
fires everyone's morning brief on the spot — a GPU spike and a confusing first
impression at once.

**A failed job does not retry on the next tick.** `last_run` is stamped before
generation, so a dead connector cannot turn the scheduler into a tight loop
against the model plane.

**Concurrency is bounded, small.** Proactive work is background work: it must
yield to people waiting on an interactive request. Two at a time by default.

**One user's failure is contained.** A connector outage during Alice's brief must
not stop Bob's from being generated.

**Users control their own schedule.** `PUT /me/schedule` sets the time and
timezone. A user who cannot change when their assistant wakes up will simply stop
opening the brief, which is worse than the wrong default.

## Staleness

Only the latest brief is kept, and it is withheld once older than
`UIONE_BRIEF_MAX_AGE_MINUTES` (12 hours by default). Yesterday's morning brief
describes a world that has moved on; serving it would be worse than serving
nothing. `GET /brief?refresh=true` forces a rebuild.

## A bug worth recording

`BriefStore` defines `__len__`, which makes an **empty store falsy**. The idiom
`store or BriefStore()` therefore silently discards a caller's empty store and
substitutes a fresh one — so writes went to one object and reads came from
another. A test caught it by asserting on the store it had passed in.

Both the call site and the `__len__` docstring now say so, because the next
person to write `x or Default()` against a container-like object will hit exactly
this.

## Configuration

```bash
UIONE_SCHEDULER_ENABLED=true
UIONE_SCHEDULER_INTERVAL_S=60
UIONE_BRIEF_TIME=07:30
UIONE_BRIEF_TIMEZONE=Europe/Istanbul
UIONE_BRIEF_JITTER_S=900          # spread across 15 minutes
UIONE_BRIEF_MAX_AGE_MINUTES=720
UIONE_SCHEDULER_CONCURRENCY=2
```

## Not yet done

Schedules live in memory, so they are lost on restart — unlike approvals and
autonomy, which are durable. The storage layer exists and this is a table away;
it is called out here rather than left to be discovered.
