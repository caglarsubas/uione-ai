from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from uione.api import deps
from uione.api.app import create_app
from uione.config import get_settings
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
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    """App wired with a stub model and a throwaway database.

    The database is per-test on purpose: governance state is now durable, so
    without isolation each test would inherit the previous one's pending
    approvals and autonomy record.
    """
    monkeypatch.setenv("UIONE_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    get_settings.cache_clear()

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

    get_settings.cache_clear()


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


# -- pre-generated briefs and schedules -----------------------------------


def test_first_brief_is_generated_on_request(client: TestClient) -> None:
    use_model(client, Completion(content="Fresh brief."))

    body = client.get("/brief", headers=ALICE_HEADERS).json()

    assert body["pregenerated"] is False
    assert body["body"] == "Fresh brief."


def test_second_read_is_served_from_the_pre_generated_copy(client: TestClient) -> None:
    """'Good morning' should be answered immediately, not regenerated per reader."""
    use_model(client, Completion(content="First."))
    client.get("/brief", headers=ALICE_HEADERS)

    use_model(client, Completion(content="Second."))
    body = client.get("/brief", headers=ALICE_HEADERS).json()

    assert body["pregenerated"] is True
    assert body["body"] == "First."
    assert body["age_seconds"] is not None


def test_refresh_forces_regeneration(client: TestClient) -> None:
    use_model(client, Completion(content="First."))
    client.get("/brief", headers=ALICE_HEADERS)

    use_model(client, Completion(content="Second."))
    body = client.get("/brief?refresh=true", headers=ALICE_HEADERS).json()

    assert body["body"] == "Second."
    assert body["pregenerated"] is False


def test_cached_briefs_are_per_user(client: TestClient) -> None:
    use_model(client, Completion(content="Alice's."))
    client.get("/brief", headers=ALICE_HEADERS)

    use_model(client, Completion(content="Bob's."))
    body = client.get("/brief", headers=BOB_HEADERS).json()

    assert body["body"] == "Bob's."
    assert body["pregenerated"] is False


def test_schedule_starts_empty(client: TestClient) -> None:
    assert client.get("/me/schedule", headers=ALICE_HEADERS).json() == []


def test_setting_a_schedule_returns_the_next_run(client: TestClient) -> None:
    r = client.put(
        "/me/schedule",
        json={"at": "06:45", "timezone": "Europe/Istanbul"},
        headers=ALICE_HEADERS,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["at"] == "06:45"
    assert body["timezone"] == "Europe/Istanbul"
    assert body["next_run"] is not None


def test_schedule_can_be_disabled(client: TestClient) -> None:
    client.put("/me/schedule", json={"at": "07:00"}, headers=ALICE_HEADERS)

    r = client.put("/me/schedule", json={"enabled": False}, headers=ALICE_HEADERS)

    assert r.json()["enabled"] is False
    assert r.json()["next_run"] is None


def test_updating_a_schedule_keeps_the_other_fields(client: TestClient) -> None:
    client.put(
        "/me/schedule", json={"at": "06:45", "timezone": "Europe/Istanbul"}, headers=ALICE_HEADERS
    )

    r = client.put("/me/schedule", json={"at": "08:15"}, headers=ALICE_HEADERS)

    assert r.json()["at"] == "08:15"
    assert r.json()["timezone"] == "Europe/Istanbul"


def test_a_bad_time_is_rejected(client: TestClient) -> None:
    r = client.put("/me/schedule", json={"at": "tomorrow"}, headers=ALICE_HEADERS)
    assert r.status_code == 422


def test_an_unknown_timezone_is_rejected(client: TestClient) -> None:
    r = client.put("/me/schedule", json={"timezone": "Mars/Olympus"}, headers=ALICE_HEADERS)
    assert r.status_code == 422


def test_schedules_are_per_user(client: TestClient) -> None:
    client.put("/me/schedule", json={"at": "06:45"}, headers=ALICE_HEADERS)

    assert client.get("/me/schedule", headers=BOB_HEADERS).json() == []


# -- the workspace ---------------------------------------------------------


def test_workspace_is_served(client: TestClient) -> None:
    r = client.get("/ui/")

    assert r.status_code == 200
    assert "UiOne" in r.text


def test_root_redirects_to_the_workspace(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)

    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/ui/"


def test_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/styles.css").status_code == 200


def test_the_client_loads_nothing_from_outside(client: TestClient) -> None:
    """An air-gapped deployment cannot reach the internet, and must not try.

    Matches reference syntax rather than the substring "cdn", which appears in a
    source comment stating there is no CDN — the first version of this test
    failed on its own documentation.
    """
    external = re.compile(
        r"""(src|href)\s*=\s*["']\s*(https?:)?//"""  # <script src="//...">
        r"""|url\(\s*["']?\s*(https?:)?//"""  # css url(//...)
        r"""|@import\s+(url\()?["']\s*(https?:)?//"""  # css @import
        r"""|fetch\(\s*["']\s*(https?:)?//""",  # fetch("//...")
        re.IGNORECASE,
    )

    for path in ("/ui/", "/ui/app.js", "/ui/styles.css"):
        body = client.get(path).text
        match = external.search(body)
        assert match is None, f"{path} references an external origin: {match.group(0)!r}"


def test_api_routes_are_not_shadowed_by_the_static_mount(client: TestClient) -> None:
    assert client.get("/system/health").status_code == 200


# -- authentication --------------------------------------------------------


def test_an_unidentified_request_is_refused(client: TestClient) -> None:
    """There is no default identity. The placeholder used to return 'alice'."""
    r = client.get("/brief")

    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_every_user_route_requires_identity(client: TestClient) -> None:
    for path in ("/brief", "/approvals", "/me/autonomy", "/me/schedule"):
        assert client.get(path).status_code == 401, path
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_health_does_not_require_identity(client: TestClient) -> None:
    """Liveness probes have no credentials and must not be gated."""
    assert client.get("/health").status_code == 200


def test_the_workspace_itself_is_not_gated(client: TestClient) -> None:
    """The page has to load in order to authenticate; the API behind it is gated."""
    assert client.get("/ui/").status_code == 200


# -- A2A over the API ------------------------------------------------------


def register_bob(client: TestClient) -> None:
    """Give Bob an assistant by having him make one request."""
    client.get("/colleagues", headers=BOB_HEADERS)


def test_colleagues_lists_other_assistants(client: TestClient) -> None:
    register_bob(client)

    listed = client.get("/colleagues", headers=ALICE_HEADERS).json()

    assert any(c["owner_id"] == "bob" for c in listed)
    assert all(c["owner_id"] != "alice" for c in listed), "own assistant is not a colleague"


def test_asking_a_colleague_reveals_availability_only(client: TestClient) -> None:
    register_bob(client)

    r = client.post(
        "/colleagues/ask",
        json={"agent_id": "agent:bob", "kind": "ask_availability"},
        headers=ALICE_HEADERS,
    )

    body = r.json()
    assert body["outcome"] in ("answered", "partial")
    assert "free_slots" in body["data"]
    assert "meetings" not in body["data"]


def test_workload_is_refused_by_the_default_contract(client: TestClient) -> None:
    """The 'what is she working on?' question, refused by default."""
    register_bob(client)

    body = client.post(
        "/colleagues/ask",
        json={"agent_id": "agent:bob", "kind": "ask_workload"},
        headers=ALICE_HEADERS,
    ).json()

    assert body["outcome"] == "refused"


def test_bob_can_widen_his_own_contract(client: TestClient) -> None:
    register_bob(client)
    client.put(
        "/me/disclosure",
        json={"default": ["free_busy", "out_of_office", "contact", "workload"]},
        headers=BOB_HEADERS,
    )

    body = client.post(
        "/colleagues/ask",
        json={"agent_id": "agent:bob", "kind": "ask_workload"},
        headers=ALICE_HEADERS,
    ).json()

    assert body["outcome"] in ("answered", "partial")
    assert "workload" in body["data"]


def test_alice_cannot_change_bobs_contract(client: TestClient) -> None:
    """The subject owns the policy. Otherwise it is not a policy."""
    register_bob(client)
    client.put("/me/disclosure", json={"default": ["workload"]}, headers=ALICE_HEADERS)

    bob = client.get("/me/disclosure", headers=BOB_HEADERS).json()

    assert "workload" not in bob["default"]


def test_a_meeting_proposal_lands_in_bobs_approvals(client: TestClient) -> None:
    register_bob(client)

    asked = client.post(
        "/colleagues/ask",
        json={
            "agent_id": "agent:bob",
            "kind": "propose_meeting",
            "payload": {"subject": "incident review", "slot": "14:00"},
        },
        headers=ALICE_HEADERS,
    ).json()

    assert asked["outcome"] == "held"
    pending = client.get("/approvals", headers=BOB_HEADERS).json()
    assert any("incident review" in p["reason"] for p in pending)
    assert client.get("/approvals", headers=ALICE_HEADERS).json() == []


def test_an_unknown_request_kind_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/colleagues/ask",
        json={"agent_id": "agent:bob", "kind": "read_their_email"},
        headers=ALICE_HEADERS,
    )
    assert r.status_code == 422


def test_disclosure_endpoints_require_identity(client: TestClient) -> None:
    assert client.get("/me/disclosure").status_code == 401
    assert client.get("/colleagues").status_code == 401


# -- the login surface -----------------------------------------------------


def test_auth_mode_is_readable_without_signing_in(client: TestClient) -> None:
    """The workspace must know whether to show a sign-in button."""
    body = client.get("/auth/mode").json()

    assert body["mode"] == "dev"
    assert body["login_url"] is None


def test_whoami_reports_the_caller(client: TestClient) -> None:
    body = client.get("/auth/me", headers=ALICE_HEADERS).json()

    assert body["user_id"] == "alice"
    assert body["roles"] == ["analyst"]
    assert body["auth_mode"] == "dev"


def test_whoami_requires_identity(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401


def test_login_is_absent_when_the_deployment_has_no_flow(client: TestClient) -> None:
    """Offering a login we cannot honour is worse than offering none."""
    assert client.get("/auth/login", follow_redirects=False).status_code == 404


def test_logout_is_harmless_without_a_session(client: TestClient) -> None:
    assert client.post("/auth/logout").status_code == 200
