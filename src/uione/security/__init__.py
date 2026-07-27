"""Cross-cutting security primitives that no layer may own."""

from uione.security.injection import (
    InjectionFinding,
    scan_for_injection,
)

__all__ = ["InjectionFinding", "scan_for_injection"]
