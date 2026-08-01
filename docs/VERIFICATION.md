# Read-after-write — F2.6

An assistant that reports what it **attempted** is a different product from one
that reports what **happened**.

A connector returns the vendor's response to a write. That response is the vendor
agreeing it received the request — not evidence the state changed. Between those
two sits every "I've closed the ticket" that left the ticket open: a `200` on a
field the API silently ignored, a workflow rule that reverted the transition, a
write against the wrong record, a permission that allows the call and refuses the
effect.

So after a mutating call succeeds, the gateway calls a **read** tool back through
itself and compares what it finds against what was asked for.

```
tasks.update_issue  →  connector: "PAY#2 is now closed"
                    →  tasks.get_issue PAY#2
                    →  server: state=open
                    →  CONTRADICTED
```

## The four verdicts

| Verdict | Means | Counts as verified |
|---|---|---|
| `confirmed` | Read back, and the world matches | **yes** |
| `contradicted` | Read back, and it does not | no |
| `unverifiable` | No read-back registered for this tool | no |
| `unavailable` | The read-back itself could not be completed | no |

Only `confirmed` counts. The north-star metric is *Verified* Assisted Actions,
and a metric that treats "nobody checked" as a pass grows by adding tools nobody
checked.

## Four decisions that the obvious implementation gets wrong

**A contradicted write is not a failed call.** The write executed. Returning it
as a failure invites the model to retry, and retrying a write that already landed
is how one comment becomes two and one refund becomes two. The result stands, the
verdict rides alongside it, and the note appended for the model says *do not
retry* in those words — because a model's instinct on hearing something went
wrong is to try again.

**A failed read-back is not a contradiction.** If the system goes down between
the write and the read, that is `unavailable`. Reporting it as "your ticket did
not close" is a false alarm, and a verifier that cries wolf is one people learn
to ignore exactly when it is right.

**Verification never blocks the write.** It is bounded by a timeout (10 s by
default), and a broken predicate or an exploding plan builder becomes
`unavailable` rather than losing a write that succeeded.

**The read-back goes through the gateway.** Policy-checked, rate-limited and
audited like any other call. A verification pass that reached connectors by a
private door would be a hole in the record of what the assistant looked at.

One consequence worth knowing before you read a SIEM feed: the read-back is
logged *before* the write it verifies. The write's record carries the verdict, so
it cannot be written until the verdict exists. Chronologically the assistant
appears to read an issue it has not yet touched.

## What is covered today

| Tool | Read back with | Checks |
|---|---|---|
| `tasks.update_issue` | `tasks.get_issue` | the issue's state is what was asked for |

That is one tool, deliberately. The mechanism is proved end to end against a real
connector before it is spread across six — the same order the connectors
themselves were built in.

**`tasks.comment_on_issue` is not covered, and the reason is not laziness.** It
could be checked by re-reading the issue and looking for the comment body, but
the read tool returns only the last ten comments. On a busy issue a comment that
landed perfectly would read back as missing, and a verifier that reports false
contradictions on healthy writes is worse than no verifier at all. Covering it
properly needs the comment id threaded out of the write.

## The gap that is not covered

**Only successful mutations are verified.** The inverse — a connector reporting
failure on a write that actually landed, after a timeout on the vendor's side —
is the nastier drift, and nothing here catches it. It needs predicates that
assert *absence*, which is a different shape of plan.

Named rather than implied, because "verified" is a word this product sells and a
gate with a hole in it that nobody documents is worse than a smaller gate.

## Adding a connector

The connector declares it, because only the connector knows which read answers
"did that actually happen":

```python
def register_gitea_verification(verifier) -> None:
    def plan_for_update(arguments, _result):
        return VerificationPlan(
            tool="tasks.get_issue",
            arguments={"issue": arguments["issue"]},
            expect=lambda result: (result.structured or {}).get("state") == expected,
            describes=f"{reference} is {expected}",
        )

    verifier.register("tasks.update_issue", plan_for_update)
```

`expect` reads the **read-back's** result, never the write's own response.
Comparing a write against what it said about itself verifies nothing: it is the
same claim, made twice.

Return `None` from the builder when a particular call cannot be checked — an
unparseable reference, a tool whose arguments do not identify a record. `None`
means `unverifiable`, which is honest. Guessing at a record to re-read is not.
