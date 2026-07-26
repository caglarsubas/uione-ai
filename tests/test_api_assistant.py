from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from uione.api import deps
from uione.api.app import create_app
from uione.modelplane import Completion, ToolCall

ALICE_HEADERS = {"X-User-Id": "alice", "X-User-Roles": "analyst", "X-User-Name": "Alice"}
BOB_HEADERS = {"X-User-Id": "bob", "X-User-Roles": "analyst"}


class ScriptedModel:
    def __init__(self, *completions: Completion) -> None:
        self._queue = list(completions)

    async def chat(self, messages, **kwargs):
        return self._queue.pop(0) if self._queue else Completion(content="ok", model="stub")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """App wired with a stub model so the API is testable without a GPU."""
    original = deps.build_services

    async def build_with_stub():
        services = await original()
        services.model = ScriptedModel()
        services.runtime._model = services.model
        services.brief._model = services.model
        return services

    monkeypatch.setattr(deps, "build_services", build_with_stub)
    with TestClient(create_app()) as c:
        yield c


def use_model(client: TestClient, *completions: Completion) -> None:
    services = deps.get_services()
    model = ScriptedModel(*completions)
    services.runtime._model = model
    services.brief._model = model


# -- brief -----------------------------------------------------------------


def test_brief_returns_body_and_provenance(client: TestClient) -> None:
    use_model(client, Completion(content="Your brief.", model="stub"))

    r = client.get("/brief", headers=ALICE_HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert body["body"] == "Your brief."
    assert body["complete"] is True
    assert body["provenance"]["incidents"] == "incidents.active"


def test_brief_reports_each_section_with_its_source(client: TestClient) -> None:
    use_model(client, Completion(content="x"))

    sections = client.get("/brief", headers=ALICE_HEADERS).json()["sections"]

    assert {s["section"] for s in sections} == {"incidents", "mail", "calendar", "tasks"}
    assert all(s["available"] for s in sections)


def test_brief_flags_untrusted_content(client: TestClient) -> None:
    use_model(client, Completion(content="x"))

    assert client.get("/brief", headers=ALICE_HEADERS).json()["untrusted_content_seen"] is True


# -- chat and the approval loop -------------------------------------------


def test_chat_answers_directly(client: TestClient) -> None:
    use_model(client, Completion(content="Ankara.", model="stub"))

    r = client.post("/chat", json={"message": "capital of Turkey?"}, headers=ALICE_HEADERS)

    assert r.status_code == 200
    assert r.json()["reply"] == "Ankara."
    assert r.json()["tool_calls"] == []


def test_read_tool_runs_without_approval(client: TestClient) -> None:
    use_model(
        client,
        Completion(tool_calls=[ToolCall(id="c1", name="tasks.my_open_issues", arguments="{}")]),
        Completion(content="You have 3 open tasks."),
    )

    r = client.post("/chat", json={"message": "my tasks?"}, headers=ALICE_HEADERS)

    body = r.json()
    assert body["tool_calls"][0]["ok"] is True
    assert body["pending_approvals"] == []


def test_write_is_held_and_surfaced_to_the_user(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="tasks.update_issue",
                    arguments='{"key": "PAY-1182", "status": "Done"}',
                )
            ]
        ),
        Completion(content="That needs your approval."),
    )

    body = client.post("/chat", json={"message": "close PAY-1182"}, headers=ALICE_HEADERS).json()

    assert len(body["pending_approvals"]) == 1
    assert body["tool_calls"][0]["held"] is True
    assert "need your approval" in body["notice"]


def test_approval_queue_shows_the_real_payload(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)

    pending = client.get("/approvals", headers=ALICE_HEADERS).json()

    assert len(pending) == 1
    assert "PAY-1182" in pending[0]["preview"]
    assert pending[0]["risk"] == "reversible_write"


def test_approving_executes_the_action(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)
    action_id = client.get("/approvals", headers=ALICE_HEADERS).json()[0]["id"]

    r = client.post(f"/approvals/{action_id}/approve", json={}, headers=ALICE_HEADERS)

    assert r.status_code == 200
    assert r.json()["executed"] is True
    assert client.get("/approvals", headers=ALICE_HEADERS).json() == []


def test_rejecting_closes_the_action(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)
    action_id = client.get("/approvals", headers=ALICE_HEADERS).json()[0]["id"]

    r = client.post(
        f"/approvals/{action_id}/reject", json={"note": "wrong ticket"}, headers=ALICE_HEADERS
    )

    assert r.json()["status"] == "rejected"
    assert client.get("/approvals", headers=ALICE_HEADERS).json() == []


def test_deciding_twice_conflicts(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)
    action_id = client.get("/approvals", headers=ALICE_HEADERS).json()[0]["id"]
    client.post(f"/approvals/{action_id}/approve", json={}, headers=ALICE_HEADERS)

    r = client.post(f"/approvals/{action_id}/reject", json={}, headers=ALICE_HEADERS)

    assert r.status_code == 409


# -- authorisation ---------------------------------------------------------


def test_another_user_cannot_see_or_decide_your_actions(client: TestClient) -> None:
    """404 rather than 403, so the endpoint does not enumerate others' action IDs."""
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)
    action_id = client.get("/approvals", headers=ALICE_HEADERS).json()[0]["id"]

    assert client.get("/approvals", headers=BOB_HEADERS).json() == []
    assert (
        client.post(f"/approvals/{action_id}/approve", json={}, headers=BOB_HEADERS).status_code
        == 404
    )


def test_unknown_action_is_404(client: TestClient) -> None:
    r = client.post("/approvals/deadbeef/approve", json={}, headers=ALICE_HEADERS)
    assert r.status_code == 404


# -- transparency ----------------------------------------------------------


def test_autonomy_page_shows_what_the_assistant_may_do(client: TestClient) -> None:
    body = client.get("/me/autonomy", headers=ALICE_HEADERS).json()

    assert "tasks.update_issue" in body["visible_tools"]
    assert body["recent_actions"] == []


def test_autonomy_page_records_executed_actions(client: TestClient) -> None:
    use_model(
        client,
        Completion(
            tool_calls=[
                ToolCall(id="c1", name="tasks.update_issue", arguments='{"key": "PAY-1182"}')
            ]
        ),
        Completion(content="held"),
    )
    client.post("/chat", json={"message": "close it"}, headers=ALICE_HEADERS)
    action_id = client.get("/approvals", headers=ALICE_HEADERS).json()[0]["id"]
    client.post(f"/approvals/{action_id}/approve", json={}, headers=ALICE_HEADERS)

    body = client.get("/me/autonomy", headers=ALICE_HEADERS).json()

    assert body["recent_actions"][0]["tool"] == "tasks.update_issue"


def test_connector_health_is_exposed(client: TestClient) -> None:
    body = client.get("/system/health").json()

    assert set(body["connectors"]) == {"mail", "tasks", "incidents", "calendar"}
    assert body["degraded"] == []
