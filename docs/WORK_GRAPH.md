# The work graph — gap G4

Twenty connectors give an assistant **reach**. The work graph gives it
**coherence**: knowing that the supplier's email, the reconciliation ticket, and
the invoice number are one piece of work rather than three unrelated rows in
three lists.

This is the layer the strategy document calls the moat, because enterprise search
vendors have a read-only version of it and nobody has a write-action-aware one
on-premise.

## v1 is deterministic on purpose

Shared identifiers, email addresses, and URLs only. No embeddings, no fuzzy
matching, no model in the loop.

That is a precision decision, not a shortcut. **A wrong link is worse than a
missing one** — it puts another customer's ticket in your brief, and a user who
finds one of those stops trusting all of them. Probabilistic resolution (F8.4)
belongs on top of a base the user can verify by eye, not instead of it.

The same reasoning drives requiring *known* project prefixes. A generic
`[A-Z]{2,}-\d+` pattern happily matches `COVID-19`, `UTF-8` and `ISO-9001`, so
it exists but stays off unless a deployment opts in.

Two details that decide whether anything links at all:

- **Case folding.** A mailbox writes `pay-1182`, a tracker writes `PAY-1182`. If
  those do not collapse to one key, the graph holds two unrelated nodes and finds
  nothing. The extraction pattern is case-insensitive for exactly this reason —
  precision comes from the prefix allowlist, which is a far better filter than
  letter case. (This was a real bug, caught by a test before it shipped.)
- **Message-IDs are not people.** `<abc@mail.corp.example>` matches an email
  address pattern. Treating it as a colleague pollutes every person query.

## What it answers

| Query | Question it serves |
|---|---|
| `about(entity)` | "What's the story with INC-4471?" |
| `clusters()` | Groups of items that belong to one piece of work |
| `cross_system_clusters()` | The joins a user *cannot* make by glancing at one tool |
| `duplicates_of(item)` | The same event arriving through four channels (gap **G7**) |

`duplicates_of` is deliberately narrow: only items naming this item's *subject*.
Sharing an invoice number makes two items related; it does not make them the same
event, and over-merging is how a brief hides something the user needed.

Every link carries its evidence. `Link.explain()` returns
`"m-3 ↔ PAY-1182 (via INV-88213)"`, because "these are related, trust me" is the
kind of claim that destroys confidence the one time it is wrong.

## Effect on the brief

Before, the brief connected mail `m-3` to task `PAY-1182` only when the model
happened to notice both mentioned the same invoice. Now the connection is
computed first and handed to the model as fact.

Observed with `ministral-3:8b`:

```
DETERMINISTIC CONNECTIONS FOUND: ['INC-4471', 'INV-88213']

Mail
- [m-1] P1 alert … Incident INC-4471 referenced. No action required beyond tracking.
- [m-3] External … 4,200 EUR discrepancy. Due Friday. Connected to task [PAY-1182].

Tasks
- [PAY-1182] Reconcile INV-88213 (due 2026-07-31) — External procurement email
  demands confirmation by Friday.
```

Two things changed relative to the pre-graph run recorded in
[MORNING_BRIEF.md](MORNING_BRIEF.md):

1. The links are stated in both directions and attributed, rather than appearing
   as an aside in one section.
2. **The due date is right.** The earlier run said `PAY-1182` was due 28 July
   against a fixture value of 31 July; this run quotes `2026-07-31`.

Be careful with the second point: it is one run, and these models vary between
runs of an identical prompt (see [MODEL_TRIALS.md](MODEL_TRIALS.md)). It is
consistent with a more structured prompt reducing invention, but it is *not*
proof, and it is not a substitute for the fixture-exact eval gate in F11.3. The
honest claim is that the links are now guaranteed; the prose accuracy is not.

## Scope limits, stated plainly

**One graph item per brief section, not per record.** Connectors return rendered
text rather than structured rows, so section granularity is what can be indexed
honestly today. It already answers *which systems are talking about the same
thing*, which is the question a brief needs. Per-record resolution follows when
connectors return structured items.

**Rebuilt per request, held in memory.** Honest for the current scale — one
user's morning is hundreds of items, not millions. The interface is what a
persistent store later has to satisfy.

**No cross-user graph.** Everything is scoped to what the requesting principal
could already read through the gateway, so the graph cannot become a way around
tool policy. A shared org-wide graph needs permission-aware retrieval (**G3**)
underneath it first.

## Configuration

```bash
UIONE_TICKET_PREFIXES=PAY,OPS,PLAT      # project keys in this estate
UIONE_INTERNAL_DOMAINS=corp.example
```

`INC` (incidents) and `INV`, `CLM`, `PO` (business references) are recognised by
default, since those conventions are near-universal.
