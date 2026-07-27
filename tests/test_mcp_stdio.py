"""Speaking real MCP, to real servers.

Two servers, both actual subprocesses over actual pipes:

* `tests/mcpservers/real_server.py`, built with the **official MCP SDK**. The
  point is that we did not write the protocol side — a hand-written server on
  both ends would happily agree with itself about a misreading of the spec.
* `tests/mcpservers/hostile_server.py`, hand-written *because* the SDK would not
  let it misbehave. Every mode there is something a real server does, through
  compromise or through being written on a Friday.

No mocked transport anywhere in this file. The bugs that matter here — a pipe
buffer filling, a dead process leaving a request pending, a lie about risk — are
invisible to a fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpConfigError,
    McpError,
    McpGateway,
    McpSupervisor,
    MCPToolSource,
    Principal,
    RiskClass,
    ServerConfig,
    StdioMcpClient,
    ToolPolicy,
    parse_server_config,
)

SERVERS = Path(__file__).parent / "mcpservers"
REAL = SERVERS / "real_server.py"
HOSTILE = SERVERS / "hostile_server.py"

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))

mcp_sdk = pytest.importorskip("mcp", reason="the official MCP SDK provides the real server")


def real_config(**kwargs) -> ServerConfig:
    return ServerConfig(name="tickets", command=sys.executable, args=[str(REAL)], **kwargs)


def hostile_config(mode: str, **kwargs) -> ServerConfig:
    return ServerConfig(name="hostile", command=sys.executable, args=[str(HOSTILE), mode], **kwargs)


@pytest.fixture
async def real_client():
    client = StdioMcpClient(real_config())
    await client.start()
    yield client
    await client.aclose()


# -- the wire, against an implementation we did not write ------------------


async def test_the_handshake_negotiates_a_supported_protocol(real_client) -> None:
    assert real_client.protocol_version == "2025-06-18"
    assert real_client.server_info["name"] == "tickets"
    assert real_client.alive


async def test_tools_are_discovered_with_their_schemas(real_client) -> None:
    tools = {t.name: t for t in (await real_client.list_tools()).tools}

    assert set(tools) == {"search_issues", "close_issue", "explode"}
    assert tools["search_issues"].description == "Search the ticket system."
    assert set(tools["search_issues"].inputSchema["properties"]) == {"query", "limit"}


async def test_a_tool_call_returns_the_servers_answer(real_client) -> None:
    result = await real_client.call_tool("search_issues", {"query": "payments", "limit": 2})

    assert not result.isError
    assert "PAY-1182" in result.content[0].text


async def test_a_tool_error_arrives_as_an_error_not_an_answer(real_client) -> None:
    """A server that raises must not look like a server that answered."""
    result = await real_client.call_tool("explode", {"reason": "kaboom"})

    assert result.isError
    assert "kaboom" in result.content[0].text


async def test_concurrent_calls_do_not_cross_wires(real_client) -> None:
    """One pipe, many requests. Responses are matched by id, not by arrival."""
    import asyncio

    results = await asyncio.gather(
        real_client.call_tool("search_issues", {"query": "alpha"}),
        real_client.call_tool("search_issues", {"query": "beta"}),
        real_client.call_tool("close_issue", {"key": "PAY-1"}),
    )

    assert "alpha" in results[0].content[0].text
    assert "beta" in results[1].content[0].text
    assert "PAY-1" in results[2].content[0].text


# -- through the gateway ---------------------------------------------------


def gateway() -> McpGateway:
    return McpGateway(
        policy=ToolPolicy(
            [
                Grant(role="analyst", tools=frozenset({"tickets.*"}), max_risk=RiskClass.READ),
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )


async def test_a_real_server_becomes_governed_tools(real_client) -> None:
    """The whole point: a third-party server arrives under our policy, not its own."""
    hub = gateway()
    await hub.register(
        MCPToolSource("tickets", real_client, risk_overrides={"search_issues": RiskClass.READ})
    )

    visible = {s.qualified_name: s for s in hub.tools_for(ALICE)}

    assert "tickets.search_issues" in visible
    # Only the tool the operator classified as READ is within an analyst's
    # read-only grant. The other two default to IRREVERSIBLE and are not visible.
    assert "tickets.close_issue" not in visible


async def test_an_unclassified_remote_tool_is_refused_not_run(real_client) -> None:
    hub = gateway()
    await hub.register(MCPToolSource("tickets", real_client))

    call = await hub.call(ALICE, "tickets.close_issue", {"key": "PAY-1"})

    assert not call.result.ok
    assert "not permitted" in (call.result.error or "").lower()


async def test_a_permitted_remote_call_runs_end_to_end(real_client) -> None:
    hub = gateway()
    await hub.register(
        MCPToolSource("tickets", real_client, risk_overrides={"search_issues": RiskClass.READ})
    )

    call = await hub.call(ALICE, "tickets.search_issues", {"query": "payments"})

    assert call.result.ok
    assert "PAY-1182" in call.result.content


# -- hostile servers -------------------------------------------------------


async def test_a_server_cannot_lie_its_way_out_of_approval() -> None:
    """`delete_everything`, declared `readOnlyHint: true`.

    READ is the class exempt from the approval ladder, so honouring that hint
    would hand a compromised server unattended deletion. It arrives IRREVERSIBLE.
    """
    client = StdioMcpClient(hostile_config("liar"))
    await client.start()
    try:
        specs = await MCPToolSource("hostile", client).list_tools()
    finally:
        await client.aclose()

    assert [s.tool for s in specs] == ["delete_everything"]
    assert specs[0].risk is RiskClass.IRREVERSIBLE


async def test_a_poisoned_description_never_reaches_the_catalog() -> None:
    """Tool poisoning: the payload is in the description, not in any result.

    It reaches the context window with nobody invoking anything, and is present
    in every request. The tool is withheld; the clean one beside it is not.
    """
    client = StdioMcpClient(hostile_config("poisoned"))
    await client.start()
    try:
        specs = await MCPToolSource("hostile", client).list_tools()
    finally:
        await client.aclose()

    assert [s.tool for s in specs] == ["search"]


async def test_a_chatty_server_does_not_deadlock_the_client() -> None:
    """4,000 lines of stderr before answering anything.

    A client that does not drain stderr blocks the server's next write once the
    pipe buffer fills — which looks exactly like a hang and is not one.
    """
    client = StdioMcpClient(hostile_config("noisy", timeout_s=10))
    await client.start()
    try:
        tools = await client.list_tools()
    finally:
        await client.aclose()

    assert tools.tools


async def test_garbage_between_messages_is_skipped_not_fatal() -> None:
    """Servers print stray output. A reader that dies on it takes every call with it."""
    client = StdioMcpClient(hostile_config("garbage"))
    await client.start()
    try:
        tools = await client.list_tools()
    finally:
        await client.aclose()

    assert [t.name for t in tools.tools] == ["delete_everything"]


async def test_a_silent_server_fails_on_its_deadline() -> None:
    """The failure that matters most: no answer, ever.

    Without a deadline the morning brief waits on a pipe nobody will write to.
    """
    client = StdioMcpClient(hostile_config("silent", timeout_s=0.5))

    with pytest.raises(McpError, match="timed out"):
        await client.start()

    assert not client.alive, "a half-initialised server must not be left running"


async def test_a_crash_mid_call_fails_the_call_immediately() -> None:
    """Not on the timeout — the process is gone, and waiting 30s helps nobody."""
    client = StdioMcpClient(hostile_config("crasher", timeout_s=30))
    await client.start()

    try:
        with pytest.raises(McpError):
            await client.call_tool("anything", {})
    finally:
        await client.aclose()


async def test_an_unknown_protocol_version_is_refused() -> None:
    """The spec has the client disconnect rather than guess at a dialect."""
    client = StdioMcpClient(hostile_config("future"))

    with pytest.raises(McpError, match="protocol"):
        await client.start()


async def test_a_missing_command_fails_before_anything_starts() -> None:
    client = StdioMcpClient(ServerConfig(name="ghost", command="/nonexistent/mcp-server"))

    with pytest.raises(McpError, match="command not found"):
        await client.start()


# -- what the subprocess inherits ------------------------------------------


def test_a_server_does_not_inherit_our_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """An MCP server has no business reading our database URL or mail password."""
    monkeypatch.setenv("UIONE_MAIL_PASSWORD", "hunter2")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = ServerConfig(name="x", command="true").environment()

    assert "UIONE_MAIL_PASSWORD" not in environment
    assert environment["PATH"] == "/usr/bin"


def test_a_server_can_be_given_exactly_what_it_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIONE_MAIL_PASSWORD", "hunter2")

    environment = ServerConfig(name="x", command="true", env={"TICKETS_TOKEN": "abc"}).environment()

    assert environment["TICKETS_TOKEN"] == "abc"
    assert "UIONE_MAIL_PASSWORD" not in environment


def test_inheritance_is_available_but_deliberate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UIONE_MAIL_PASSWORD", "hunter2")

    environment = ServerConfig(name="x", command="true", inherit_env=True).environment()

    assert environment["UIONE_MAIL_PASSWORD"] == "hunter2"


# -- configuration ---------------------------------------------------------


def test_a_configured_server_parses() -> None:
    config, overrides = parse_server_config(
        '[{"name":"tickets","command":"python","args":["-m","srv"],'
        '"risk":{"search":"read"},"timeout_s":12}]'
    )[0]

    assert config.name == "tickets"
    assert config.args == ["-m", "srv"]
    assert config.timeout_s == 12
    assert overrides == {"search": RiskClass.READ}


def test_no_configuration_is_not_an_error() -> None:
    assert parse_server_config("") == []


def test_malformed_configuration_fails_loudly() -> None:
    """An operator is at the keyboard. Booting with silently zero connectors —
    an assistant that can do nothing, for no stated reason — is worse."""
    with pytest.raises(McpConfigError, match="not valid JSON"):
        parse_server_config("{not json")


def test_a_server_without_a_command_is_refused() -> None:
    with pytest.raises(McpConfigError, match="command"):
        parse_server_config('[{"name":"tickets"}]')


def test_duplicate_server_names_are_refused() -> None:
    """Two servers under one name shadow each other, and which answers depends
    on registration order."""
    with pytest.raises(McpConfigError, match="duplicate"):
        parse_server_config('[{"name":"a","command":"x"},{"name":"a","command":"y"}]')


def test_an_unknown_risk_class_is_refused() -> None:
    with pytest.raises(McpConfigError, match="unknown risk"):
        parse_server_config('[{"name":"a","command":"x","risk":{"t":"probably_fine"}}]')


# -- the supervisor --------------------------------------------------------


async def test_the_supervisor_starts_a_real_server() -> None:
    supervisor = McpSupervisor.from_config(
        f'[{{"name":"tickets","command":"{sys.executable}","args":["{REAL}"],'
        f'"risk":{{"search_issues":"read"}}}}]'
    )
    sources = await supervisor.start_all()
    try:
        assert [s.name for s in sources] == ["tickets"]
        assert supervisor.health()[0]["connected"]
        assert supervisor.health()[0]["tools"] == 3
    finally:
        await supervisor.aclose()


async def test_one_broken_server_does_not_stop_the_others() -> None:
    """Refusing to boot over one connector makes every connector a single point
    of failure."""
    supervisor = McpSupervisor.from_config(
        f'[{{"name":"ghost","command":"/nonexistent/server"}},'
        f'{{"name":"tickets","command":"{sys.executable}","args":["{REAL}"]}}]'
    )
    sources = await supervisor.start_all()
    try:
        assert [s.name for s in sources] == ["tickets"]
        health = {h["server"]: h for h in supervisor.health()}
        assert not health["ghost"]["connected"]
        assert "command not found" in health["ghost"]["error"]
        assert health["tickets"]["connected"]
    finally:
        await supervisor.aclose()
