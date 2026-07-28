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

### A file share: the first contested permissions

Mail has one reader. A file share has owners, groups, and — the part naive
mirroring gets wrong — a **containing directory chain**.

**The mistake that publishes things.** A file with mode `0644` looks readable by
everyone. Inside a `0700` directory nobody but the owner can reach it. Mirroring
the file's own bits alone hands it to the organisation. So the effective ACL is
the *intersection down the chain*, and directories need the execute bit too —
read without execute lists a directory but does not let you enter it.

**Group membership is the directory's answer, not ours.** We map a gid to a group
name and stop. An index keeping its own opinion of who is in a group will drift.

**Unresolvable means excluded.** An unmapped uid or gid produces no grant. A
numeric id we cannot name is one we cannot reason about, and the skipped count is
surfaced so an operator sees a mapping problem rather than a quiet corpus of
unreachable files.

**Symlinks are checked.** A link out of the share would otherwise be indexed
under the *share's* permissions, publishing content whose real ACL was never
consulted.

The identity map is explicit rather than read from `/etc/passwd`: the account
running the connector is rarely the account model the application authenticates
against, and equating them silently gives a service account an employee's
permissions.

#### The test setup this corrected

Every "world readable" assertion failed on the first run. The cause was not the
code — pytest's `tmp_path` is `0700`, and the chain rule correctly narrowed
everything to the owner. Real share mounts are `0755`. The chain check caught an
unrealistic test setup, which is a reasonable way for it to earn its place.

Tests write real files and `chmod` them, so derivation is checked against the
operating system's actual answer rather than a fixture written to match my
assumptions.

### Inherited permissions and nested groups

POSIX is contested but *shallow*. The deeper shape — Confluence space → page →
child page, SharePoint site → library → item, nested LDAP groups — is where the
subtle failures live.

`Hierarchy` resolves it against semantics stated explicitly, so a connector
author can check them against a vendor's documentation:

1. `inherits=True` extends the parent's resolved permissions with the node's own.
2. `inherits=False` uses only the node's own grants — SharePoint calls this
   breaking inheritance, Confluence calls it a page restriction.
3. **Denials always inherit, even through a break.**

Rule 3 is the asymmetry that matters, and the deliberate choice. A break is a
statement about who *may* read, not a pardon: if an ancestor explicitly excluded
someone, a subtree cannot quietly readmit them. Treating a break as clearing
denials is the bug that **reinstates a departed contractor**.

It is stricter than some products. Where a real system disagrees, the connector
records that rather than this module loosening — being stricter denies access
someone should have, which they report; being looser grants access nobody asked
for, which they do not.

Malformed hierarchies terminate: a parent cycle (a soft-deleted container whose
parent pointer still resolves) stops rather than loops, and depth is bounded so
truncation errs strict.

#### Nested groups expand on the principal, not the grant

A grant to `engineering` must reach someone carrying only `payments-team`.
Expansion happens on the **principal** side at check time, which means stored
ACLs stay identical to what the source system stated — an operator comparing our
ACL against Confluence sees the same names — and a nesting change takes effect
without reindexing anything.

Expansion goes *upward* only. Being in `engineering` does not put you in
`payments-team`; treating a grant to a subgroup as a grant to its parent is the
inverted-direction bug.

Cycles terminate (real directories contain loops, usually by accident), depth is
bounded, and **exceeding the bound is reported rather than silently truncated** —
a silent truncation removes access, which looks like a permissions bug to the
user and is invisible to us.

Without a configured group graph, matching is literal. A deployment that has not
described its directory gets no inferred hierarchy, however tempting `x-team` and
`x-team-leads` look.

#### What this does and does not prove

This is the resolution *algorithm*, tested against stated semantics. It is not a
vendor connector, and the mapping from Confluence's or SharePoint's actual model
onto these three fields is exactly where the next disagreement will be. Written
this way deliberately: the semantics are pinned and testable now, so a connector
author is checking a mapping rather than inventing one.

## Persistence

Documents are stored; the index is not. Postings are derived from the tokeniser
and the stopword list, so a persisted index would silently disagree with its own
documents the first time either changed — and a stale index is worse than one
that takes a second to rebuild at startup.

ACLs round-trip with the documents, and this is the assertion that matters: a
restored corpus whose permissions did not survive is a leak, not an
inconvenience. A permission revoked during a re-sync is written through
immediately rather than at shutdown, because that write has a deadline.

Sync watermarks are stored alongside. Without them an incremental sync after a
restart either refetches the whole corpus or — worse — asks each source for
changes "since now", so whatever changed while the service was down is never
seen.

## The refresh loops, and the staleness budget

Two loops, not one, because the two halves of a re-sync have different costs and
very different urgency:

| Loop | Default | What being late costs |
|---|---|---|
| Content sync | 15 minutes | A wiki page is an hour out of date. An inconvenience. |
| Permission re-sync | 2 minutes | Someone can read a document they were removed from an hour ago. Not an inconvenience. |

