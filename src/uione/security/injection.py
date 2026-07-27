"""Known prompt-injection phrasings, and the scan over them.

A leaf module with no imports from the rest of the product, because two layers
that must not depend on each other both need it: `governance.containment` scans
tool *results* on the way back, and `mcphub.source` scans third-party tool
*descriptions* on the way in. Putting the patterns in either one would have the
hub importing governance, which inverts the layering the architecture depends on.

Detection is a signal, never the control. A scanner that misses a novel phrasing
must not mean the attack succeeds — which is why taint escalation is
unconditional on reading untrusted content rather than conditional on finding
something suspicious in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InjectionFinding:
    pattern: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.pattern}: {self.excerpt!r}"


# Phrasings that recur in real indirect-injection payloads. Signal for review and
# for the audit trail — deliberately not a gate.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions?", re.I),
    ),
    ("role_hijack", re.compile(r"you\s+are\s+now\s+(a|an|the)\b", re.I)),
    (
        "system_prompt_spoof",
        re.compile(r"</?(system|assistant)\s*>|\[/?INST\]|<\|im_start\|>", re.I),
    ),
    (
        "authority_claim",
        re.compile(r"(as|this\s+is)\s+(your\s+)?(administrator|developer|anthropic|openai)", re.I),
    ),
    (
        "exfiltration",
        re.compile(
            r"(send|forward|email|post|upload|exfiltrate)\s+.{0,40}(to|at)\s+\S+@|https?://", re.I
        ),
    ),
    (
        "secret_seeking",
        re.compile(
            r"(reveal|print|show|repeat)\s+.{0,30}(system\s+prompt|api[_\s]?key|password|token|credential)",
            re.I,
        ),
    ),
    (
        "urgency_pressure",
        re.compile(
            r"(urgent|immediately|do\s+not\s+ask|without\s+(asking|confirmation|approval))", re.I
        ),
    ),
)


def scan_for_injection(content: str, *, max_findings: int = 5) -> list[InjectionFinding]:
    """Look for known injection phrasings.

    Used for audit, alerting, and to warn the model — never to decide whether an
    action may proceed. Treating detection as the control would make every novel
    phrasing a bypass.
    """
    findings: list[InjectionFinding] = []
    for name, pattern in _INJECTION_PATTERNS:
        match = pattern.search(content)
        if match:
            start = max(0, match.start() - 20)
            end = min(len(content), match.end() + 20)
            findings.append(InjectionFinding(name, content[start:end].strip()))
        if len(findings) >= max_findings:
            break
    return findings
