from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from uione.mcphub import MCPToolSource, RiskClass, classify_risk


@dataclass
class FakeAnnotations:
    readOnlyHint: bool | None = None  # noqa: N815 — mirrors the MCP wire schema
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815


@dataclass
class FakeTool:
    name: str
    description: str = ""
    inputSchema: dict | None = None  # noqa: N815
    annotations: Any = None


@dataclass
class FakeToolsResult:
    tools: list[FakeTool]


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeCallResult:
    content: Any
    isError: bool = False  # noqa: N815
    structuredContent: dict | None = None  # noqa: N815


class FakeSession:
    def __init__(self, tools: list[FakeTool], result: Any = None, raises: Exception | None = None):
        self._tools = tools
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> FakeToolsResult:
        return FakeToolsResult(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> Any:
        self.calls.append((name, arguments))
        if self._raises:
            raise self._raises
        return self._result


# -- risk classification ---------------------------------------------------


def test_read_only_hint_maps_to_read() -> None:
    assert classify_risk("search", FakeAnnotations(readOnlyHint=True)) is RiskClass.READ


def test_destructive_hint_maps_to_irreversible() -> None:
    assert classify_risk("delete", FakeAnnotations(destructiveHint=True)) is RiskClass.IRREVERSIBLE


def test_open_world_hint_maps_to_external_facing() -> None:
    assert classify_risk("post", FakeAnnotations(openWorldHint=True)) is RiskClass.EXTERNAL_FACING


def test_unclassified_tools_default_to_the_safe_assumption() -> None:
    """Mistaking a destructive tool for a read is the expensive direction."""
    assert classify_risk("mystery", None) is RiskClass.IRREVERSIBLE


def test_our_override_beats_the_servers_own_hint() -> None:
    """A connector claiming its send tool is read-only must not be believed."""
    risk = classify_risk(
        "send", FakeAnnotations(readOnlyHint=True), {"send": RiskClass.EXTERNAL_FACING}
    )
    assert risk is RiskClass.EXTERNAL_FACING


# -- adapter ---------------------------------------------------------------


async def test_tools_are_discovered_with_schema_and_risk() -> None:
    session = FakeSession(
        [
            FakeTool(
                "search",
                "Search things",
                {"type": "object", "properties": {"q": {"type": "string"}}},
                FakeAnnotations(readOnlyHint=True),
            )
        ]
    )

    specs = await MCPToolSource("jira", session).list_tools()

    assert len(specs) == 1
    assert specs[0].qualified_name == "jira.search"
    assert specs[0].risk is RiskClass.READ
    assert specs[0].parameters["properties"]["q"]["type"] == "string"


async def test_text_content_blocks_are_flattened() -> None:
    session = FakeSession(
        [], FakeCallResult([FakeTextBlock("line one"), FakeTextBlock("line two")])
    )

    result = await MCPToolSource("jira", session).call("search", {"q": "x"})

    assert result.ok
    assert result.content == "line one\nline two"


async def test_non_text_blocks_are_flagged_not_dropped() -> None:
    """Silently omitting content lets the model reason on a truncated result."""

    @dataclass
    class ImageBlock:
        type: str = "image"

    session = FakeSession([], FakeCallResult([FakeTextBlock("caption"), ImageBlock()]))

    result = await MCPToolSource("jira", session).call("get", {})

    assert "caption" in result.content
    assert "image content omitted" in result.content


async def test_tool_reported_errors_become_failures() -> None:
    session = FakeSession([], FakeCallResult([FakeTextBlock("no such issue")], isError=True))

    result = await MCPToolSource("jira", session).call("get", {"id": "X"})

    assert not result.ok
    assert result.error == "no such issue"


async def test_transport_exceptions_become_failures() -> None:
    session = FakeSession([], raises=TimeoutError("server gone"))

    result = await MCPToolSource("jira", session).call("get", {})

    assert not result.ok
    assert "TimeoutError" in (result.error or "")


async def test_structured_content_is_preserved() -> None:
    session = FakeSession(
        [], FakeCallResult([FakeTextBlock("ok")], structuredContent={"issues": [{"key": "A-1"}]})
    )

    result = await MCPToolSource("jira", session).call("search", {})

    assert result.structured == {"issues": [{"key": "A-1"}]}


async def test_arguments_reach_the_session_unchanged() -> None:
    session = FakeSession([], FakeCallResult([FakeTextBlock("ok")]))

    await MCPToolSource("jira", session).call("search", {"q": "budget", "limit": 10})

    assert session.calls == [("search", {"q": "budget", "limit": 10})]
