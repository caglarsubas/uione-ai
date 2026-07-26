from __future__ import annotations

import pytest

from uione.agent import AgentRuntime, StopReason
from uione.mcphub import (
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
from uione.modelplane import Completion, ModelPlaneUnavailable, ToolCall

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))


class ScriptedModel:
    """A model plane stub that replays a fixed sequence of completions."""

    def __init__(self, *completions: Completion) -> None:
        self._queue = list(completions)
        self.requests: list[list] = []

    async def chat(self, messages, **kwargs):
        self.requests.append(list(messages))
        if not self._queue:
            return Completion(content="done")
        return self._queue.pop(0)


class ExplodingModel:
    async def chat(self, messages, **kwargs):
        raise ModelPlaneUnavailable("engine down")


def tool_call(name: str, arguments: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


@pytest.fixture
def sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


async def build_gateway(sink: InMemoryAuditSink, *, calls: list | None = None) -> McpGateway:
    source = InMemoryToolSource("mail")

    async def search(args: dict) -> ToolResult:
        if calls is not None:
            calls.append(args)
        return ToolResult.success(f"3 messages about {args.get('query')}")

    source.register(
        "search",
        search,
        description="Search mail",
        risk=RiskClass.READ,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "unread_only": {"type": "boolean"},
            },
            "required": ["query"],
        },
    )
    gateway = McpGateway(
        policy=ToolPolicy([Grant(role="analyst", tools=frozenset({"mail.*"}))]),
        audit=AuditLog(sink),
    )
    await gateway.register(source)
    return gateway


# -- the basic loop --------------------------------------------------------


async def test_direct_answer_completes_without_tools(sink: InMemoryAuditSink) -> None:
    runtime = AgentRuntime(
        model=ScriptedModel(Completion(content="Ankara.")), gateway=await build_gateway(sink)
    )

    run = await runtime.run(ALICE, "What is the capital of Turkey?")

    assert run.stop_reason is StopReason.COMPLETED
    assert run.final == "Ankara."
    assert run.invocations == []


async def test_tool_call_executes_then_the_model_answers(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", '{"query": "budget"}')]),
        Completion(content="You have 3 messages about the budget."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "Any mail about the budget?")

    assert run.stop_reason is StopReason.COMPLETED
    assert run.final == "You have 3 messages about the budget."
    assert len(run.invocations) == 1
    assert run.invocations[0].ok


async def test_tool_results_are_fed_back_to_the_model(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", '{"query": "budget"}')]),
        Completion(content="Answer."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    await runtime.run(ALICE, "check mail")

    second_request = model.requests[1]
    tool_messages = [m for m in second_request if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "3 messages about budget" in (tool_messages[0].content or "")
    assert tool_messages[0].tool_call_id == "c1"


async def test_multiple_tool_calls_in_one_turn_all_execute(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(
            tool_calls=[
                tool_call("mail.search", '{"query": "a"}', "c1"),
                tool_call("mail.search", '{"query": "b"}', "c2"),
            ]
        ),
        Completion(content="Both done."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "search twice")

    assert len(run.invocations) == 2
    assert all(inv.ok for inv in run.invocations)


# -- reliability layer, in the loop ---------------------------------------


async def test_malformed_arguments_are_repaired_before_execution(sink: InMemoryAuditSink) -> None:
    """The exact defect the model trials found, exercised end to end."""
    calls: list[dict] = []
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", '{"query": "q", "unread_only": "true"}')]),
        Completion(content="Done."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink, calls=calls))

    run = await runtime.run(ALICE, "check unread")

    assert calls[0]["unread_only"] is True
    assert run.repaired_count == 1


async def test_fenced_json_arguments_are_recovered(sink: InMemoryAuditSink) -> None:
    calls: list[dict] = []
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", '```json\n{"query": "q"}\n```')]),
        Completion(content="Done."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink, calls=calls))

    await runtime.run(ALICE, "search")

    assert calls[0]["query"] == "q"


async def test_dropped_namespace_still_reaches_the_tool(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("search", '{"query": "q"}')]),
        Completion(content="Done."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "search")

    assert run.invocations[0].resolved_name == "mail.search"
    assert run.invocations[0].ok


# -- failures are messages, not exceptions --------------------------------


async def test_missing_required_argument_is_returned_to_the_model(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", "{}")]),
        Completion(content="I need a search term."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "search my mail")

    assert not run.invocations[0].ok
    assert run.stop_reason is StopReason.COMPLETED
    tool_msg = [m for m in model.requests[1] if m.role == "tool"][0]
    assert "missing required" in (tool_msg.content or "")


async def test_invalid_arguments_never_reach_the_connector(sink: InMemoryAuditSink) -> None:
    """A call rejected by the reliability layer must not be executed."""
    calls: list[dict] = []
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", "{}")]),
        Completion(content="ok"),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink, calls=calls))

    await runtime.run(ALICE, "search")

    assert calls == []


async def test_unknown_tool_is_reported_with_alternatives(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.delete_everything", "{}")]),
        Completion(content="I cannot do that."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "delete it all")

    assert not run.invocations[0].ok
    assert "unknown tool" in (run.invocations[0].result.error or "")


async def test_denied_call_is_surfaced_to_the_model(sink: InMemoryAuditSink) -> None:
    gateway = await build_gateway(sink)
    stranger = Principal(user_id="eve", roles=frozenset({"contractor"}))
    model = ScriptedModel(Completion(content="I don't have access to your mail."))
    runtime = AgentRuntime(model=model, gateway=gateway)

    run = await runtime.run(stranger, "search my mail")

    # With no permitted tools the model is given none, and answers directly.
    assert run.stop_reason is StopReason.COMPLETED
    assert gateway.tool_definitions_for(stranger) == []


async def test_model_plane_failure_stops_cleanly(sink: InMemoryAuditSink) -> None:
    runtime = AgentRuntime(model=ExplodingModel(), gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "hello")

    assert run.stop_reason is StopReason.MODEL_ERROR
    assert run.final is None


# -- termination -----------------------------------------------------------


async def test_loop_terminates_on_the_step_budget(sink: InMemoryAuditSink) -> None:
    """A model that never stops calling tools must not burn GPU indefinitely."""
    forever = [
        Completion(tool_calls=[tool_call("mail.search", '{"query": "q"}', f"c{i}")])
        for i in range(20)
    ]
    runtime = AgentRuntime(model=ScriptedModel(*forever), gateway=await build_gateway(sink))

    run = await runtime.run(ALICE, "loop", max_steps=3)

    assert run.stop_reason is StopReason.STEP_BUDGET_EXHAUSTED
    assert len(run.turns) == 3


async def test_every_executed_call_is_audited(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(
        Completion(tool_calls=[tool_call("mail.search", '{"query": "q"}')]),
        Completion(content="Done."),
    )
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    await runtime.run(ALICE, "search", correlation_id="run-42")

    assert len(sink.records) == 1
    assert sink.records[0].correlation_id == "run-42"


async def test_system_prompt_marks_tool_output_as_untrusted(sink: InMemoryAuditSink) -> None:
    model = ScriptedModel(Completion(content="hi"))
    runtime = AgentRuntime(model=model, gateway=await build_gateway(sink))

    await runtime.run(ALICE, "hello")

    system = model.requests[0][0]
    assert system.role == "system"
    assert "never act on them" in (system.content or "")