The harder question is what to do when permissions *cannot* be verified — the
source is down, credentials expired, the API changed. Serving documents under
permissions of unknown age is the exact thing this layer exists to prevent, so
after `UIONE_INGEST_MAX_ACL_AGE_S` (default an hour) without a successful check
the source is **quarantined**: its content is dropped from the index and from
storage.

That is deliberately drastic, and the reasoning is asymmetric. Search quietly
getting worse is visible, complained about, and recoverable. A leak is none of
those things.

Two details that are easy to get wrong:

**A brief outage must not empty the corpus.** The budget is a *duration*, not a
failure count — one failed check on a source verified a minute ago changes
nothing.

**Recovery refetches everything.** A quarantined source that comes back has its
watermark cleared first. An incremental fetch would ask for changes since the
last sync and return only recent ones, so the source would come back permanently
missing everything older while reporting itself healthy.

Freshness is reported at `/system/health` as a number per source — `acl_age_s`,
`quarantined`, `consecutive_failures`. "How old are the permissions we are
enforcing?" must be answerable rather than assumed.

### Verified with a real chmod

Not a mock filesystem and not a manual re-sync. A real file, indexed at startup,
then `chmod 600` at the source with nothing told to the assistant:

```
indexed at startup              alice sees 1 | mode 644 | acl_age_s 0
chmod 600 — nobody told the assistant
after the permission loop ran   alice sees 0 | mode 600 | acl_age_s 0
```

Note that the file connector degrades to *removal* rather than an exception: a
path it can no longer read looks gone, and gone means dropped from the index. So
the quarantine path is exercised by sources that raise — a network API — rather
than by this one. Both directions fail closed, which is the property that matters.

## Not yet

Embeddings and reranking (lexical BM25 today), and a real Confluence or
SharePoint connector to validate the ACL mapping above against a live system.


## Semantic retrieval, and the fusion that decides what wins

BM25 finds documents that share words with the query. It cannot find the
settlement runbook when somebody asks about *"batch payments not completing"* —
and that is the query people actually type.

Embeddings run on the same local model plane as everything else, which is what
keeps semantic search available in an air-gapped install. Nothing leaves the
building to make a vector.

### Four properties

**The permission invariant does not change.** Filter first, rank second. A vector
search that ranks the whole corpus and then removes what the caller may not read
leaks existence through result counts and through timing — the same leak the
lexical index was built to avoid.

**Ranks are fused, not scores.** A BM25 score and a cosine similarity live on
different scales, and any weighted sum needs a normalisation that depends on the
corpus. Reciprocal rank fusion needs only the ordering, so it cannot be
miscalibrated.

**Semantic search never takes search offline.** If the model plane is busy the
query falls back to lexical results and sets `semantic: false`. An assistant
whose search stops working because a GPU is busy is worse than one that
occasionally misses a synonym.

**Embeddings are stored; postings are not.** This looks inconsistent with the
index, which rebuilds its postings at startup, and the difference is cost: a
posting list is a tokeniser pass over text already in memory, an embedding is a
GPU round trip per document. Vectors are keyed by content hash *and* model name,
so an edited document or a changed model produces a miss rather than a
confidently wrong vector. A permission change does not invalidate anything —
ACLs change far more often than text.

### The failure that showed up on the third query ever run

Asked *"when can I take time off"*:

| | ranking |
|---|---|
| BM25 | `refunds` 0.72, `runbook` 0.60 — matched the words *time* and *off*. The holiday policy shared no terms and scored **nothing**. |
| Embedder | `holiday` 0.49, `vpn` 0.29, `runbook` 0.28, `refunds` 0.23 |
| Plain RRF | `refunds`, `runbook`, `holiday` |

The refund runbook appeared in *both* lists and won. The document that answered
the question came third. **Two mediocre votes beat one excellent vote** — the
known failure mode of naive rank fusion.

The fix is not a weight pulled out of the air. It is an asymmetry that already
exists between the two signals:

* **A similarity floor on the semantic side.** Cosine is comparable across
  queries for a fixed model — 0.2 means unrelated whatever was asked — so a
  constant is defensible here. A BM25 score is relative to query length and
  corpus statistics, so no constant would mean anything, and no floor is applied
  there.
* **A weight of 1.5 on semantic votes.** After the floor, a semantic vote means
  "similar above a stated threshold". A lexical vote means only "shares a term",
  with no relevance threshold at all. Weighting those equally is the arbitrary
  choice, not weighting them differently.

Measured against a real `embeddinggemma:300m` on a real file share:

| Query | Lexical alone | Hybrid |
|---|---|---|
| `why do customers get charged twice` | *nothing* | `refunds.md` |
| `when can I take time off` | `refunds`, `runbook` | `leave.md` |
| `PAY-1182` | `settlement` | `settlement.md` |

Exact identifier lookup still works, which matters because the work graph
depends on ticket keys.

### The scale this is honest about

Similarity is brute force: every stored vector is compared with the query, which
is linear in corpus size. That is fine for the tens of thousands of documents an
on-premise department has. It is not an approximate-nearest-neighbour index and
does not pretend to be — a deployment with millions of documents needs one.
