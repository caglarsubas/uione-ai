"""Running the configured MCP servers.

One place that owns subprocess lifecycles, so the gateway keeps talking to
`ToolSource` and knows nothing about processes.

Two judgements are encoded here, and they point in opposite directions on
purpose:

**A broken server is not an outage.** If the ticket system's MCP server fails to
start, mail and calendar still work and the assistant says which system is
unavailable. Refusing to boot the whole product over one connector would turn
every connector into a single point of failure.

**A broken *configuration* is a startup failure.** A malformed server list is an
operator error made seconds ago, at a keyboard, with the logs in front of them.
Starting with silently zero connectors — an assistant that can do nothing, for no
stated reason — is far worse than exiting with the parse error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from uione.mcphub.source import MCPToolSource
from uione.mcphub.stdio import McpError, ServerConfig, StdioMcpClient
from uione.mcphub.types import RiskClass

log = structlog.get_logger(__name__)


class McpConfigError(ValueError):
    """The configured server list could not be understood."""


@dataclass
class ServerStatus:
    name: str
    connected: bool = False
    protocol: str = ""
    implementation: str = ""
    tools: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "server": self.name,
            "connected": self.connected,
            "protocol": self.protocol,
            "implementation": self.implementation,
            "tools": self.tools,
            "error": self.error,
        }


def parse_server_config(raw: str) -> list[tuple[ServerConfig, dict[str, RiskClass]]]:
    """Read the configured server list.

    Shape, as JSON:

    ```json
    [{"name": "tickets",
      "command": "/usr/bin/python3",
      "args": ["-m", "corp_tickets_mcp"],
      "env": {"TICKETS_URL": "https://tickets.corp.example"},
      "timeout_s": 30,
      "risk": {"search_issues": "read", "close_issue": "reversible_write"}}]
    ```

    The `risk` mapping is the *only* way a tool becomes `read` — a server's own
    annotations cannot lower its risk. Writing that mapping is the moment an
    operator looks at each tool and decides, which is the point.
    """
    if not raw.strip():
        return []

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"UIONE_MCP_SERVERS is not valid JSON: {exc}") from exc

    if not isinstance(entries, list):
        raise McpConfigError("UIONE_MCP_SERVERS must be a JSON list of server objects")

    parsed: list[tuple[ServerConfig, dict[str, RiskClass]]] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise McpConfigError(f"server #{index} is not an object")
        name = entry.get("name")
        command = entry.get("command")
        if not name or not command:
            raise McpConfigError(f"server #{index} needs both 'name' and 'command'")
        if name in seen:
            # Two servers under one name would shadow each other in the catalog,
            # and which one answered would depend on registration order.
            raise McpConfigError(f"duplicate server name {name!r}")
        seen.add(name)

        parsed.append(
            (
                ServerConfig(
                    name=str(name),
                    command=str(command),
                    args=[str(a) for a in entry.get("args", [])],
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    cwd=entry.get("cwd"),
                    timeout_s=float(entry.get("timeout_s", 30.0)),
                    inherit_env=bool(entry.get("inherit_env", False)),
                ),
                _parse_risk(name, entry.get("risk") or {}),
            )
        )

    return parsed


def _parse_risk(server: str, raw: Any) -> dict[str, RiskClass]:
    if not isinstance(raw, dict):
        raise McpConfigError(f"server {server!r}: 'risk' must be an object")
    overrides: dict[str, RiskClass] = {}
    for tool, value in raw.items():
        try:
            overrides[str(tool)] = RiskClass(str(value))
        except ValueError as exc:
            allowed = ", ".join(r.value for r in RiskClass)
            raise McpConfigError(
                f"server {server!r}: unknown risk {value!r} for tool {tool!r} (expected {allowed})"
            ) from exc
    return overrides


@dataclass
class McpSupervisor:
    """Starts, holds and stops the configured servers."""

    servers: list[tuple[ServerConfig, dict[str, RiskClass]]] = field(default_factory=list)
    clients: list[StdioMcpClient] = field(default_factory=list)
    status: dict[str, ServerStatus] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: str) -> McpSupervisor:
        return cls(servers=parse_server_config(raw))

    async def start_all(self) -> list[MCPToolSource]:
        """Start every configured server; return sources for those that came up."""
        sources: list[MCPToolSource] = []

        for config, overrides in self.servers:
            status = ServerStatus(name=config.name)
            self.status[config.name] = status
            client = StdioMcpClient(config)
            try:
                await client.start()
            except (McpError, OSError) as exc:
                # One dead connector is not an outage. Recorded so the answer to
                # "why can't it see my tickets?" is in /system/health rather than
                # in someone's memory of the boot logs.
                status.error = f"{type(exc).__name__}: {exc}"
                log.error("mcp.start_failed", server=config.name, error=status.error)
                continue

            source = MCPToolSource(config.name, client, risk_overrides=overrides)
            try:
                status.tools = len(await source.list_tools())
            except Exception as exc:  # noqa: BLE001 — a server that connects but will not list
                status.error = f"{type(exc).__name__}: {exc}"
                log.error("mcp.list_failed", server=config.name, error=status.error)
                await client.aclose()
                continue

            status.connected = True
            status.protocol = client.protocol_version
            status.implementation = str(client.server_info.get("name", ""))
            self.clients.append(client)
            sources.append(source)

        return sources

    async def aclose(self) -> None:
        for client in self.clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — shutdown must reach every server
                log.warning("mcp.close_failed", server=client.name)
        self.clients = []

    def health(self) -> list[dict]:
        return [s.as_dict() for s in self.status.values()]
