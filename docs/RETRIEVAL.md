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

## Not yet

Embeddings and reranking (lexical BM25 today), persistence — the index is
in-memory and rebuilt on start — and ACL derivation for systems with *inherited*
permission models: SharePoint's broken inheritance, Confluence space-versus-page
restrictions, nested LDAP groups. POSIX is contested but shallow; those are where
the next disagreements live.
