"""Streaming: the model plane, the agent loop, and the SSE endpoint.

Three things here are worth more than the rest.

Tool-call arguments arrive fragmented on some engines and whole on others, and
code written against the wrong one fails only on the engine a customer runs.

A held action must be visible in the stream, because an action waiting for
approval is not a failure and a UI that shows it as one teaches people to
distrust the approval queue.

And `done` must be the completion signal, because without it a dropped
connection produces a half-answer that looks finished.
"""

from __future__ import annotations

import json

import httpx
import pytest

from uione.agent import AgentRuntime
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
from uione.modelplane import ModelPlaneClient, ModelPlaneError
from uione.modelplane.types import ToolCall

ALICE = Principal(user_id="alice", roles=frozenset({"analyst"}))


def sse(frames: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    return (body + "data: [DONE]\n\n").encode()


def client_streaming(frames: list[dict]) -> ModelPlaneClient:
    """A model plane whose engine returns these frames."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(frames))

    client = ModelPlaneClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def content_frame(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


# -- the model plane -------------------------------------------------------


async def test_content_arrives_in_order() -> None:
    model = client_streaming([content_frame("Hello "), content_frame("world")])

    chunks = [v for kind, v in [e async for e in model.stream([])] if kind == "content"]

    assert chunks == ["Hello ", "world"]


async def test_the_stream_ends_with_the_assembled_completion() -> None:
    model = client_streaming([content_frame("a"), content_frame("b")])

    events = [e async for e in model.stream([])]

    kind, completion = events[-1]
    assert kind == "completion"
    assert completion.content == "ab"


async def test_a_tool_call_delivered_whole_is_read(monkeypatch) -> None:
    """Ollama's behaviour: one delta, complete arguments."""
    model = client_streaming(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {
                                        "name": "mail_list_unread",
                                        "arguments": '{"limit": 5}',
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    )

    _, completion = [e async for e in model.stream([])][-1]

    assert [(c.name, c.arguments) for c in completion.tool_calls] == [
        ("mail_list_unread", '{"limit": 5}')
    ]


async def test_a_tool_call_fragmented_across_deltas_is_reassembled() -> None:
    """vLLM and llama.cpp fragment the argument JSON. Code written against
    Ollama parses `{"lim` and fails on the engine a customer actually runs —
    and this product targets all three.
    """
    model = client_streaming(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "mail_list"}}
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"lim'}}]},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": 'it": 5}'}}]
                        },
                    }
                ]
            },
        ]
    )

    _, completion = [e async for e in model.stream([])][-1]

    assert completion.tool_calls[0].arguments == '{"limit": 5}'
    assert json.loads(completion.tool_calls[0].arguments) == {"limit": 5}


async def test_two_interleaved_tool_calls_stay_separate() -> None:
    model = client_streaming(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "a", "function": {"name": "first"}},
                                {"index": 1, "id": "b", "function": {"name": "second"}},
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": "{}"}},
                                {"index": 0, "function": {"arguments": "{}"}},
                            ]
                        },
                    }
                ]
            },
        ]
    )

    _, completion = [e async for e in model.stream([])][-1]

    assert [c.name for c in completion.tool_calls] == ["first", "second"]


async def test_a_malformed_frame_does_not_end_the_answer() -> None:
    """One bad frame from an engine under load must not truncate a reply."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"good"}}]}\n\n'
            "data: {not json\n\n"
            'data: {"choices":[{"delta":{"content":" still good"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    model = ModelPlaneClient()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    text = "".join(v for kind, v in [e async for e in model.stream([])] if kind == "content")

    assert text == "good still good"


async def test_keepalive_comments_are_ignored() -> None:
    """SSE uses ':' prefixed lines as keep-alives, which arrive from a busy
    engine."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = ': keep-alive\n\ndata: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode())

    model = ModelPlaneClient()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert [v for kind, v in [e async for e in model.stream([])] if kind == "content"] == ["x"]


async def test_an_engine_error_raises_rather_than_ending_silently() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="engine exploded")

    model = ModelPlaneClient()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ModelPlaneError, match="500"):
        [e async for e in model.stream([])]


# -- the agent loop --------------------------------------------------------


class ScriptedModel:
    """Replays a list of turns, each a (content, tool_calls) pair."""

    def __init__(self, turns: list[tuple[str, list[ToolCall]]]) -> None:
        self.turns = turns
        self.calls = 0

    async def stream(self, messages, **kwargs):
        from uione.modelplane.types import Completion

        content, tool_calls = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        for word in content.split(" "):
            yield ("content", word + " ")
        yield ("completion", Completion(content=content, tool_calls=tool_calls, model="scripted"))


