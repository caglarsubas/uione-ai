from __future__ import annotations

import pytest

from uione.agent import ToolNameResolver, extract_json, validate_and_repair

MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "unread_only": {"type": "boolean"},
        "limit": {"type": "integer"},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        "labels": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query"],
}


# -- JSON recovery ---------------------------------------------------------


def test_clean_json_parses() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json_is_recovered() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_embedded_in_prose_is_recovered() -> None:
    raw = 'Sure, I will search now: {"query": "budget"} — let me know if that helps.'
    assert extract_json(raw) == {"query": "budget"}


def test_braces_inside_strings_do_not_confuse_extraction() -> None:
    assert extract_json('{"query": "a } b"}') == {"query": "a } b"}


def test_empty_arguments_are_an_empty_object() -> None:
    assert extract_json("") == {}


def test_unrecoverable_json_returns_none() -> None:
    assert extract_json("this is not json at all") is None


# -- the defect the model trials actually found ----------------------------


def test_string_boolean_is_repaired() -> None:
    """llama3.2:3b emitted {"unread_only": "true"} against a boolean schema."""
    result = validate_and_repair('{"query": "q", "unread_only": "true"}', MAIL_SCHEMA)

    assert result.ok
    assert result.arguments["unread_only"] is True
    assert result.was_repaired


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("True", True), ("false", False), ("no", False), ("YES", True)],
)
def test_boolean_spellings(raw: str, expected: bool) -> None:
    result = validate_and_repair({"query": "q", "unread_only": raw}, MAIL_SCHEMA)
    assert result.arguments["unread_only"] is expected


def test_ambiguous_boolean_strings_are_left_alone() -> None:
    """'1' could mean the number; guessing is worse than passing it through."""
    result = validate_and_repair({"query": "q", "unread_only": "1"}, MAIL_SCHEMA)
    assert result.arguments["unread_only"] == "1"


# -- type coercion ---------------------------------------------------------


def test_string_integer_is_repaired() -> None:
    result = validate_and_repair({"query": "q", "limit": "25"}, MAIL_SCHEMA)
    assert result.arguments["limit"] == 25


def test_integral_float_becomes_integer() -> None:
    result = validate_and_repair({"query": "q", "limit": 25.0}, MAIL_SCHEMA)
    assert result.arguments["limit"] == 25


def test_number_becomes_string_when_schema_says_string() -> None:
    result = validate_and_repair({"query": 2026}, MAIL_SCHEMA)
    assert result.arguments["query"] == "2026"


def test_scalar_is_wrapped_when_schema_wants_an_array() -> None:
    result = validate_and_repair({"query": "q", "labels": "urgent"}, MAIL_SCHEMA)
    assert result.arguments["labels"] == ["urgent"]


def test_enum_casing_is_corrected() -> None:
    result = validate_and_repair({"query": "q", "priority": "HIGH"}, MAIL_SCHEMA)
    assert result.arguments["priority"] == "high"


def test_unknown_enum_value_is_not_invented() -> None:
    result = validate_and_repair({"query": "q", "priority": "urgent"}, MAIL_SCHEMA)
    assert result.arguments["priority"] == "urgent"


def test_nested_objects_are_repaired() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": {"unread": {"type": "boolean"}},
            }
        },
    }
    result = validate_and_repair({"filter": {"unread": "false"}}, schema)
    assert result.arguments["filter"]["unread"] is False


def test_array_items_are_repaired() -> None:
    schema = {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
    }
    result = validate_and_repair({"ids": ["1", "2"]}, schema)
    assert result.arguments["ids"] == [1, 2]


# -- refusing to invent data ----------------------------------------------


def test_missing_required_argument_is_refused_not_defaulted() -> None:
    """Silently defaulting a missing recipient is how an assistant does damage."""
    result = validate_and_repair({"unread_only": True}, MAIL_SCHEMA)

    assert not result.ok
    assert "query" in (result.error or "")


def test_null_required_argument_is_refused() -> None:
    result = validate_and_repair({"query": None}, MAIL_SCHEMA)
    assert not result.ok


def test_error_message_tells_the_model_what_to_do() -> None:
    result = validate_and_repair({}, MAIL_SCHEMA)
    assert "do not guess" in (result.error or "").lower()


def test_unparseable_arguments_are_refused_with_guidance() -> None:
    result = validate_and_repair("I'll search your mail now", MAIL_SCHEMA)

    assert not result.ok
    assert "JSON" in (result.error or "")


# -- pass-through behaviour ------------------------------------------------


def test_valid_arguments_are_untouched() -> None:
    args = {"query": "budget", "unread_only": True, "limit": 10}
    result = validate_and_repair(args, MAIL_SCHEMA)

    assert result.ok
    assert result.arguments == args
    assert not result.was_repaired


def test_unknown_keys_are_preserved() -> None:
    """Connectors often accept more than they declare; dropping args is worse."""
    result = validate_and_repair({"query": "q", "extra": "value"}, MAIL_SCHEMA)
    assert result.arguments["extra"] == "value"


def test_optional_nulls_are_dropped() -> None:
    result = validate_and_repair({"query": "q", "limit": None}, MAIL_SCHEMA)
    assert "limit" not in result.arguments


def test_repairs_are_reported_for_observability() -> None:
    result = validate_and_repair({"query": "q", "unread_only": "true", "limit": "5"}, MAIL_SCHEMA)
    assert len(result.repairs) == 2


# -- tool name resolution --------------------------------------------------


@pytest.fixture
def resolver() -> ToolNameResolver:
    return ToolNameResolver(["mail.search", "mail.send", "jira.create_issue"])


def test_exact_name_resolves(resolver: ToolNameResolver) -> None:
    assert resolver.resolve("mail.search") == ("mail.search", None)


def test_dropped_namespace_resolves_when_unique(resolver: ToolNameResolver) -> None:
    assert resolver.resolve("create_issue")[0] == "jira.create_issue"


def test_case_insensitive_match_resolves(resolver: ToolNameResolver) -> None:
    assert resolver.resolve("Mail.Search")[0] == "mail.search"


def test_separator_variation_resolves(resolver: ToolNameResolver) -> None:
    assert resolver.resolve("create-issue")[0] == "jira.create_issue"


def test_ambiguous_bare_name_is_an_error_not_a_guess() -> None:
    """Picking the wrong 'search' searches the wrong system."""
    resolver = ToolNameResolver(["mail.search", "jira.search"])

    resolved, error = resolver.resolve("search")

    assert resolved is None
    assert "ambiguous" in (error or "")


def test_unknown_tool_lists_the_alternatives(resolver: ToolNameResolver) -> None:
    resolved, error = resolver.resolve("delete_everything")

    assert resolved is None
    assert "mail.search" in (error or "")
