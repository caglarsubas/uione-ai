from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from uione.api.app import create_app
from uione.config import get_settings


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health_is_independent_of_downstreams(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@respx.mock
def test_ready_when_model_plane_reachable(client: TestClient) -> None:
    base = get_settings().model_plane_url
    respx.get(f"{base}/models").mock(return_value=httpx.Response(200, json={"data": []}))

    r = client.get("/ready")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["model_plane"] == "reachable"


@respx.mock
def test_ready_reports_degraded_when_model_plane_down(client: TestClient) -> None:
    base = get_settings().model_plane_url
    respx.get(f"{base}/models").mock(side_effect=httpx.ConnectError("refused"))

    r = client.get("/ready")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["model_plane"] == "unreachable"
