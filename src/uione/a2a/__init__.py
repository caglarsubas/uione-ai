"""A2A — assistants collaborating on their owners' behalf, under contract."""

from uione.a2a.answering import GatewayAnswerer
from uione.a2a.bus import A2ABus, Answerer
from uione.a2a.commitments import commitment_spec, render_commitment
from uione.a2a.contracts import (
    DEFAULT_EXTERNAL,
    DEFAULT_INTERNAL,
    DEFAULT_TEAM,
    ContractRegistry,
    Disclosure,
    DisclosureContract,
    Facet,
)
from uione.a2a.messages import (
    COMMITMENTS,
    REQUIRED_FACETS,
    A2ARequest,
    A2AResponse,
    AgentCard,
    AgentDirectory,
    Capability,
    Outcome,
    RequestKind,
)

__all__ = [
    "COMMITMENTS",
    "DEFAULT_EXTERNAL",
    "DEFAULT_INTERNAL",
    "DEFAULT_TEAM",
    "REQUIRED_FACETS",
    "A2ABus",
    "A2ARequest",
    "A2AResponse",
    "AgentCard",
    "AgentDirectory",
    "Answerer",
    "Capability",
    "ContractRegistry",
    "Disclosure",
    "DisclosureContract",
    "Facet",
    "GatewayAnswerer",
    "Outcome",
    "RequestKind",
    "commitment_spec",
    "render_commitment",
]
