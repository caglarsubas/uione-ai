# Permission-aware retrieval — gap G3

The strategy document calls this the single hardest connector-side requirement.
The failure it prevents is concrete and career-ending: **the assistant surfacing
the layoff plan to an intern**, because the index knew the document existed and
nobody asked whether that person was allowed to see it.

## Filtering happens before ranking

"Retrieve the top 20, then filter" is the natural implementation and it leaks.

Not the document *text* — the *existence* of documents. Result counts shift.
Relevance scores move as the corpus changes. "No results" and "results you cannot
see" become distinguishable. A user learns that a confidential merger document
exists, which is frequently the sensitive part.

So `search()` narrows to the permitted set **first**, then computes BM25
statistics over that subset only. A test asserts the consequence directly: adding
twenty documents the intern cannot see does not move the intern's scores by a
single decimal.

There is no unfiltered accessor to reach for by mistake — every path that returns
a document takes a principal.

## Deny by default

A document with no ACL is visible to **nobody**, not everybody.

An ingestion bug that loses permissions must make content *disappear* — which
someone notices and reports within a day — rather than make it universally
readable, which nobody notices until it is a headline. Documents in that state
are counted in `stats().unreadable`, because a whole source there means a broken
permission sync rather than a genuinely private corpus.

## Other properties, each with a test

**The ACL travels with the document.** Not looked up later, not inferred from a
folder path — carried on the record, so there is no window in which content is
indexed and permissions are not.

**Denials win.** Source systems have deny rules, and dropping them silently
widens access. An explicit denial beats a group grant and beats organisation-wide
visibility.

**Missing and forbidden are indistinguishable.** `get()` returns `None` for both,
so probing identifiers reveals nothing.

**Revocation is immediate and cheap.** It is the operation with a deadline: a
permission removed in SharePoint that lingers here is a live leak for as long as
it lingers. `remove_source()` exists for the harder case — when a source's
permissions cannot be re-synced at all, deleting the content is the correct
response to not knowing who may see it.

**Reads are audited.** Reads can leak, so a search is recorded like any other
tool call.

## A stopword decision worth stating

`"it"` is **not** a stopword. Lowercased, the pronoun and the department are the
same token, and dropping it makes "IT budget" unsearchable. BM25's IDF already
down-weights terms that appear everywhere — which is precisely the mechanism an
aggressive stopword list duplicates, badly. The list is short on purpose.

## Verified through the gateway

Not only against the index — through the gateway, as a real request arrives:

```
intern → knowledge.search "restructuring"   → count 0, nothing confidential
hr     → knowledge.search "restructuring"   → the confidential plan
intern → knowledge.fetch  "hr-1"            → refused, no title leaked

forbidden search and nonsense search return identical structure
hr → intern → hr, same query: no bleed between callers
```

The gateway binds the calling principal into the source before invoking it, so a
retrieval tool cannot run without knowing who is asking.

## Ingestion, and deriving the ACL

The index *enforces* permissions; ingestion decides what they **are**. That is the
harder half, and where mirrored-permission designs meet their first real
disagreement with the source system.

**An ACL that cannot be derived is not a document.** Not "index it and sort the
permissions out later" — later never arrives, and meanwhile the content is either
readable by everyone or invisible and nobody knows which. Skipped documents are
counted and reported, never dropped silently.

**A failed sync does not advance the watermark.** Otherwise the next incremental
run skips exactly what was never fetched.

**Re-sync is a deadline, not a background nicety.** A permission removed at the
source is a live leak until it lands here, so ACL refresh is separated from
content refresh — permissions can be re-verified often and cheaply without
refetching bodies. A failing ACL read changes nothing: serving slightly stale
permissions beats deleting a corpus on a network blip.

`quarantine()` drops a source entirely, for when its permissions cannot be
verified at all. Deleting content is the correct response to not knowing who may
see it.

### A bug this shipped with in its first draft

A source with *static* permissions — a personal mailbox, where the owner never
changes — returned an empty mapping from `current_acls`. Re-sync read that as
"the source knows about none of these documents" and **deleted the entire
source, quietly, on every refresh**.

It now returns `None`, which means "static, nothing to do", explicitly distinct
from `{}`. The distinction is documented on the protocol method because the
failure is silent and total, and a test pins it.

### Mail as the first real source

Mail is the right place to start mirroring permissions because its access model
is unambiguous: a personal mailbox is readable by exactly one person. That makes
mistakes obvious rather than subtle. The ACL names the **individual**, not a
role — a role-based ACL would widen the moment someone joins that role.

Shared mailboxes and distribution lists are a harder problem, deliberately not
handled rather than approximated.

### Verified against a real IMAP server

Two mailboxes on one server, over a socket:

```
mail:alice: 2 indexed        mail:bob: 1 indexed
index holds 3 documents from ['mail:alice', 'mail:bob']

alice searches "budget"        → Q3 budget review
bob   searches "budget"        → (nothing — it is Alice's)
alice searches "compensation"  → nothing        ← Bob's confidential salary mail
bob   searches "compensation"  → Salary banding review

permission re-sync             → 0 removed, index still holds 3
```

## Not yet

Embeddings and reranking (lexical BM25 today), ACL derivation for sources with
*real* group permissions — Confluence, SharePoint, a file share — and
persistence: the index is in-memory and rebuilt on start.

Mail proves the mechanism against an unambiguous access model. A source with
genuine group ACLs is where the design will first be argued with, and that is the
next thing worth doing.
