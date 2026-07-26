# The Morning Brief — implementation notes and observed output

The signature moment: the user says "good morning" and gets a triaged, actionable
picture of their work, each item traceable to the system it came from.

## How it works

1. **Gather concurrently.** Four sources (incidents, mail, calendar, tasks) are
   fetched in parallel through the governed gateway. Concurrency because the
   brief's whole promise is speed; independence because one dead connector must
   not take the others with it.
2. **Quarantine on the way in.** Anything from a connector that declares
   `returns_untrusted_content` is wrapped and marked `trust=untrusted` before it
   reaches the prompt. In the demo estate that is mail — which is also what taints
   the session and gates any write that follows.
3. **Compose.** One reasoning-tier call assembles the brief from retrieved data
   only, with explicit instructions never to invent identifiers and to name any
   system that was unreachable.
4. **Return structure, not just prose.** The response carries `complete`,
   `unavailable`, per-section `provenance`, and timings alongside the text.

## Observed output — `ministral-3:8b`, local, 7.7 s

```
The payment gateway latency breach is live and requires immediate attention.

Incidents
- [INC-4471] P1: Payment gateway p99 latency breach since 23:40 (correlates
  with 23:35 config push). Owner: You.
- [INC-4468] P3: Dashboard slowdown (watching). Owner: Bora.

Mail
- [m-1] Automated P1 alert for INC-4471 (duplicate of incident).
- [m-2] Q3 budget review moved to 14:00 (bring forecast + headcount plan).
- [m-3] External: Invoice INV-88213 shows 4,200 EUR discrepancy — confirm by
  Friday (task: [PAY-1182]).
- [m-5] External newsletter (ignore).

Suggested first moves:
1. For INC-4471: prepare for the 09:30 review by drafting a root-cause
   hypothesis and flag the 23:35 change in the incident.
2. For [m-3]: confirm the INV-88213 discrepancy with procurement (task: PAY-1182).
3. For [m-2]: block 13:30–14:00 to prep the Q3 forecast (task: PAY-1204).
```

Three behaviours worth noting, because they are the ones that make a brief feel
like a colleague rather than a feed:

- **Cross-system linking emerged unprompted.** It connected mail `m-3` (the
  supplier's invoice query) to task `PAY-1182` (reconcile that invoice) — the
  work-graph behaviour of gap **G4**, here produced by the model because both
  facts were in one context. Doing it *reliably* is still an explicit epic; a
  single 8B call getting it right on one estate is not a substitute for E8.
- **Deduplication.** It recognised `m-1` as a duplicate of `INC-4471` rather than
  listing the same problem twice — exactly the noise reduction gap **G7** asks for.
- **Noise dismissal.** It marked the vendor newsletter "ignore".

## Two honest defects, and what they changed

**1. A wrong date.** The brief stated `PAY-1182` was due 28 July; the fixture says
31 July. Everything else was accurate, which is precisely what makes this
dangerous — a brief that is 95 % right trains the user to stop checking. This is
the argument for provenance being non-negotiable, and for the eval harness
(**F11.3**) gating every model change on fixture-exact assertions rather than on
whether the prose reads well.

**2. Degradation was announced inconsistently.** With `incidents` and `tasks`
forced down, the model handled incidents well:

> *"No real-time updates, but INC-4471 is flagged in [m-1]."*

but **silently dropped the tasks section** rather than saying Jira was
unreachable — despite an explicit instruction to name unavailable systems.

That is the finding that matters most here. A prompt instruction is not a
guarantee, so the API returns `complete: false` and `unavailable: ["incidents",
"tasks"]` as **structured fields the UI must render**, independent of whether the
model remembered to mention it. Gap **G8** is satisfied by the contract, not by
the prose.

## Reproducing

```bash
python scripts/demo_brief.py --model ministral-3:8b
python scripts/demo_brief.py --down incidents,tasks
```
