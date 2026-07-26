from __future__ import annotations

import httpx
import pytest
import respx

from uione.config import Settings
from uione.modelplane import (
    ChatMessage,
    ModelPlaneClient,
    ModelPlaneError,
    ModelPlaneTimeout,
    ModelPlaneUnavailable,
    TaskClass,
    ToolDefinition,
)

BASE = "http://engine.test/v1"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        model_plane_url=BASE,
        model_tier_triage="small-model",
        model_tier_workhorse="mid-model",
        model_tier_reasoning="big-model",
        model_plane_connect_timeout_s=0.1,
        model_plane_timeout_s=1.0,
    )


def _completion_body(**overrides):
    body = {
        "model": "mid-model",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    body.update(overrides)
    return body


@respx.mock
async def test_chat_returns_content_and_records_usage(settings: Settings) -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_body())
    )

    async with ModelPlaneClient(settings) as client:
        result = await client.chat([ChatMessage(role="user", content="hello")])

        assert result.content == "hi"
        assert result.usage.total_tokens == 15
        assert client.usage.calls == 1
        assert client.usage.by_model["mid-model"].total_tokens == 15


@respx.mock
async def test_task_class_selects_the_configured_tier(settings: Settings) -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_body())
    )

    async with ModelPlaneClient(settings) as client:
        await client.chat([ChatMessage(role="user", content="x")], task=TaskClass.TRIAGE)
        assert route.calls.last.request.content.decode().count("small-model") == 1

        await client.chat([ChatMessage(role="user", content="x")], task=TaskClass.REASONING)
        assert route.calls.last.request.content.decode().count("big-model") == 1


@respx.mock
async def test_explicit_model_overrides_the_tier(settings: Settings) -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_body())
    )

    async with ModelPlaneClient(settings) as client:
        await client.chat(
            [ChatMessage(role="user", content="x")],
            task=TaskClass.TRIAGE,
            model="pinned-model",
        )

    assert "pinned-model" in route.calls.last.request.content.decode()


@respx.mock
async def test_tool_calls_are_parsed(settings: Settings) -> None:
    body = _completion_body(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "search_mail",
                                "arguments": '{"query": "invoice"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=body))

    async with ModelPlaneClient(settings) as client:
        result = await client.chat(
            [ChatMessage(role="user", content="find it")],
            tools=[ToolDefinition(name="search_mail", description="search")],
        )

    assert result.requested_tools
    call = result.tool_calls[0]
    assert call.name == "search_mail"
    assert call.parsed_arguments() == {"query": "invoice"}


@respx.mock
async def test_tool_call_without_id_is_given_one(settings: Settings) -> None:
    """Some runtimes omit the id; the agent loop still needs to correlate results."""
    body = _completion_body(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=body))

    async with ModelPlaneClient(settings) as client:
        result = await client.chat([ChatMessage(role="user", content="x")])

    assert result.tool_calls[0].id == "call_0"


@respx.mock
async def test_legacy_function_call_dialect_is_normalised(settings: Settings) -> None:
    body = _completion_body(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "function_call": {"name": "legacy", "arguments": '{"a": 1}'},
                },
                "finish_reason": "function_call",
            }
        ]
    )
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, json=body))

    async with ModelPlaneClient(settings) as client:
        result = await client.chat([ChatMessage(role="user", content="x")])

    assert result.tool_calls[0].name == "legacy"


@respx.mock
async def test_server_errors_are_retried_then_surface(settings: Settings) -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(503, text="overloaded")
    )

    async with ModelPlaneClient(settings) as client:
        with pytest.raises(ModelPlaneUnavailable):
            await client.chat([ChatMessage(role="user", content="x")])

    assert route.call_count == 3


@respx.mock
async def test_transient_error_then_success(settings: Settings) -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(503, text="starting"),
            httpx.Response(200, json=_completion_body()),
        ]
    )

    async with ModelPlaneClient(settings) as client:
        result = await client.chat([ChatMessage(role="user", content="x")])

    assert result.content == "hi"


@respx.mock
async def test_client_errors_are_not_retried(settings: Settings) -> None:
    """A 400 will fail identically on retry; burning the budget helps nobody."""
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(400, text="bad model")
    )

    async with ModelPlaneClient(settings) as client:
        with pytest.raises(ModelPlaneError):
            await client.chat([ChatMessage(role="user", content="x")])

    assert route.call_count == 1


@respx.mock
async def test_timeouts_are_typed(settings: Settings) -> None:
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))

    async with ModelPlaneClient(settings) as client:
        with pytest.raises(ModelPlaneTimeout):
            await client.chat([ChatMessage(role="user", content="x")])


@respx.mock
async def test_embeddings_are_returned_in_index_order(settings: Settings) -> None:
    respx.post(f"{BASE}/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )
    )

    async with ModelPlaneClient(settings) as client:
        vectors = await client.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@respx.mock
async def test_empty_choices_is_an_error(settings: Settings) -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    async with ModelPlaneClient(settings) as client:
        with pytest.raises(ModelPlaneError):
            await client.chat([ChatMessage(role="user", content="x")])


async def test_assistant_tool_call_message_serialises_without_null_content() -> None:
    """Runtimes reject null content; a tool-requesting turn must still round-trip."""
    from uione.modelplane.types import ToolCall

    msg = ChatMessage(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="c1", name="f", arguments="{}")],
    )
    wire = msg.to_wire()

    assert wire["content"] == ""
    assert wire["tool_calls"][0]["function"]["name"] == "f"


async def test_malformed_tool_arguments_raise_a_clear_error() -> None:
    from uione.modelplane.types import ToolCall

    with pytest.raises(ValueError, match="invalid JSON"):
        ToolCall(id="c", name="f", arguments="{not json").parsed_arguments()


async def test_non_object_tool_arguments_are_rejected() -> None:
    from uione.modelplane.types import ToolCall

    with pytest.raises(ValueError, match="must be an object"):
        ToolCall(id="c", name="f", arguments="[1, 2]").parsed_arguments()
