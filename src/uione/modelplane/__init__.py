"""Model plane: the boundary between UiOne and whatever serves open-weight models.

Everything downstream of this package speaks one OpenAI-compatible dialect, so the
serving runtime (llm_inference_engine on vLLM, llama.cpp, MLX, or Ollama in
development) is configuration rather than code.
"""

from uione.modelplane.client import (
    ModelPlaneClient,
    ModelPlaneError,
    ModelPlaneTimeout,
    ModelPlaneUnavailable,
)
from uione.modelplane.router import TaskRouter
from uione.modelplane.types import (
    ChatMessage,
    Completion,
    TaskClass,
    ToolCall,
    ToolDefinition,
    Usage,
)

__all__ = [
    "ChatMessage",
    "Completion",
    "ModelPlaneClient",
    "ModelPlaneError",
    "ModelPlaneTimeout",
    "ModelPlaneUnavailable",
    "TaskClass",
    "TaskRouter",
    "ToolCall",
    "ToolDefinition",
    "Usage",
]
