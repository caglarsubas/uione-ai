#!/usr/bin/env python3
"""End-to-end agent trial against a real open-weight model.

Exercises the whole stack — model plane, reliability layer, gateway policy,
audit tap, agent loop — against fixture enterprise tools. Unit tests prove the
pieces; this proves they compose when a real model is driving.

Usage:
    python scripts/trial_agent.py --model ministral-3:8b
    python scripts/trial_agent.py --model gemma4:26b --scenario denied
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uione.agent import AgentRuntime  # noqa: E402
from uione.config import Settings  # noqa: E402
from uione.mcphub import (  # noqa: E402
    AuditLog,
    Grant,
    InMemoryAuditSink,
    InMemoryToolSource,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
    ToolResult,
)
from uione.modelplane import ModelPlaneClient  # noqa: E402

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}), display_name="Alice")

FIXTURE_MAIL = [
    {"from": "cfo@corp", "subject": "Q3 budget review moved to Thursday", "unread": True},
    {"from": "ops@corp", "subject": "Payment gateway latency spike overnight", "unread": True},
    {"from": "hr@corp", "subject": "Annual leave policy update", "unread": False},
]


def build_tools() -> InMemoryToolSource:
    source = InMemoryToolSource("mail")

    async def search(args: dict) -> ToolResult:
        query = str(args.get("query", "")).lower()
        unread_only = args.get("unread_only", False)
        # A string "true" reaching here unrepaired would silently filter nothing.
        if not isinstance(unread_only, bool):
            return ToolResult.failure(
                f"unread_only must be a boolean, got {type(unread_only).__name__}"
            )
        hits = [
            m
            for m in FIXTURE_MAIL
            if (not unread_only or m["unread"]) and query in m["subject"].lower()
        ]
        if not hits:
            return ToolResult.success("No matching messages.")
        return ToolResult.success("\n".join(f"- from {m['from']}: {m['subject']}" for m in hits))

    async def send(args: dict) -> ToolResult:
        return ToolResult.success(f"Sent to {args.get('to')}")

    source.register(
        "search",
        search,
        description="Search the user's mailbox.",
        risk=RiskClass.READ,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to match in the subject."},
                "unread_only": {"type": "boolean", "description": "Only unread messages."},
            },
            "required": ["query"],
        },
    )
    source.register(
        "send",
        send,
        description="Send an email on the user's behalf.",
        risk=RiskClass.EXTERNAL_FACING,
        parameters={
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
        },
    )
    return source


SCENARIOS = {
    "search": "Is there anything unread about the budget?",
    "denied": "Email the CFO at cfo@corp saying I'll be late — subject 'Running late'.",
    "restraint": "Thanks, that's everything for now.",
    "ambiguous": "Send it.",
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="ministral-3:8b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="search")
    parser.add_argument("--max-steps", type=int, default=4)
    args = parser.parse_args()

    settings = Settings(
        model_plane_url=args.base_url,
        model_tier_reasoning=args.model,
        model_tier_workhorse=args.model,
        model_plane_timeout_s=300,
    )

    sink = InMemoryAuditSink()
    gateway = McpGateway(
        # Read access only. Sending mail is deliberately NOT granted, so the
        # 'denied' scenario shows the governance boundary holding under a real model.
        policy=ToolPolicy(
            [Grant(role="analyst", tools=frozenset({"mail.*"}), max_risk=RiskClass.READ)]
        ),
        audit=AuditLog(sink),
    )
    await gateway.register(build_tools())

    prompt = SCENARIOS[args.scenario]
    print(f"Model     : {args.model}")
    print(f"Scenario  : {args.scenario}")
    print(f"User      : {prompt}")
    print(f"Tools     : {[d.name for d in gateway.tool_definitions_for(ALICE)]}")
    print("-" * 72)

    async with ModelPlaneClient(settings) as model:
        runtime = AgentRuntime(model=model, gateway=gateway)
        run = await runtime.run(ALICE, prompt, max_steps=args.max_steps)

    for i, turn in enumerate(run.turns, 1):
        print(f"\n[turn {i}] model: {turn.model}")
        if turn.content:
            print(f"  says : {turn.content.strip()[:300]}")
        for inv in turn.invocations:
            status = "ok" if inv.ok else "FAIL"
            print(f"  tool : {inv.requested_name} -> {inv.resolved_name} [{status}]")
            print(f"  args : {inv.arguments}")
            if inv.repairs:
                print(f"  fixed: {inv.repairs}")
            body = inv.result.content if inv.ok else inv.result.error
            print(f"  got  : {str(body).strip()[:200]}")

    print("\n" + "-" * 72)
    print(f"stop reason : {run.stop_reason}")
    print(f"final       : {(run.final or '').strip()[:400]}")
    print(f"repairs     : {run.repaired_count}")
    print("\naudit trail:")
    for record in sink.records:
        print(
            f"  {record.outcome:<14} {record.tool:<14} risk={record.risk:<16} "
            f"{record.duration_ms:6.1f}ms  args#{record.arguments_hash[:8]}"
        )
    if not sink.records:
        print("  (no tool calls reached the gateway)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