async def build_gateway(*, held: bool = False, fails: bool = False) -> McpGateway:
    """A gateway with one mail tool.

    With `held`, the tool is irreversible, the grant permits it, and a governor
    is present — which is what produces a *held* action rather than a denied
    one. Without the governor there is no approval ladder and the policy simply
    refuses, which is a different event and would not test what it claims to.
    """
    from uione.governance import Governor

    gateway = McpGateway(
        policy=ToolPolicy(
            [
                Grant(
                    role="analyst",
                    tools=frozenset({"mail.*"}),
                    max_risk=RiskClass.IRREVERSIBLE if held else RiskClass.READ,
                )
            ]
        ),
        audit=AuditLog(InMemoryAuditSink()),
        governor=Governor() if held else None,
    )
    source = InMemoryToolSource("mail")

    async def unread(_args: dict) -> ToolResult:
        if fails:
            return ToolResult.failure("mailbox unreachable")
        return ToolResult.success("2 unread", {"count": 2})

    source.register(
        "list_unread",
        unread,
        description="unread",
        risk=RiskClass.IRREVERSIBLE if held else RiskClass.READ,
    )
    await gateway.register(source)
    return gateway


def runtime_for(model, gateway) -> AgentRuntime:
    return AgentRuntime(model=model, gateway=gateway)


async def collect(runtime, message: str = "how many unread?") -> list[tuple[str, dict]]:
    return [event async for event in runtime.stream(ALICE, message)]


async def test_a_plain_answer_streams_tokens_then_finishes() -> None:
    runtime = runtime_for(ScriptedModel([("You have two.", [])]), await build_gateway())

    events = await collect(runtime)

    kinds = [k for k, _ in events]
    assert kinds[0] == "step"
    assert "token" in kinds
    assert kinds[-1] == "done"


async def test_the_final_answer_is_carried_on_done() -> None:
    """A client that missed a token must still end up with the whole reply."""
    runtime = runtime_for(ScriptedModel([("You have two.", [])]), await build_gateway())

    events = await collect(runtime)

    assert dict(events)["done"]["final"] == "You have two."


async def test_a_tool_call_announces_itself_before_it_runs() -> None:
    """A mailbox round trip dwarfs the time to write a sentence about it. A user
    watching "reading your mail…" learns more than one watching a spinner."""
    model = ScriptedModel(
        [
            ("Let me look.", [ToolCall(id="1", name="mail.list_unread", arguments="{}")]),
            ("You have two.", []),
        ]
    )
    runtime = runtime_for(model, await build_gateway())

    events = await collect(runtime)
    kinds = [k for k, _ in events]

    assert kinds.index("tool") < kinds.index("tool_result")
    assert dict(events)["tool"]["name"] == "mail.list_unread"


async def test_a_tool_result_reports_success() -> None:
    model = ScriptedModel(
        [
            ("Looking.", [ToolCall(id="1", name="mail.list_unread", arguments="{}")]),
            ("Two.", []),
        ]
    )

    events = await collect(runtime_for(model, await build_gateway()))

    result = next(payload for kind, payload in events if kind == "tool_result")
    assert result["ok"] is True
    assert result["held"] is False


async def test_a_held_action_is_visible_in_the_stream() -> None:
    """An action waiting for approval is not a failure, and a UI that shows it
    as one teaches people to distrust the approval queue."""
    model = ScriptedModel(
        [
            ("Sending.", [ToolCall(id="1", name="mail.list_unread", arguments="{}")]),
            ("Done.", []),
        ]
    )

    events = await collect(runtime_for(model, await build_gateway(held=True)))

    result = next(payload for kind, payload in events if kind == "tool_result")
    assert result["held"] is True


async def test_a_failing_tool_reports_why() -> None:
    model = ScriptedModel(
        [
            ("Looking.", [ToolCall(id="1", name="mail.list_unread", arguments="{}")]),
            ("Sorry.", []),
        ]
    )

    events = await collect(runtime_for(model, await build_gateway(fails=True)))

    result = next(payload for kind, payload in events if kind == "tool_result")
    assert result["ok"] is False
    assert "unreachable" in (result["error"] or "")


async def test_a_dead_model_plane_emits_an_error_not_silence() -> None:
    class DeadModel:
        async def stream(self, messages, **kwargs):
            raise ModelPlaneError("engine down")
            yield  # pragma: no cover — makes this an async generator

    events = await collect(runtime_for(DeadModel(), await build_gateway()))

    assert events[-1][0] == "error"
    assert "engine down" in events[-1][1]["message"]


async def test_the_step_budget_ends_the_stream_with_done() -> None:
    """Even exhaustion is a completion signal — a client must never be left
    waiting for an event that is not coming."""
    model = ScriptedModel([("Again.", [ToolCall(id="1", name="mail.list_unread", arguments="{}")])])

    events = [
        e async for e in runtime_for(model, await build_gateway()).stream(ALICE, "x", max_steps=2)
    ]

    assert events[-1][0] == "done"
    assert events[-1][1]["reason"] == "step_budget_exhausted"


# -- the SSE endpoint ------------------------------------------------------


def test_frames_are_separated_by_a_blank_line() -> None:
    """A client splitting on the separator must not lose the trailing frame."""
    from uione.api.routes.assistant import _sse

    frame = _sse("token", {"text": "hi"})

    assert frame == 'event: token\ndata: {"text": "hi"}\n\n'


def test_the_payload_is_json_on_one_line() -> None:
    """A newline inside a data field would end the frame early and truncate the
    reply at whatever the user happened to type."""
    from uione.api.routes.assistant import _sse

    frame = _sse("token", {"text": "line one\nline two"})

    assert frame.count("\n\n") == 1
    assert json.loads(frame.split("data: ")[1].strip())["text"] == "line one\nline two"
