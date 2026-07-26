from __future__ import annotations

import pytest
from pydantic import ValidationError

from uione.config import Settings


def test_model_plane_url_trailing_slash_is_normalised() -> None:
    assert Settings(model_plane_url="http://engine:8080/v1/").model_plane_url == (
        "http://engine:8080/v1"
    )


def test_autonomy_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(autonomy_default_mode="yolo")


def test_autonomy_defaults_to_preview() -> None:
    """Deny-by-default: nothing auto-executes until explicitly configured (gap G1)."""
    assert Settings().autonomy_default_mode == "preview"


def test_no_default_points_at_the_public_internet() -> None:
    """An on-prem product must never phone home by default."""
    s = Settings()
    assert s.model_plane_url.startswith(("http://127.0.0.1", "http://localhost"))
