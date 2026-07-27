"""A real MCP client, over stdio.

Until now the hub spoke to in-process Python objects and to a duck-typed
"session" that a caller supplied. That was enough to build governance against,
but it left the product's central claim — *your platforms already have MCP
servers, we connect to them* — unproven. This is the client that makes it true:
JSON-RPC 2.0 over a subprocess's stdin and stdout, the `initialize` handshake,
`tools/list`, `tools/call`.

Written rather than taken from the official SDK on purpose. An on-premise product
ships what it depends on, and the transport to an *untrusted third-party process*
is exactly the boundary worth owning. The SDK is used in the tests, as the server
on the other end, so the wire format is validated against an implementation we
did not write.

**Four things this file treats as requirements, not error handling:**

*The server is not trusted.* It is a third-party process. Everything it says
about itself — tool names, descriptions, risk hints — is input, not fact.

*A dead server must not hang the assistant.* Every request has a deadline, and a
process that dies fails its pending calls immediately rather than leaving the
brief waiting on a pipe nobody will write to.

*stderr is drained and logged, never parsed.* MCP servers log there. A client
that lets stderr fill its pipe buffer will deadlock a chatty server, and one that
reads it as protocol will misparse a log line as a message.

*A malformed line is not fatal.* One unparseable message from a buggy server must
not take down a connector, let alone the loop reading it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import structlog

from uione import __version__

log = structlog.get_logger(__name__)

#: Protocol revisions this client understands. The spec has a client disconnect
#: when the server answers with a version it cannot support — so an unknown
#: version is a refusal to register the server, loudly, rather than a hopeful
#: attempt to speak it anyway.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

#: A description longer than this is truncated before the model ever sees it. A
#: tool description is attacker-controllable text with a direct path into the
#: context window; there is no legitimate 20,000-character description.
MAX_DESCRIPTION_CHARS = 1024


class McpError(RuntimeError):
    """A protocol-level or transport-level failure."""


@dataclass
class ServerConfig:
    """How to launch one MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    #: Per-request deadline. Generous enough for a slow ticket system, short
    #: enough that a hung server does not hold up a morning brief.
    timeout_s: float = 30.0

    #: Whether the environment is passed through. Off by default: a subprocess
    #: inheriting our environment inherits database URLs, OIDC secrets and mail
    #: passwords, and an MCP server has no business reading any of them.
    inherit_env: bool = False

    def environment(self) -> dict[str, str]:
        base = dict(os.environ) if self.inherit_env else _minimal_env()
        base.update(self.env)
        return base


