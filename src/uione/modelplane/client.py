"""HTTP client for an OpenAI-compatible model plane.

Normalises the dialect differences between serving runtimes (llm_inference_engine,
vLLM, Ollama) so callers see one shape, and turns transport failures into a small
set of typed errors the agent runtime can reason about.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx
import structlog

from uione.config import Settings, get_settings
from uione.modelplane.types import (
    ChatMessage,
    Completion,
    TaskClass,
    ToolCall,
    ToolDefinition,
    Usage,
)

log = structlog.get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class ModelPlaneError(RuntimeError):
    """Base class for model plane failures."""


class ModelPlaneTimeout(ModelPlaneError):
    """The model plane did not answer within the configured budget."""


class ModelPlaneUnavailable(ModelPlaneError):
    """The model plane is unreachable or persistently failing."""


class UsageRecorder:
    """Collects token usage so cost can be attributed per user and department.

    In-process and deliberately trivial for now; the interface is what matters, so
    that F11.1 (token and GPU accounting) has a seam to attach to.
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.by_model: dict[str, Usage] = {}

    def record(self, model: str, usage: Usage) -> None:
        self.calls += 1
        agg = self.by_model.setdefault(model, Usage())
        agg.prompt_tokens += usage.prompt_tokens
        agg.completion_tokens += usage.completion_tokens
        agg.total_tokens += usage.total_tokens


class ModelPlaneClient:
    """Async client against one OpenAI-compatible endpoint."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        recorder: UsageRecorder | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._settings.model_plane_timeout_s,
                connect=self._settings.model_plane_connect_timeout_s,
            )
        )
        self.usage = recorder or UsageRecorder()

    async def __aenter__(self) -> ModelPlaneClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    # -- internals ---------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.model_plane_api_key:
            headers["Authorization"] = f"Bearer {self._settings.model_plane_api_key}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._settings.model_plane_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post(url, json=payload, headers=self._headers)
            except httpx.TimeoutException as exc:
                last_exc = ModelPlaneTimeout(f"{path} timed out after {attempt} attempt(s)")
                log.warning("modelplane.timeout", path=path, attempt=attempt, error=str(exc))
            except httpx.HTTPError as exc:
                last_exc = ModelPlaneUnavailable(f"{path} unreachable: {exc}")
                log.warning(
                    "modelplane.transport_error", path=path, attempt=attempt, error=str(exc)
                )
            else:
                if response.status_code in _RETRYABLE_STATUS:
                    last_exc = ModelPlaneUnavailable(
                        f"{path} returned {response.status_code}: {response.text[:200]}"
                    )
                    log.warning(
                        "modelplane.retryable_status",
                        path=path,
                        attempt=attempt,
                        status=response.status_code,
                    )
                elif response.is_error:
                    # 4xx other than 408/429 will not succeed on retry.
                    raise ModelPlaneError(
                        f"{path} returned {response.status_code}: {response.text[:500]}"
                    )
                else:
                    return response.json()

            if attempt < _MAX_ATTEMPTS:
                # Jittered backoff so a restarting engine is not thundered on.
                await asyncio.sleep((2 ** (attempt - 1)) * 0.5 + random.uniform(0, 0.25))

        assert last_exc is not None
        raise last_exc

    def _resolve_model(self, task: TaskClass | None, model: str | None) -> str:
        if model:
            return model
        if task is None:
            return self._settings.model_tier_workhorse
        return {
            TaskClass.TRIAGE: self._settings.model_tier_triage,
            TaskClass.WORKHORSE: self._settings.model_tier_workhorse,
            TaskClass.REASONING: self._settings.model_tier_reasoning,
        }[task]

    # -- public API --------------------------------------------------------

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        task: TaskClass = TaskClass.WORKHORSE,
        model: str | None = None,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        """Run one chat completion.

        ``response_format`` is passed through so callers can demand JSON; the agent
        runtime pairs it with schema validation rather than trusting it (gap G5).
        """
        resolved = self._resolve_model(task, model)
        payload: dict[str, Any] = {
            "model": resolved,
            "messages": [m.to_wire() for m in messages],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [t.to_wire() for t in tools]
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        data = await self._post("/chat/completions", payload)
        completion = _parse_completion(data, fallback_model=resolved)
        self.usage.record(completion.model or resolved, completion.usage)
        log.debug(
            "modelplane.chat",
            model=completion.model or resolved,
            task=str(task),
            tool_calls=len(completion.tool_calls),
            total_tokens=completion.usage.total_tokens,
        )
        return completion

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        payload = {"model": model or self._settings.model_tier_embedding, "input": texts}
        data = await self._post("/embeddings", payload)
        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [row["embedding"] for row in rows]

    async def list_models(self) -> list[str]:
        url = f"{self._settings.model_plane_url}/models"
        try:
            response = await self._client.get(url, headers=self._headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelPlaneUnavailable(f"/models unreachable: {exc}") from exc
        return [m["id"] for m in response.json().get("data", [])]


def _parse_completion(data: dict[str, Any], *, fallback_model: str) -> Completion:
    """Normalise a raw chat-completion body.

    Tolerates the small dialect differences between runtimes: absent ``usage``,
    ``function_call`` instead of ``tool_calls``, missing ``id`` on a tool call.
    """
    choices = data.get("choices") or []
    if not choices:
        raise ModelPlaneError("model plane returned no choices")

    choice = choices[0]
    message = choice.get("message") or {}

    tool_calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        fn = raw.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        tool_calls.append(
            ToolCall(
                # Some runtimes omit the id; synthesise a stable one so the
                # tool-result correlation in the agent loop still works.
                id=raw.get("id") or f"call_{index}",
                name=name,
                arguments=fn.get("arguments") or "{}",
            )
        )

    # Legacy single-function dialect.
    legacy = message.get("function_call") or {}
    if not tool_calls and legacy.get("name"):
        tool_calls.append(
            ToolCall(
                id="call_0",
                name=legacy["name"],
                arguments=legacy.get("arguments") or "{}",
            )
        )

    raw_usage = data.get("usage") or {}
    usage = Usage(
        prompt_tokens=raw_usage.get("prompt_tokens") or 0,
        completion_tokens=raw_usage.get("completion_tokens") or 0,
        total_tokens=raw_usage.get("total_tokens") or 0,
    )

    return Completion(
        content=message.get("content"),
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason"),
        model=data.get("model") or fallback_model,
        usage=usage,
    )
