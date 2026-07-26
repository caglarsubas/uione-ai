#!/usr/bin/env python3
"""Generate a Morning Brief against a real open-weight model.

The signature moment, end to end: gather from four connectors concurrently,
quarantine what came from outside the company, and have a local model write the
brief.

Usage:
    python scripts/demo_brief.py
    python scripts/demo_brief.py --model gemma4:26b
    python scripts/demo_brief.py --down incidents,tasks   # honest degradation
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uione.config import Settings  # noqa: E402
from uione.connectors.demo import build_all  # noqa: E402
from uione.governance import Governor  # noqa: E402
from uione.mcphub import (  # noqa: E402
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
)
from uione.modelplane import ModelPlaneClient  # noqa: E402
from uione.proactive import BriefGenerator  # noqa: E402

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="ministral-3:8b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--down", default="", help="Comma-separated connectors to fail.")
    args = parser.parse_args()

    failing = {name.strip() for name in args.down.split(",") if name.strip()}

    settings = Settings(
        model_plane_url=args.base_url,
        model_tier_reasoning=args.model,
        model_plane_timeout_s=600,
    )
    gateway = McpGateway(
        policy=ToolPolicy(
            [
                Grant(
                    role="analyst",
                    tools=frozenset({"mail.*", "tasks.*", "incidents.*", "calendar.*"}),
                    max_risk=RiskClass.READ,
                )
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
        governor=Governor(),
    )
    for source in build_all(failing=failing):
        await gateway.register(source)

    async with ModelPlaneClient(settings) as model:
        started = time.perf_counter()
        brief = await BriefGenerator(model=model, gateway=gateway).generate(ALICE)
        elapsed = time.perf_counter() - started

    print("=" * 78)
    print(brief.body)
    print("=" * 78)
    print(f"model        : {brief.model}   ({elapsed:.1f}s)")
    print(f"complete     : {brief.complete}")
    if brief.degraded_sources:
        print(f"UNAVAILABLE  : {', '.join(brief.degraded_sources)}")
    print(f"untrusted    : {brief.taint.summary()}")
    print("sections     :")
    for section in brief.sections:
        status = "ok" if section.ok else "FAILED"
        print(
            f"  {section.section:<10} {status:<7} {section.duration_ms:6.1f}ms  "
            f"{section.tool:<22} {section.error or ''}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
