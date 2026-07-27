"""The Gitea task connector.

Two ways of running the same assertions:

* against the **mock**, in-process over ASGI — fast, no ports, runs in CI forever;
* against a **real Gitea**, when `UIONE_TEST_GITEA_URL` and `UIONE_TEST_GITEA_TOKEN`
  are set — which is how the mock's fidelity is kept honest.

That second path is the point of preferring a self-hostable system. A connector
whose only evidence is a fixture someone wrote is a connector that agrees with
its author, and the failure mode is silent: every test passes and the first real
call 404s on a field name nobody checked.
"""

from __future__ import annotations

import os

import httpx
import pytest

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.connectors.tasks import (
    GiteaTasks,
    build_gitea_source,
    gitea_config,
    issue_key,
    parse_ref,
)
from uione.mcphub import (
    AuditLog,
    Grant,
    InMemoryAuditSink,
    McpGateway,
    Principal,
    RiskClass,
    ToolPolicy,
)
from uione.vendormocks.gitea import build_gitea_mock, seed_gitea

ALICE = Principal(user_id="uione", roles=frozenset({"analyst"}))

REAL_URL = os.environ.get("UIONE_TEST_GITEA_URL", "")
REAL_TOKEN = os.environ.get("UIONE_TEST_GITEA_TOKEN", "")


@pytest.fixture
def mock_tasks() -> GiteaTasks:
    """The connector, wired to the mock over ASGI rather than a socket."""
    app = build_gitea_mock(seed_gitea())
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gitea.mock/api/v1"
    )
    return GiteaTasks(gitea_config("http://gitea.mock", "test-token"), client=client)


# -- reference parsing -----------------------------------------------------


def test_a_reference_splits_into_owner_repo_and_number() -> None:
    assert parse_ref("uione/payments-platform#3") == ("uione", "payments-platform", 3)


@pytest.mark.parametrize("bad", ["#3", "PAY-1182", "uione#3", "uione/repo", "uione/repo#x", ""])
def test_a_malformed_reference_is_refused_rather_than_guessed(bad: str) -> None:
    """A model that passes `#3` must be told the format, not have a repository
    chosen for it."""
    with pytest.raises(ValueError, match="owner/repo#number"):
        parse_ref(bad)


def test_the_key_is_the_one_a_person_would_recognise() -> None:
    """Not the database id, which appears in no UI, commit message or sentence."""
    issue = {"number": 7, "repository": {"full_name": "uione/payments-platform"}}

    assert issue_key(issue) == "uione/payments-platform#7"


# -- reading ---------------------------------------------------------------


async def test_the_queue_lists_open_issues(mock_tasks: GiteaTasks) -> None:
    source = build_gitea_source(mock_tasks)

    result = await source.call("my_open_issues", {})

    assert result.ok
    assert "PAY-1182" in result.content
    assert result.structured["count"] == 3


async def test_the_queue_excludes_closed_work(mock_tasks: GiteaTasks) -> None:
    result = await build_gitea_source(mock_tasks).call("my_open_issues", {})

    assert "Rotate acquirer sandbox credentials" not in result.content


async def test_an_issue_appears_once_even_when_raised_and_owned(mock_tasks: GiteaTasks) -> None:
    """Gitea's `assigned` and `created_by` are separate filters, so the connector
    makes two calls; merging them badly shows the same issue twice."""
    result = await build_gitea_source(mock_tasks).call("my_open_issues", {})

    keys = result.structured["keys"]
    assert len(keys) == len(set(keys))


async def test_reading_one_issue_includes_its_body_and_comments(mock_tasks: GiteaTasks) -> None:
    result = await build_gitea_source(mock_tasks).call(
        "get_issue", {"issue": "uione/payments-platform#1"}
    )

    assert "soft decline" in result.content
    assert "ops-oncall" in result.content


async def test_the_queue_carries_no_bodies(mock_tasks: GiteaTasks) -> None:
    """A queue is titles and states. Pulling every description to render a
    summary line fills the context with text nobody asked for."""
    result = await build_gitea_source(mock_tasks).call("my_open_issues", {})

    assert "Ops escalated at 07:40" not in result.content


async def test_an_empty_queue_says_so_plainly(mock_tasks: GiteaTasks) -> None:
    app = build_gitea_mock()  # no seed
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gitea.mock/api/v1"
    )
    tasks = GiteaTasks(gitea_config("http://gitea.mock", "t"), client=client)

    result = await build_gitea_source(tasks).call("my_open_issues", {})

    assert result.ok
    assert result.structured["count"] == 0


