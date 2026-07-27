"""A genuine MCP server, built with the official SDK.

The point of this file is that *we did not write the protocol side*. Our client
is validated against someone else's implementation of the same spec, which is
what makes the wire format claim mean anything. A hand-written server on both
ends would agree with itself about a misreading.

Run as a subprocess over stdio, exactly as a real connector would be.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("tickets")


@server.tool()
def search_issues(query: str, limit: int = 5) -> str:
    """Search the ticket system."""
    return f"2 issues matching {query!r} (limit {limit}): PAY-1182, PAY-1190"


@server.tool()
def close_issue(key: str) -> str:
    """Close an issue. Changes state."""
    return f"{key} moved to Done"


@server.tool()
def explode(reason: str = "boom") -> str:
    """Always fails, so error propagation can be tested."""
    raise RuntimeError(reason)


if __name__ == "__main__":
    print("tickets server starting", file=sys.stderr, flush=True)
    server.run()
