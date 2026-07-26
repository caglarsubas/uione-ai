"""Tool-call reliability layer — gap G5.

Open-weight models drive tools well but not perfectly. The trials in
``docs/MODEL_TRIALS.md`` produced a concrete example: ``llama3.2:3b`` emitted
``{"unread_only": "true"}`` — a JSON *string* against a schema declaring a
boolean. It parses, then misbehaves inside the connector.

This module converts that class of model imperfection from a product risk into an
engineering problem we own: parse leniently, validate strictly, repair what is
unambiguously repairable, and refuse clearly when it is not.

The line that must not be crossed: **never invent data.** A missing required
argument is reported back to the model so it can supply one. Silently defaulting
a missing recipient or ticket ID is how an assistant does confident damage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_TRUE = frozenset({"true", "yes"})
_FALSE = frozenset({"false", "no"})


@dataclass
class RepairResult:
    """Outcome of validating and repairing one tool call's arguments."""

    ok: bool
    arguments: dict[str, Any] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def was_repaired(self) -> bool:
        return bool(self.repairs)


def extract_json(raw: str) -> dict[str, Any] | None:
    """Recover a JSON object from model output.

    Handles the three shapes models actually emit when they wander off-format:
    clean JSON, JSON inside a markdown fence, and JSON embedded in prose.
    """
    if not raw or not raw.strip():
        return {}

    candidates = [raw]

    if match := _FENCE.search(raw):
        candidates.append(match.group(1))

    # First balanced {...} span, for output with commentary around it.
    start = raw.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(raw[start : index + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _coerce_scalar(value: Any, schema: dict[str, Any], path: str, repairs: list[str]) -> Any:
    """Coerce a value toward its declared type when the intent is unambiguous.

    Conservative on purpose. ``"true"`` to ``True`` is safe because no other
    reading exists; ``"1"`` to ``True`` is not attempted, because the model may
    have meant the number.
    """
    declared = schema.get("type")
    if declared is None:
        return value

    types = {declared} if isinstance(declared, str) else set(declared)

    # Enum repair: right value, wrong casing.
    if (enum := schema.get("enum")) and value not in enum:
        if isinstance(value, str):
            matches = [e for e in enum if isinstance(e, str) and e.lower() == value.lower()]
            if len(matches) == 1:
                repairs.append(f"{path}: {value!r} -> {matches[0]!r} (enum casing)")
                return matches[0]
        return value

    if "boolean" in types and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            repairs.append(f"{path}: {value!r} -> True (string to boolean)")
            return True
        if lowered in _FALSE:
            repairs.append(f"{path}: {value!r} -> False (string to boolean)")
            return False

    if "integer" in types and not isinstance(value, bool):
        if isinstance(value, str):
            try:
                coerced = int(value.strip())
            except ValueError:
                pass
            else:
                repairs.append(f"{path}: {value!r} -> {coerced} (string to integer)")
                return coerced
        elif isinstance(value, float) and value.is_integer():
            repairs.append(f"{path}: {value} -> {int(value)} (float to integer)")
            return int(value)

    if "number" in types and isinstance(value, str):
        try:
            coerced = float(value.strip())
        except ValueError:
            pass
        else:
            repairs.append(f"{path}: {value!r} -> {coerced} (string to number)")
            return coerced

    if "string" in types and isinstance(value, int | float) and not isinstance(value, bool):
        repairs.append(f"{path}: {value} -> {str(value)!r} (number to string)")
        return str(value)

    if "array" in types and not isinstance(value, list) and value is not None:
        repairs.append(f"{path}: wrapped single value in a list")
        return [value]

    return value


def _repair_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    path: str,
    repairs: list[str],
    missing: list[str],
) -> dict[str, Any]:
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = schema.get("required") or []
    result: dict[str, Any] = {}

    for key, raw in value.items():
        prop_schema = properties.get(key)
        child_path = f"{path}.{key}" if path else key

        if prop_schema is None:
            # Unknown key. Keep it: connectors often accept more than they declare,
            # and dropping a caller's argument silently is worse than passing it on.
            result[key] = raw
            continue

        if raw is None and key not in required:
            continue

        result[key] = _repair_value(raw, prop_schema, child_path, repairs, missing)

    for key in required:
        if key not in result or result[key] is None:
            missing.append(f"{path}.{key}" if path else key)

    return result


def _repair_value(
    value: Any, schema: dict[str, Any], path: str, repairs: list[str], missing: list[str]
) -> Any:
    declared = schema.get("type")
    types = {declared} if isinstance(declared, str) else set(declared or ())

    if "object" in types and isinstance(value, dict):
        return _repair_object(value, schema, path, repairs, missing)

    coerced = _coerce_scalar(value, schema, path, repairs)

    if isinstance(coerced, list) and (item_schema := schema.get("items")):
        return [
            _repair_value(item, item_schema, f"{path}[{i}]", repairs, missing)
            for i, item in enumerate(coerced)
        ]

    return coerced


def validate_and_repair(
    raw_arguments: str | dict[str, Any], schema: dict[str, Any]
) -> RepairResult:
    """Parse, validate and repair tool arguments against a JSON Schema."""
    if isinstance(raw_arguments, str):
        parsed = extract_json(raw_arguments)
        if parsed is None:
            return RepairResult(
                ok=False,
                error=(
                    "arguments were not valid JSON. Respond with a single JSON object "
                    "matching the tool's schema."
                ),
            )
    else:
        parsed = raw_arguments

    repairs: list[str] = []
    missing: list[str] = []
    repaired = _repair_object(parsed, schema or {}, "", repairs, missing)

    if missing:
        return RepairResult(
            ok=False,
            arguments=repaired,
            repairs=repairs,
            error=(
                f"missing required argument(s): {', '.join(sorted(missing))}. "
                f"Supply them explicitly — do not guess."
            ),
        )

    if repairs:
        log.info("agent.arguments_repaired", repairs=repairs)

    return RepairResult(ok=True, arguments=repaired, repairs=repairs)


class ToolNameResolver:
    """Maps the name a model emitted onto a real catalog entry.

    Models drop namespaces (``search`` for ``mail.search``) and vary separators.
    Resolution is deliberately conservative: exact match, then a *unique*
    case-insensitive match, then a *unique* bare-name match. Ambiguity is an
    error, never a guess — picking the wrong ``search`` searches the wrong system.
    """

    def __init__(self, known: list[str]) -> None:
        self._known = list(known)

    def resolve(self, name: str) -> tuple[str | None, str | None]:
        """Return ``(resolved_name, error)``."""
        if name in self._known:
            return name, None

        normalised = name.strip()
        if normalised in self._known:
            return normalised, None

        lowered = normalised.lower()

        exact_ci = [k for k in self._known if k.lower() == lowered]
        if len(exact_ci) == 1:
            return exact_ci[0], None

        # Model dropped the server prefix, or used a different separator.
        bare = lowered.replace("-", "_").split(".")[-1]
        by_bare = [k for k in self._known if k.split(".")[-1].lower().replace("-", "_") == bare]
        if len(by_bare) == 1:
            return by_bare[0], None
        if len(by_bare) > 1:
            return None, (
                f"tool name {name!r} is ambiguous across servers: {', '.join(sorted(by_bare))}. "
                f"Use the fully qualified name."
            )

        return None, f"unknown tool {name!r}. Available tools: {', '.join(sorted(self._known))}"
