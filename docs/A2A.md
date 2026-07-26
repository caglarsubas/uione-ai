# A2A — assistants collaborating, under contract

The competitive research found this unserved: platform-to-platform A2A exists,
but **employee-assistant ↔ employee-assistant collaboration with governance does
not** — cloud or on-premise. It is the clearest differentiator in the product,
and also the easiest thing to get catastrophically wrong.

## The failure mode this is built around

"My assistant talks to your assistant" is a data-leak generator unless something
decides *what mine may tell yours*. The natural implementation — an assistant
that helpfully answers every question it can — leaks the first time a colleague
asks "what is she working on?", and it answers in detail.

So the interesting part is not the message bus. It is the **disclosure
contract**.

## Contracts

A contract answers one question: for this requester, what facets of my owner's
working life may be revealed?

| Facet | Reveals |
|---|---|
| `free_busy` | *When* the owner is free. Times only. |
| `meeting_subjects` | What the meetings are about. Rarely granted. |
| `workload` | Rough capacity. No specifics. |
| `task_status` | Status of work items. |
| `task_detail` | Titles and content. |
| `out_of_office` | Absence dates. |
| `contact` | Working hours, timezone, channel. |

Defaults: a colleague gets **times but not subjects, capacity but not content**.
A close team additionally sees workload and task status. Anyone external gets
**nothing** until someone decides otherwise. The default is what actually ships,
so it is the line most people would draw for a colleague they do not work with.

Facets are deliberately coarse. Fine-grained per-field rules read as thorough and
are unusable — nobody configures forty toggles, so everyone keeps the default.

**Contracts are owned by the subject.** Bob decides what Bob's assistant says
about Bob. It is the only arrangement an employee would accept, and the one a
works council will ask about. A test asserts Alice cannot edit Bob's.

## Three gates

1. **Capability** — does the receiving assistant answer this at all? Refusing
   here reveals nothing about the owner.
2. **Disclosure** — of what was asked, what may this requester see? What cannot
   is withheld *and reported*, because an answer that quietly omits half the
   picture teaches the asker to treat partial information as complete.
3. **Commitment** — proposing a meeting or delegating work binds the owner, so it
   is held for their approval **regardless of contract**. A contract governs
   disclosure; agreeing to attend a meeting is not disclosure.

A commitment that could never be answered is refused rather than queued — do not
make someone reject a request their own policy already forbids.

## The answerer cannot leak by forgetting

The function that builds a response is *handed* the facets it may reveal:

```python
async def __call__(self, target, request, granted: frozenset[Facet]) -> dict:
```

It is never given the un-permitted data to forget about. Everything it reads goes
through the governed gateway **as the owner**, so a colleague's assistant can
never learn something the owner could not see themselves. A2A widens who may ask,
never what may be reached.

Note what is absent: `meeting_subjects` and `task_detail` have no branch in the
production answerer at all. The coarse facets are answerable from data the owner
already exposes; the content facets need a deliberate design pass. Not
implementing them is safer than implementing them approximately.

## Delegation chains

Every request carries the agents it passed through. "Alice's assistant asked" and
"Alice's assistant asked on behalf of someone two hops away" are different
events, and only one is benign — the audit records the whole chain.

Cycles are refused. The check covers the *current sender* as well as the earlier
chain; a first version only checked the chain, which let an agent forward to
itself indefinitely since it was not yet recorded in the list it was about to be
appended to. A test caught it.

## Verified live

```
Alice's assistant → Bob's assistant, "when are you free?"
  answered   free at 10:00, 12:00, 13:00, 15:00, 16:00, 17:00
             (no meeting subjects — never requested, never revealed)

Alice's assistant → Bob's assistant, "what are you working on?"
  refused    withheld by the owner disclosure policy: workload

Alice's assistant → Bob's assistant, "meet at 14:00 Thursday about INC-4471?"
  held       "agent:bob has passed this to their user for approval"
  bob's queue:   a2a.accept_meeting — "agent:alice proposes INC-4471
                 incident review at 14:00 Thursday"
  alice's queue: []          ← the commitment belongs to the person bound by it

Bob widens his own contract to include workload, then:
  answered   bob's assistant: workload moderate
```

## Not yet

External A2A over the Linux Foundation wire protocol (our types sit behind an
adapter for exactly this), contracts persisted to storage — they are in-memory
like schedules — and a UI for editing them; today it is the `/me/disclosure`
endpoint.
