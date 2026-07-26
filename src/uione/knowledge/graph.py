"""The work graph — gap G4.

Twenty connectors give an assistant *reach*. This gives it *coherence*: the
knowledge that the supplier's email, the reconciliation ticket, and the invoice
number are one piece of work rather than three unrelated items in three lists.

Three queries carry the product value:

* :meth:`WorkGraph.about` — everything touching one entity, for "what's the story
  with INC-4471?"
* :meth:`WorkGraph.clusters` — items grouped by what they share, which is what
  turns a flat brief into a narrative
* :meth:`WorkGraph.duplicates_of` — the same event arriving through four
  channels, collapsed to one (gap G7)

Held in memory and rebuilt per request. That is honest for the current scale —
one user's morning is hundreds of items, not millions — and the interface is what
a persistent store later has to satisfy.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from uione.knowledge.entities import EntityKind, EntityRef, GraphItem
from uione.knowledge.extract import ExtractionRules, extract_entities

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Link:
    """A connection between two items, and the evidence for it.

    ``via`` exists so a link is explainable. "These are related, trust me" is
    exactly the kind of claim that erodes confidence when it is occasionally
    wrong; "these both reference INV-88213" can be checked in a second.
    """

    source: GraphItem
    target: GraphItem
    via: frozenset[EntityRef]

    @property
    def strength(self) -> int:
        return len(self.via)

    def explain(self) -> str:
        shared = ", ".join(sorted(str(e) for e in self.via))
        return f"{self.source.summary()} ↔ {self.target.summary()} (via {shared})"


@dataclass
class Cluster:
    """Items that belong to one piece of work."""

    anchor: EntityRef
    items: list[GraphItem] = field(default_factory=list)

    @property
    def sources(self) -> set[str]:
        return {item.source for item in self.items}

    @property
    def cross_system(self) -> bool:
        """True when this cluster spans more than one system.

        The single most useful signal in a brief: one thing appearing in mail,
        the tracker and the incident queue is what the user needs to see whole.
        """
        return len({s.split(".")[0] for s in self.sources}) > 1

    def render(self) -> str:
        lines = [f"{self.anchor} — {len(self.items)} related item(s):"]
        lines.extend(f"  - [{item.source}] {item.summary()}" for item in self.items)
        return "\n".join(lines)


class WorkGraph:
    def __init__(self, rules: ExtractionRules | None = None) -> None:
        self._rules = rules or ExtractionRules()
        self._items: dict[str, GraphItem] = {}
        self._by_entity: dict[str, set[str]] = defaultdict(set)
        self._entities: dict[str, EntityRef] = {}

    # -- ingestion ---------------------------------------------------------

    def add(self, item: GraphItem) -> GraphItem:
        """Index one item, extracting entities from its text."""
        mentions = set(item.mentions)
        mentions |= extract_entities(f"{item.title}\n{item.body}", self._rules)
        # An item never counts as mentioning itself; that would make every item
        # its own duplicate and collapse the whole graph.
        mentions.discard(item.subject)
        item.mentions = mentions

        self._items[item.id] = item
        self._entities[item.subject.id] = item.subject
        self._by_entity[item.subject.id].add(item.id)
        for ref in mentions:
            self._entities[ref.id] = ref
            self._by_entity[ref.id].add(item.id)
        return item

    def add_all(self, items: list[GraphItem]) -> None:
        for item in items:
            self.add(item)

    # -- queries -----------------------------------------------------------

    @property
    def items(self) -> list[GraphItem]:
        return list(self._items.values())

    def entities(self, kind: EntityKind | None = None) -> list[EntityRef]:
        refs = list(self._entities.values())
        return [r for r in refs if kind is None or r.kind is kind]

    def about(self, ref: EntityRef) -> list[GraphItem]:
        """Every item that is, or mentions, this entity."""
        return [self._items[i] for i in sorted(self._by_entity.get(ref.id, set()))]

    def links_for(self, item: GraphItem) -> list[Link]:
        """Other items sharing at least one entity with this one."""
        shared: dict[str, set[EntityRef]] = defaultdict(set)

        # Two directions matter: what this item mentions, and who mentions it.
        for ref in item.mentions:
            for other_id in self._by_entity.get(ref.id, set()):
                if other_id != item.id:
                    shared[other_id].add(ref)

        for other_id in self._by_entity.get(item.subject.id, set()):
            if other_id != item.id:
                shared[other_id].add(item.subject)

        links = [
            Link(source=item, target=self._items[other_id], via=frozenset(refs))
            for other_id, refs in shared.items()
        ]
        return sorted(links, key=lambda link: (-link.strength, link.target.id))

    def duplicates_of(self, item: GraphItem) -> list[GraphItem]:
        """Items describing the same event through a different channel.

        Deliberately narrow: only items that name this item's *subject*. Sharing
        an invoice number makes two items related; it does not make them the same
        event, and over-merging is how a brief hides something the user needed.
        """
        return [
            other
            for other in self.about(item.subject)
            if other.id != item.id and item.subject in other.mentions
        ]

    def clusters(self, *, min_items: int = 2) -> list[Cluster]:
        """Group items by the entities they share, largest first."""
        found: list[Cluster] = []
        for entity_id, item_ids in self._by_entity.items():
            if len(item_ids) < min_items:
                continue
            found.append(
                Cluster(
                    anchor=self._entities[entity_id],
                    items=[self._items[i] for i in sorted(item_ids)],
                )
            )
        return sorted(found, key=lambda c: (-len(c.items), c.anchor.id))

    def cross_system_clusters(self) -> list[Cluster]:
        return [c for c in self.clusters() if c.cross_system]

    def render_context(self, *, limit: int = 6) -> str:
        """Render the interesting links for a prompt.

        Only cross-system clusters, because a brief's value is in the joins the
        user cannot make by glancing at one tool. Two tickets in the same project
        referencing each other is not news.
        """
        clusters = self.cross_system_clusters()[:limit]
        if not clusters:
            return ""
        lines = ["The following items are connected across systems:"]
        lines.extend(cluster.render() for cluster in clusters)
        return "\n".join(lines)
