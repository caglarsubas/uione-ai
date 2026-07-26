"""Runtime configuration.

Every setting is environment-driven so the same image runs in a laptop PoC and an
air-gapped datacentre. Nothing here may default to an internet endpoint: an
on-premise product that silently phones home is a failed audit.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="UIONE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    environment: str = "dev"

    # --- Model plane (llm_inference_engine, OpenAI-compatible) ---
    model_plane_url: str = Field(
        default="http://127.0.0.1:8080/v1",
        description="Base URL of the OpenAI-compatible inference engine.",
    )
    model_plane_api_key: str = ""
    model_plane_timeout_s: float = 120.0
    model_plane_connect_timeout_s: float = 10.0

    # Task-tier model assignments. Overridable per deployment; see docs/MODEL_TIERS.md.
    model_tier_triage: str = "qwen3:4b"
    model_tier_workhorse: str = "qwen3:30b-a3b"
    model_tier_reasoning: str = "qwen3:30b-a3b"
    model_tier_embedding: str = "qwen3-embedding:0.6b"

    # --- Persistence ---
    database_url: str = "sqlite+aiosqlite:///./uione.db"

    # --- Governance ---
    # Deny-by-default: an action class must be explicitly allow-listed to auto-run.
    autonomy_default_mode: str = "preview"

    @field_validator("model_plane_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("autonomy_default_mode")
    @classmethod
    def _valid_autonomy(cls, v: str) -> str:
        allowed = {"preview", "approve", "auto"}
        if v not in allowed:
            raise ValueError(f"autonomy_default_mode must be one of {sorted(allowed)}")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
