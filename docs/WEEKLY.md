# The weekly review, and the daily census behind it

## Why a week is not a longer day

The morning brief answers *what needs me now*. A weekly review answers *what
changed, and is any of it unusual* — which is a question about history, and
history is the one thing connectors cannot answer. Every connector reports what
is true this instant.

Before this, the `WEEKLY_REVIEW` job existed in name only: it ran the morning
brief and changed the greeting to "Here is your week". That is worse than not
having it, because the label promises a different kind of thinking and delivers
the same list of today's unread mail.

## The census

Once a day, per person, the size of every queue: open incidents, open tasks, open
claims, firing alerts, unread chat, unread mail.

**Queue sizes only.** These are the metrics where a change genuinely means
something happened — incidents doubling is a bad week, claims flatlining is a
broken feed. Latency, revenue and conversion belong to the systems that own them,
and re-deriving them here produces a second set of numbers that disagrees with
the dashboard everyone already trusts.

**What the tool returned, not what the vendor holds.** The count is of records
*this principal can see*, because that is the number they experience. An
estate-wide count would be a different metric wearing the same name.

**A failed source records nothing, never zero.** This is the one that matters. A
connector outage writing a `0` puts a cliff in the series, the detector fires on
it, and somebody is told their incidents vanished overnight. A gap is honest; a
zero is a lie with a chart behind it.

**A day is a key.** Rows are keyed on the date, so a restart or a retried tick
overwrites instead of double-counting. Two entries for one Tuesday would quietly
skew the detector's weekday baseline and nothing would ever surface it.

## The review

Two comparisons, deliberately kept apart:

* **A** — this week's daily average against last week's. Averages rather than
  endpoints: comparing today with the number exactly seven days ago makes the
  whole review depend on which two days those were, and one bank holiday makes
  every metric look like a crisis.
* **B** — the most recent single day against that metric's own history, through
  the [anomaly detector](BI.md).

**The model writes the prose and never does the sums.** Every figure is computed
in Python and handed over as text to rephrase. Asking a model to compare this
week's count with last week's is asking for a plausible number, and the recurring
finding in [EVALS.md](EVALS.md) is that open-weight models invent field values
with total confidence.

### Two things a real model taught this file

Both were found by reading actual output, not by reasoning about it:

**The comparisons must be labelled as different questions.** An earlier version
headed them "Week on week" and "Unusual against this metric's own history", and
the model produced *"open tasks dropped 57.1% week over week (3 vs 7)"* — where 3
and 7 came from the *other* comparison. Both numbers were real; the sentence was
not. They are now `COMPARISON A` and `COMPARISON B`, with the difference spelled
out.

**A finding must carry its date.** Given "the same weekday baseline" with no
date, the model wrote *"the drop on Friday"* — a confident invention about a day
never mentioned. Findings now carry the day they describe.

## What it says when it cannot say much

A new deployment gets: *"No history yet — a review needs about two weeks before
it can compare anything."* A metric with too little history is named as
unassessed rather than folded into a cheerful summary, because "nothing unusual"
over three metrics while eight were skipped is the sentence that ends trust the
day somebody checks.

If the model plane is down, the figures are still the report. Losing the prose is
a degradation; losing the numbers would be a failure.