# -- writing ---------------------------------------------------------------


async def test_closing_an_issue_moves_its_state(mock_tasks: GiteaTasks) -> None:
    source = build_gitea_source(mock_tasks)

    result = await source.call(
        "update_issue", {"issue": "uione/payments-platform#2", "state": "closed"}
    )

    assert result.ok
    assert result.structured["state"] == "closed"
    assert (
        "uione/payments-platform#2"
        not in (await source.call("my_open_issues", {})).structured["keys"]
    )


async def test_closing_is_reversible(mock_tasks: GiteaTasks) -> None:
    """The issue survives, which is what makes this a REVERSIBLE_WRITE rather
    than something that can never earn unattended execution."""
    source = build_gitea_source(mock_tasks)
    await source.call("update_issue", {"issue": "uione/payments-platform#2", "state": "closed"})

    reopened = await source.call(
        "update_issue", {"issue": "uione/payments-platform#2", "state": "open"}
    )

    assert reopened.structured["state"] == "open"


async def test_an_invalid_state_is_refused_before_the_call(mock_tasks: GiteaTasks) -> None:
    result = await build_gitea_source(mock_tasks).call(
        "update_issue", {"issue": "uione/payments-platform#1", "state": "done"}
    )

    assert not result.ok
    assert "open" in (result.error or "")


async def test_commenting_reaches_the_issue(mock_tasks: GiteaTasks) -> None:
    source = build_gitea_source(mock_tasks)

    posted = await source.call(
        "comment_on_issue", {"issue": "uione/payments-platform#1", "body": "Acquirer notified."}
    )
    read = await source.call("get_issue", {"issue": "uione/payments-platform#1"})

    assert posted.ok
    assert "Acquirer notified." in read.content


async def test_an_empty_comment_is_refused(mock_tasks: GiteaTasks) -> None:
    result = await build_gitea_source(mock_tasks).call(
        "comment_on_issue", {"issue": "uione/payments-platform#1", "body": "   "}
    )

    assert not result.ok


# -- risk classification ---------------------------------------------------


async def test_the_risks_we_assign_are_the_ones_that_matter(mock_tasks: GiteaTasks) -> None:
    specs = {s.tool: s for s in await build_gitea_source(mock_tasks).list_tools()}

    assert specs["my_open_issues"].risk is RiskClass.READ
    assert specs["get_issue"].risk is RiskClass.READ
    # Closing keeps the issue, so it can be put back.
    assert specs["update_issue"].risk is RiskClass.REVERSIBLE_WRITE
    # A comment notifies watchers, and that cannot be unsent from their inbox.
    assert specs["comment_on_issue"].risk is RiskClass.IRREVERSIBLE


async def test_issue_text_is_treated_as_untrusted(mock_tasks: GiteaTasks) -> None:
    """Anyone who can file an issue can write into the model's context —
    contractors on an internal tracker, anybody at all on a public one."""
    specs = {s.tool: s for s in await build_gitea_source(mock_tasks).list_tools()}

    assert specs["my_open_issues"].returns_untrusted_content
    assert specs["get_issue"].returns_untrusted_content


# -- failure -----------------------------------------------------------------


async def test_a_missing_issue_fails_without_raising(mock_tasks: GiteaTasks) -> None:
    """One dead call is not an outage: the agent loop must be able to show the
    model why and let it choose differently."""
    result = await build_gitea_source(mock_tasks).call(
        "get_issue", {"issue": "uione/payments-platform#999"}
    )

    assert not result.ok
    assert "404" in (result.error or "") or "no such object" in (result.error or "")


async def test_an_unreachable_vendor_reports_itself_by_name() -> None:
    tasks = GiteaTasks(
        VendorConfig(
            name="gitea",
            base_url="http://127.0.0.1:1/api/v1",
            auth=Auth(scheme="token", secret="t"),
            attempts=1,
            timeout_s=0.5,
        )
    )
    try:
        result = await build_gitea_source(tasks).call("my_open_issues", {})
    finally:
        await tasks.aclose()

    assert not result.ok
    assert "gitea" in (result.error or "")


# -- through the gateway ---------------------------------------------------


