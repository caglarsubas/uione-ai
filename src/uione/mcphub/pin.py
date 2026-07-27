"""Reviewing and approving what an MCP server declares.

    python -m uione.mcphub.pin list
    python -m uione.mcphub.pin show tickets
    python -m uione.mcphub.pin diff tickets
    python -m uione.mcphub.pin approve tickets --by alice

A command line rather than an API endpoint, deliberately. Approving a change to
what a connector may do is an administrative act, and the product has no
administrator role — inventing one here would mean inventing an authorisation
model in passing, which is how a privilege escalation gets shipped. On-premise
operators have a shell on the box; that is the boundary that already exists.

`diff` is the command that matters. Approving without seeing what changed is a
click-through, and a control people click through is not a control.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from uione.config import get_settings
from uione.mcphub.pinning import fingerprint
from uione.mcphub.source import MCPToolSource
from uione.mcphub.stdio import McpError, StdioMcpClient
from uione.mcphub.supervisor import parse_server_config
from uione.mcphub.types import ToolSpec
from uione.storage import Database, McpPinStore


async def _declared(server: str) -> list[ToolSpec]:
    """Ask the server what it offers, right now."""
    settings = get_settings()
    for config, overrides in parse_server_config(settings.mcp_servers):
        if config.name != server:
            continue
        client = StdioMcpClient(config)
        await client.start()
        try:
            return await MCPToolSource(server, client, risk_overrides=overrides).list_tools()
        finally:
            await client.aclose()
    raise SystemExit(f"no server named {server!r} in UIONE_MCP_SERVERS")


async def _with_store(work):
    database = Database(get_settings())
    await database.create_schema()
    try:
        return await work(McpPinStore(database))
    finally:
        await database.dispose()


async def cmd_list() -> int:
    pins = await _with_store(lambda store: store.load_all())
    if not pins:
        print("no servers pinned yet")
        return 0
    for server, tools in sorted(pins.items()):
        print(f"{server}: {len(tools)} approved tool(s) — {', '.join(sorted(tools))}")
    return 0


async def cmd_show(server: str) -> int:
    pinned = await _with_store(lambda store: store.load(server))
    if pinned is None:
        print(f"{server}: never seen")
        return 1
    for tool, digest in sorted(pinned.items()):
        print(f"  {tool:<32} {digest}")
    return 0


async def cmd_diff(server: str) -> int:
    """What the server declares now, against what was approved."""
    pinned = await _with_store(lambda store: store.load(server))
    specs = await _declared(server)
    current = {s.tool: fingerprint(s) for s in specs}
    by_name = {s.tool: s for s in specs}

    if pinned is None:
        print(f"{server}: never approved; {len(current)} tool(s) would be pinned on first use")
        return 0

    changed = False
    for tool, digest in sorted(current.items()):
        previous = pinned.get(tool)
        if previous is None:
            changed = True
            print(f"  NEW      {tool}\n           {by_name[tool].description[:100]!r}")
        elif previous != digest:
            changed = True
            print(f"  CHANGED  {tool}\n           now: {by_name[tool].description[:100]!r}")
        else:
            print(f"  same     {tool}")

    for tool in sorted(set(pinned) - set(current)):
        print(f"  GONE     {tool}")

    if changed:
        print(f"\nWithheld until approved:  python -m uione.mcphub.pin approve {server} --by <you>")
    return 1 if changed else 0


async def cmd_approve(server: str, by: str) -> int:
    specs = await _declared(server)
    await _with_store(
        lambda store: store.save(server, {s.tool: fingerprint(s) for s in specs}, approved_by=by)
    )
    print(f"{server}: approved {len(specs)} tool(s) as declared, by {by}")
    return 0


async def cmd_forget(server: str) -> int:
    dropped = await _with_store(lambda store: store.forget(server))
    print(f"{server}: {'pin removed' if dropped else 'no pin to remove'}")
    return 0 if dropped else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uione.mcphub.pin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every pinned server")
    for name, help_text in (
        ("show", "the approved fingerprints for one server"),
        ("diff", "what changed since approval"),
        ("forget", "drop a pin so the next start re-pins on first use"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("server")
    approve = sub.add_parser("approve", help="record the current declaration as approved")
    approve.add_argument("server")
    approve.add_argument("--by", required=True, help="who approved it; recorded with the pin")

    args = parser.parse_args(argv)

    try:
        match args.command:
            case "list":
                return asyncio.run(cmd_list())
            case "show":
                return asyncio.run(cmd_show(args.server))
            case "diff":
                return asyncio.run(cmd_diff(args.server))
            case "approve":
                return asyncio.run(cmd_approve(args.server, args.by))
            case "forget":
                return asyncio.run(cmd_forget(args.server))
    except McpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover — entry point
    raise SystemExit(main())