def _minimal_env() -> dict[str, str]:
    """The little a subprocess genuinely needs.

    PATH so the interpreter can be found, HOME because some runtimes refuse to
    start without it, and the locale so text decoding matches ours.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")
    return {k: v for k in keep if (v := os.environ.get(k)) is not None}


# -- the shapes the rest of the hub already expects ------------------------
#
# `MCPToolSource` was written against a session shape rather than a class, so
# these mirror the official SDK's result objects. Two independent
# implementations now satisfy the same adapter, which is the point.


@dataclass
class RemoteTool:
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=dict)  # noqa: N815 — wire name
    annotations: Any | None = None


@dataclass
class ToolsResult:
    tools: list[RemoteTool]


@dataclass
class ContentBlock:
    type: str
    text: str = ""


@dataclass
class CallResult:
    content: list[ContentBlock]
    isError: bool = False  # noqa: N815 — wire name
    structuredContent: dict[str, Any] | None = None  # noqa: N815 — wire name


@dataclass
class Annotations:
    """A server's own hints about a tool.

    Kept as a plain object rather than trusted values: `classify_risk` decides
    what, if anything, they are allowed to change.
    """

    readOnlyHint: bool | None = None  # noqa: N815
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815

    @classmethod
    def parse(cls, raw: Any) -> Annotations | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            readOnlyHint=raw.get("readOnlyHint"),
            destructiveHint=raw.get("destructiveHint"),
            idempotentHint=raw.get("idempotentHint"),
            openWorldHint=raw.get("openWorldHint"),
        )


class StdioMcpClient:
    """One MCP server, running as a subprocess."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str = ""
        self.last_error: str = ""

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Launch the server and complete the handshake."""
        if shutil.which(self.config.command) is None and not os.path.exists(self.config.command):
            raise McpError(f"command not found: {self.config.command!r}")

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.config.environment(),
            cwd=self.config.cwd,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())

        try:
            return await self._handshake()
        except Exception:
            # A half-initialised server is worse than none: it would sit in the
            # catalog answering nothing.
            await self.aclose()
            raise

    async def _handshake(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PREFERRED_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "uione", "version": __version__},
            },
        )
        version = result.get("protocolVersion", "")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpError(
                f"server {self.name!r} speaks protocol {version!r}; "
                f"this client supports {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
            )

        self.protocol_version = version
        self.server_info = result.get("serverInfo", {})
        await self._notify("notifications/initialized", {})
        log.info(
            "mcp.connected",
            server=self.name,
            protocol=version,
            implementation=self.server_info.get("name", "unknown"),
        )
        return result

    async def aclose(self) -> None:
        """Shut the server down, and do not wait forever for it to agree."""
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader = self._stderr = None

        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return

        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            log.warning("mcp.kill", server=self.name)
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        self._fail_pending("server shut down")

    # -- the wire ----------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stdout = self._process.stdout
        while True:
            line = await stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # One bad line must not take down the connector. Servers do print
                # stray output, and a reader that dies on it takes every pending
                # call with it.
                log.warning(
                    "mcp.unparseable", server=self.name, line=line[:200].decode(errors="replace")
                )
                continue
            self._dispatch(message)

        # stdout closed: the server is gone.
        self._fail_pending("server exited")
        log.warning("mcp.disconnected", server=self.name)

    def _dispatch(self, message: dict) -> None:
        message_id = message.get("id")
        if message_id is None:
            # A notification or a request from the server. We advertise no
            # capabilities, so there is nothing to answer — logged rather than
            # silently dropped, because a server calling us is worth knowing.
            if method := message.get("method"):
                log.debug("mcp.server_message", server=self.name, method=method)
            return

        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            # A response to a request that already timed out. Dropping it is
            # correct; the caller has long since had its error.
            return

        if error := message.get("error"):
            future.set_exception(
                McpError(f"{error.get('message', 'error')} (code {error.get('code', '?')})")
            )
        else:
            future.set_result(message.get("result", {}))

    async def _drain_stderr(self) -> None:
        """Read stderr continuously so a chatty server cannot deadlock.

        A full pipe buffer blocks the server's next write, which for a server
        that logs each call means it stops answering after a few of them — a
        failure that looks like a hang and is nothing of the sort.
        """
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            log.debug("mcp.stderr", server=self.name, line=line.decode(errors="replace").rstrip())

    async def _send(self, payload: dict) -> None:
        if self._process is None or self._process.stdin is None or not self.alive:
            raise McpError(f"server {self.name!r} is not running")
        data = (json.dumps(payload) + "\n").encode()
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future

        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    **({"params": params} if params else {}),
                }
            )
            return await asyncio.wait_for(future, timeout=self.config.timeout_s)
        except TimeoutError as exc:
            self._pending.pop(message_id, None)
            self.last_error = f"timeout after {self.config.timeout_s}s"
            raise McpError(f"{method} timed out after {self.config.timeout_s}s") from exc
        finally:
            self._pending.pop(message_id, None)

    async def _notify(self, method: str, params: dict) -> None:
        await self._send(
            {"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}
        )

    def _fail_pending(self, reason: str) -> None:
        self.last_error = reason
        for future in self._pending.values():
            if not future.done():
                future.set_exception(McpError(reason))
        self._pending.clear()

    # -- the two operations the hub needs ----------------------------------

    async def list_tools(self) -> ToolsResult:
        result = await self._request("tools/list", {})
        tools = []
        for raw in result.get("tools", []):
            if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                log.warning("mcp.malformed_tool", server=self.name, raw=str(raw)[:200])
                continue
            tools.append(
                RemoteTool(
                    name=raw["name"],
                    description=_clip(raw.get("description") or ""),
                    inputSchema=raw.get("inputSchema") or {"type": "object", "properties": {}},
                    annotations=Annotations.parse(raw.get("annotations")),
                )
            )
        return ToolsResult(tools=tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallResult:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        blocks = [
            ContentBlock(type=str(b.get("type", "text")), text=str(b.get("text", "")))
            for b in result.get("content", [])
            if isinstance(b, dict)
        ]
        return CallResult(
            content=blocks,
            isError=bool(result.get("isError", False)),
            structuredContent=result.get("structuredContent"),
        )


def _clip(text: str) -> str:
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    log.warning("mcp.description_truncated", chars=len(text))
    return text[:MAX_DESCRIPTION_CHARS] + " […truncated]"