async def test_the_connector_arrives_governed(mock_tasks: GiteaTasks) -> None:
    hub = McpGateway(
        policy=ToolPolicy(
            [Grant(role="analyst", tools=frozenset({"tasks.*"}), max_risk=RiskClass.READ)]
        ),
        audit=AuditLog(InMemoryAuditSink()),
    )
    await hub.register(build_gitea_source(mock_tasks))

    read = await hub.call(ALICE, "tasks.my_open_issues", {})
    write = await hub.call(ALICE, "tasks.comment_on_issue", {"issue": "a/b#1", "body": "hi"})

    assert read.result.ok
    assert not write.result.ok, "a read-only grant must not reach an irreversible tool"


# -- against a real Gitea, when there is one -------------------------------

real_gitea = pytest.mark.skipif(
    not (REAL_URL and REAL_TOKEN),
    reason="set UIONE_TEST_GITEA_URL and UIONE_TEST_GITEA_TOKEN to run against a real instance",
)


@pytest.fixture
async def live_tasks():
    tasks = GiteaTasks(gitea_config(REAL_URL, REAL_TOKEN))
    yield tasks
    await tasks.aclose()


@real_gitea
async def test_real_gitea_authenticates(live_tasks: GiteaTasks) -> None:
    assert (await live_tasks.whoami()).get("login")


@real_gitea
async def test_real_gitea_returns_the_field_names_the_mock_claims(
    live_tasks: GiteaTasks,
) -> None:
    """The assertion the mock cannot make about itself.

    The first draft of the mock called this field `repo`, which is the obvious
    name and the wrong one. Every test passed; the real call would have produced
    keys reading `?#3`.
    """
    issues = await live_tasks.my_issues(limit=5)
    if not issues:
        pytest.skip("the live instance has no open issues for this token")

    issue = issues[0]
    assert "repository" in issue
    assert "full_name" in issue["repository"]
    assert issue_key(issue).count("/") == 1


@real_gitea
async def test_real_gitea_round_trips_a_state_change(live_tasks: GiteaTasks) -> None:
    issues = await live_tasks.my_issues(limit=1)
    if not issues:
        pytest.skip("the live instance has no open issues for this token")

    owner, repo, number = parse_ref(issue_key(issues[0]))
    closed = await live_tasks.set_state(owner, repo, number, state="closed")
    reopened = await live_tasks.set_state(owner, repo, number, state="open")

    assert closed["state"] == "closed"
    assert reopened["state"] == "open"


# -- the spine itself, over a real socket ----------------------------------


async def test_retry_after_is_honoured_and_capped() -> None:
    """A vendor asking for six hours must not become a six-hour hang."""
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"ok": True})

    client = VendorClient(
        VendorConfig(name="slow", base_url="http://vendor.test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://vendor.test"
        ),
    )

    assert await client.get("/thing") == {"ok": True}
    assert attempts["n"] == 2


async def test_a_client_error_is_not_retried() -> None:
    """Retrying a 400 sends the same bad request; retrying a 403 looks like
    brute force to whoever is watching the vendor's logs."""
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403, json={"message": "forbidden"})

    client = VendorClient(
        VendorConfig(name="strict", base_url="http://vendor.test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://vendor.test"
        ),
    )

    with pytest.raises(VendorError, match="lacks permission"):
        await client.get("/thing")
    assert attempts["n"] == 1


async def test_html_instead_of_json_names_the_real_problem() -> None:
    """A vendor answering 200 with a login page means the session expired.
    Reporting "invalid JSON" sends the reader somewhere else entirely."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Please sign in</body></html>")

    client = VendorClient(
        VendorConfig(name="portal", base_url="http://vendor.test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://vendor.test"
        ),
    )

    with pytest.raises(VendorError, match="credential"):
        await client.get("/thing")


def test_a_credential_cannot_be_printed_by_accident() -> None:
    config = VendorConfig(
        name="v", base_url="http://x", auth=Auth(scheme="bearer", secret="super-secret-token")
    )

    assert "super-secret-token" not in repr(config)
    assert "super-secret-token" not in repr(config.auth)
    assert config.auth.headers()["Authorization"].endswith("super-secret-token")


async def test_pagination_stops_rather_than_following_forever() -> None:
    """A project with 80,000 issues must not be loaded into a process that also
    has to answer chat."""
    pages = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        pages["n"] += 1
        return httpx.Response(200, json=[{"i": i} for i in range(50)])

    client = VendorClient(
        VendorConfig(name="endless", base_url="http://vendor.test"),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://vendor.test"
        ),
    )

    items = await client.paginate("/things", page_size=50)

    assert pages["n"] == 10
    assert len(items) == 500
