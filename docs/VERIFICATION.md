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
| `unavailable` | The read-back could not be completed, or could not settle it | no |

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
| `incidents.update_incident` | `incidents.get_incident` | the incident's state **code** matches |
| `claims.set_status` | `claims.get_claim` | the claim's status matches |
| `mail.mark_read` | `mail.list_unread` | the uid is **absent** from the unread list |

ServiceNow is where this matters most, because it has the widest gap between "the
API accepted it" and "the record changed": business rules, client scripts and
workflow transitions all run *after* the Table API returns, and an instance that
rejects a transition answers the PATCH with the record as it now stands rather
than an error. The connector reports what it was told, truthfully, and is wrong.

Claims is where an unverified write is most *expensive* — a status drives
reserves and regulatory clocks — and it is also the connector we have never run
against the real vendor. A check that compares the server's own answer to what
was asked for is the one assurance here that does not depend on our mock being
faithful.

## Absence, and the third answer

`mail.mark_read` is checked by re-reading the unread list and requiring the uid to
be **gone**. That shape needed something the first version of this did not have.

A predicate returning `bool` cannot express "I read the answer and it did not
settle the question". Absence is only provable against a *complete* list: if the
mailbox has more unread messages than the read returns, a missing uid could be
genuinely read or could be sitting just past the ceiling. Forced to choose, such
a predicate must either raise a false alarm or — far worse — manufacture a
confirmation out of a list nobody finished reading.

So `expect` may return `None`, which reports `unavailable`:

| Returns | Verdict |
|---|---|
| `True` | `confirmed` |
| `False` | `contradicted` |
| `None` | `unavailable` — the read-back could not settle it |

`test_absence_against_a_truncated_list_is_unavailable_not_confirmed` holds that
line. Deleting the truncation guard makes it report `write_confirmed` for a write
nobody checked, which is precisely the class of thing this feature exists to
prevent rather than commit.

## What is deliberately not covered

| Tool | Why not |
|---|---|
| `mail.send_reply` | SMTP. There is no read that answers "did that leave the building". |
| `whatsapp.send_message` | Same — outbound, no read-back. |
| `tasks.comment_on_issue` | `get_issue` returns only the last ten comments, so a comment that landed perfectly reads back as missing on a busy issue. Needs the comment id threaded out of the write. |
| `claims.add_note` | `get_claim` returns attributes, not the note history. Nothing to look for. |
| `chat.send_message` | `read_channel` returns no message identifiers, so a match would be prose-shaped and fragile. |
| `calendar.propose_meeting` | `calendar.upcoming` reports a count and no identifiers. |
| `documents.write_document` | No read tool over the share. |

Every one of these is a false-contradiction risk, not an oversight: re-reading and
failing to find something that landed correctly is worse than an honest
`unverifiable`, because a verifier that cries wolf is one people learn to ignore.

The outbound two cannot be fixed by any amount of effort — their safety comes
from being `EXTERNAL_FACING` (egress-checked, never auto-run while tainted, always
shown to a human first) rather than from a confirmation nobody can obtain.

## The gap that is still not covered

**Only successful mutations are verified.** The inverse — a connector reporting
failure on a write that actually landed, after a timeout on the vendor's side — is
the nastier drift, and nothing here catches it.

It is closer than it was: that case needs predicates that assert absence, and
`mail.mark_read` is now a working example of one. What remains is running the
check on the failure path and inverting the expectation.

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

`expect` returns `True` to confirm, `False` to contradict, and `None` when the
read-back could not settle it — see [absence](#absence-and-the-third-answer).

It reads the **read-back's** result, never the write's own response.
Comparing a write against what it said about itself verifies nothing: it is the
same claim, made twice.

Return `None` from the builder when a particular call cannot be checked — an
unparseable reference, a tool whose arguments do not identify a record. `None`
means `unverifiable`, which is honest. Guessing at a record to re-read is not.
