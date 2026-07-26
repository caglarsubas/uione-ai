"""Tool access policy and rate limiting.

Deny by default. A tool is invisible and uncallable until a role is explicitly
granted it, because the alternative — everything allowed unless blocked — means
every new connector silently widens every user's reach.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from uione.mcphub.types import Principal, RiskClass, ToolSpec


@dataclass(frozen=True)
class Grant:
    """Permission for a role to use a set of tools.

    ``tools`` accepts exact qualified names (``mail.send_message``) or a
    server-wide wildcard (``mail.*``). Wildcards may be capped by risk so a role
    can be granted broad read access without inheriting the ability to send mail.
    """

    role: str
    tools: frozenset[str]
    max_risk: RiskClass = RiskClass.READ

    def covers(self, spec: ToolSpec) -> bool:
        if spec.qualified_name in self.tools:
            return True
        return f"{spec.server}.*" in self.tools and _risk_rank(spec.risk) <= _risk_rank(
            self.max_risk
        )


_RISK_ORDER = (
    RiskClass.READ,
    RiskClass.REVERSIBLE_WRITE,
    RiskClass.EXTERNAL_FACING,
    RiskClass.IRREVERSIBLE,
)


def _risk_rank(risk: RiskClass) -> int:
    return _RISK_ORDER.index(risk)


class ToolPolicy:
    """Decides whether a principal may call a tool."""

    def __init__(self, grants: Iterable[Grant] = ()) -> None:
        self._grants: list[Grant] = list(grants)

    def grant(self, grant: Grant) -> None:
        self._grants.append(grant)

    def allows(self, principal: Principal, spec: ToolSpec) -> bool:
        return any(g.role in principal.roles and g.covers(spec) for g in self._grants)

    def visible_tools(self, principal: Principal, specs: Iterable[ToolSpec]) -> list[ToolSpec]:
        """Tools this principal may use.

        The model is only ever shown these. Filtering at the prompt rather than at
        execution keeps the model from proposing actions the user cannot take —
        which reads to the user as the assistant being confused, and wastes a turn.
        """
        return [s for s in specs if self.allows(principal, s)]


class RateLimitExceeded(RuntimeError):
    pass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class RateLimiter:
    """Token bucket per (principal, tool).

    Bounds the damage of a runaway agent loop: an agent stuck retrying
    ``send_message`` should hit a wall long before the mail server does.
    """

    capacity: float = 30.0
    refill_per_second: float = 0.5
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def check(self, principal: Principal, tool: str) -> bool:
        """Consume one token. Returns False when the caller must back off."""
        now = self.clock()
        key = (principal.user_id, tool)
        bucket = self._buckets.get(key)

        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self.capacity - 1, updated_at=now)
            return True

        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_second)
        bucket.updated_at = now

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
