"""The permission-aware document index.

BM25 over an inverted index, with one structural rule that is the whole point:

**Permissions are applied before ranking, never after.**

"Retrieve the top 20, then filter" is the natural implementation and it leaks.
Not the document text — the *existence* of documents: result counts shift,
relevance scores move as the corpus changes, and "no results" versus "results
you cannot see" become distinguishable. A user learns the confidential merger
document exists, which is often the sensitive part.

So :meth:`DocumentIndex.search` narrows the candidate set to what the principal
may read, and only then scores. Every code path that returns documents goes
through the same filter — there is no unfiltered accessor to reach for by
mistake.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass

import structlog

from uione.knowledge.documents import AccessControl, Document
from uione.mcphub import Principal

log = structlog.get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'_-]*")

#: Words carrying no retrieval signal. Deliberately short, and notably *without*
#: "it" — lowercased, the pronoun and the department are the same token, and
#: dropping it makes "IT budget" unsearchable. BM25's IDF already down-weights
#: terms that appear everywhere, which is the mechanism an aggressive stopword
#: list duplicates badly.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "in",
        "is",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
    ]
)

# Standard BM25 parameters.
K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


@dataclass
class SearchHit:
    document: Document
    score: float
    snippet: str = ""

    def render(self) -> str:
        line = f"[{self.document.id}] {self.document.title}"
        if self.document.url:
            line += f" — {self.document.url}"
        if self.snippet:
            line += f"\n    {self.snippet}"
        return line


@dataclass
class IndexStats:
    documents: int = 0
    unreadable: int = 0
    """Indexed but permitted to nobody — a whole source here means broken sync."""


class DocumentIndex:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._postings: dict[str, dict[str, int]] = defaultdict(dict)
        self._lengths: dict[str, int] = {}
        self._total_length = 0

    # -- ingestion ---------------------------------------------------------

    def add(self, document: Document) -> Document:
        if document.id in self._documents:
            self.remove(document.id)

        tokens = tokenize(document.text)
        counts: dict[str, int] = defaultdict(int)
        for token in tokens:
            counts[token] += 1

        for token, count in counts.items():
            self._postings[token][document.id] = count

        self._documents[document.id] = document
        self._lengths[document.id] = len(tokens)
        self._total_length += len(tokens)

        if document.readable_by_nobody:
            log.warning(
                "retrieval.document_unreadable", document=document.id, source=document.source
            )
        return document

    def add_all(self, documents: list[Document]) -> None:
        for document in documents:
            self.add(document)

    def remove(self, document_id: str) -> None:
        document = self._documents.pop(document_id, None)
        if document is None:
            return
        for postings in self._postings.values():
            postings.pop(document_id, None)
        self._total_length -= self._lengths.pop(document_id, 0)

    def update_acl(self, document_id: str, acl: AccessControl) -> bool:
        """Re-apply permissions from the source.

        Revocation has to be cheap, because it is the operation with a deadline:
        a permission removed in SharePoint that lingers here is a live leak for
        as long as it lingers.
        """
        document = self._documents.get(document_id)
        if document is None:
            return False
        document.acl = acl
        return True

    def remove_source(self, source: str) -> int:
        """Drop everything from one connector.

        Used when a source's permissions cannot be re-synced: removing the
        content is the correct response to not knowing who may see it.
        """
        doomed = [d.id for d in self._documents.values() if d.source == source]
        for document_id in doomed:
            self.remove(document_id)
        return len(doomed)

    # -- retrieval ---------------------------------------------------------

    def _readable(self, principal: Principal) -> dict[str, Document]:
        return {
            document_id: document
            for document_id, document in self._documents.items()
            if document.acl.permits(principal)
        }

    def search(self, principal: Principal, query: str, *, limit: int = 5) -> list[SearchHit]:
        """Find documents this principal may read, best first.

        The permitted set is computed *first*. Scoring then happens against that
        set only, so corpus statistics — and therefore scores and result counts —
        never reveal anything about documents the principal cannot see.
        """
        terms = tokenize(query)
        if not terms:
            return []

        readable = self._readable(principal)
        if not readable:
            return []

        # Statistics over the readable subset, not the whole index. Using global
        # IDF would let scores shift when a document the user cannot see is
        # added — a slow but real inference channel.
        count = len(readable)
        average_length = sum(self._lengths[d] for d in readable) / count if count else 0.0

        scores: dict[str, float] = defaultdict(float)
        for term in terms:
            postings = {
                document_id: frequency
                for document_id, frequency in self._postings.get(term, {}).items()
                if document_id in readable
            }
            if not postings:
                continue

            idf = math.log(1 + (count - len(postings) + 0.5) / (len(postings) + 0.5))
            for document_id, frequency in postings.items():
                length = self._lengths[document_id]
                norm = frequency * (K1 + 1)
                denom = frequency + K1 * (1 - B + B * (length / (average_length or 1)))
                scores[document_id] += idf * (norm / denom)

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return [
            SearchHit(
                document=readable[document_id],
                score=round(score, 4),
                snippet=readable[document_id].snippet(query),
            )
            for document_id, score in ranked
        ]

    def get(self, principal: Principal, document_id: str) -> Document | None:
        """Fetch one document, if this principal may read it.

        Returns ``None`` both for "does not exist" and "not permitted", so a
        caller cannot distinguish the two and neither can a user probing ids.
        """
        document = self._documents.get(document_id)
        if document is None or not document.acl.permits(principal):
            return None
        return document

    def document_ids_for_source(self, source: str) -> list[str]:
        """Ids from one connector. Administrative, so it takes no principal —
        it returns identifiers, never content."""
        return [d.id for d in self._documents.values() if d.source == source]

    def acl_of(self, document_id: str) -> AccessControl | None:
        """The stored ACL, for comparison against the source during re-sync."""
        document = self._documents.get(document_id)
        return document.acl if document else None

    def visible_count(self, principal: Principal) -> int:
        return len(self._readable(principal))

    # -- administration ----------------------------------------------------

    def stats(self) -> IndexStats:
        return IndexStats(
            documents=len(self._documents),
            unreadable=sum(1 for d in self._documents.values() if d.readable_by_nobody),
        )

    def sources(self) -> set[str]:
        return {d.source for d in self._documents.values()}

    def __len__(self) -> int:
        return len(self._documents)
