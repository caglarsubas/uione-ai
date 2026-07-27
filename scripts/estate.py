"""Bring up a working estate and point the product at it.

Everything in this file was first done by hand against the real systems. That is
the only reason it can be trusted: each call below is one that was watched to
succeed, not one that looked right.

    python scripts/estate.py up       # start, provision, write .env.estate
    python scripts/estate.py status   # what is running and what is not
    python scripts/estate.py down     # stop containers, keep data
    python scripts/estate.py destroy  # stop containers, delete data and secrets

**Idempotent.** Running `up` twice must not duplicate a repository, a token or an
alert rule, because the second run is usually somebody debugging the first.

**Loopback only.** Every port binds to 127.0.0.1 and the mock estate refuses any
other interface. These services are unauthenticated or hold a well-known
password, which is fine on one machine and an open door on a network.

**Secrets go to `.env.estate`, which is gitignored.** Tokens are generated per
run and printed only as prefixes. A demo that leaves a credential in the repo
teaches the wrong habit at exactly the moment somebody is deciding whether to
trust this thing with their mailbox.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "estate" / "docker-compose.yml"
ENV_FILE = ROOT / ".env.estate"

GITEA = "http://127.0.0.1:3300"
GRAFANA = "http://127.0.0.1:3400"
MATTERMOST = "http://127.0.0.1:8065"

GITEA_USER = "uione"
GITEA_PASSWORD = "uione-dev-pw"
GRAFANA_PASSWORD = "uione-dev-pw"
REPO = "payments-platform"
MM_PASSWORD = "UiOne-dev-pw1"  # Mattermost enforces a length and character mix.

#: How long to wait for a container to become useful. Gitea's first boot
#: migrates a database, which is slower than its healthcheck interval suggests.
READY_TIMEOUT_S = 180


# -- plumbing --------------------------------------------------------------


def request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str = "",
    basic: tuple[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    """One HTTP call. Returns (status, parsed) and never raises on 4xx/5xx.

    Provisioning is full of calls whose failure is expected — "does this repo
    already exist?" is answered with a 409 — so a non-2xx is data here, not an
    exception.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"token {token}")
    if basic:
        raw = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {raw}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = response.read()
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload) if payload else {}
        except ValueError:
            return exc.code, payload.decode(errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        # Broad on purpose. A container that is still starting accepts the
        # connection and then drops it, which surfaces as RemoteDisconnected —
        # an HTTPException, not a URLError, so a narrower clause lets the whole
        # provisioning run die on a service that would have been ready in two
        # seconds. OSError covers URLError and the timeout cases too.
        return 0, str(exc)


def wait_for(name: str, url: str, *, timeout_s: int = READY_TIMEOUT_S) -> None:
    """Poll until a service answers, rather than sleeping and hoping."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status, _ = request(url)
        if status == 200:
            print(f"  {name} ready")
            return
        time.sleep(2)
    raise SystemExit(f"{name} did not become ready within {timeout_s}s ({url})")


def compose(*args: str) -> int:
    if shutil.which("docker") is None:
        raise SystemExit("docker is not installed; the estate needs it for Gitea and Grafana")
    return subprocess.call(["docker", "compose", "-f", str(COMPOSE), *args])


def docker_exec(container: str, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", "exec", container, *args], capture_output=True, text=True, check=False
    )
    return result.returncode, (result.stdout + result.stderr).strip()


# -- Gitea -----------------------------------------------------------------


def provision_gitea() -> str:
    print("\nGitea")
    wait_for("  gitea", f"{GITEA}/api/v1/version")

    # `user create` fails if the account exists, which on a second run it does.
    # Treated as success rather than checked first: there is no race-free way to
    # ask, and the error is unambiguous.
    code, output = docker_exec(
        "uione-gitea",
        "su",
        "git",
        "-c",
        f"gitea admin user create --username {GITEA_USER} --password {GITEA_PASSWORD} "
        f"--email dev@corp.example --admin --must-change-password=false",
    )
    print("  user created" if code == 0 else "  user already exists")

    # Tokens cannot be read back, so an existing one is useless to us. Delete
    # and recreate, which keeps `up` idempotent without accumulating tokens.
    request(
        f"{GITEA}/api/v1/users/{GITEA_USER}/tokens/uione-estate",
        method="DELETE",
        basic=(GITEA_USER, GITEA_PASSWORD),
    )
    status, payload = request(
        f"{GITEA}/api/v1/users/{GITEA_USER}/tokens",
        method="POST",
        basic=(GITEA_USER, GITEA_PASSWORD),
        body={
            "name": "uione-estate",
            # write:user is needed to create the repository; the connector
            # itself only ever needs issue and repository scopes.
            "scopes": ["write:user", "write:issue", "write:repository"],
        },
    )
    if status not in (200, 201) or not isinstance(payload, dict):
        raise SystemExit(f"could not create a Gitea token: {status} {payload}")
    token = str(payload["sha1"])
    print(f"  token {token[:8]}…")

    status, _ = request(
        f"{GITEA}/api/v1/user/repos",
        method="POST",
        token=token,
        body={"name": REPO, "description": "Payments platform", "auto_init": True},
    )
    print("  repository created" if status in (200, 201) else "  repository already exists")

    existing = request(
        f"{GITEA}/api/v1/repos/{GITEA_USER}/{REPO}/issues?state=all&limit=50", token=token
    )[1]
    titles = {i.get("title") for i in existing} if isinstance(existing, list) else set()

    for title, body in [
        (
            "Settlement batch PAY-1182 failing on retry",
            "Batch fails when the acquirer returns a soft decline. Ops escalated at 07:40.",
        ),
        (
            "Add idempotency key to refund endpoint",
            "Duplicate refunds observed on client retries. PAY-1190 tracks the rollout.",
        ),
        (
            "Quarterly reconciliation report is late",
            "Report generation exceeded its window three days running.",
        ),
    ]:
        if title in titles:
            continue
        request(
            f"{GITEA}/api/v1/repos/{GITEA_USER}/{REPO}/issues",
            method="POST",
            token=token,
            body={"title": title, "body": body, "assignees": [GITEA_USER]},
        )
    print(f"  issues: {len(titles) or 3}")
    return token


# -- Grafana ---------------------------------------------------------------


def provision_grafana() -> str:
    print("\nGrafana")
    wait_for("  grafana", f"{GRAFANA}/api/health")
    admin = ("admin", GRAFANA_PASSWORD)

    status, accounts = request(f"{GRAFANA}/api/serviceaccounts/search?query=uione", basic=admin)
    found = accounts.get("serviceAccounts") if isinstance(accounts, dict) else None
    if found:
        account_id = found[0]["id"]
    else:
        # Viewer, deliberately. The connector has no write tool, and a token
        # that cannot silence an alert makes that guarantee structural rather
        # than a promise in a docstring.
        _, created = request(
            f"{GRAFANA}/api/serviceaccounts",
            method="POST",
            basic=admin,
            body={"name": "uione", "role": "Viewer"},
        )
        account_id = created["id"]
    print(f"  service account {account_id} (Viewer)")

    for token_info in (
        request(f"{GRAFANA}/api/serviceaccounts/{account_id}/tokens", basic=admin)[1] or []
    ):
        request(
            f"{GRAFANA}/api/serviceaccounts/{account_id}/tokens/{token_info['id']}",
            method="DELETE",
            basic=admin,
        )

    status, payload = request(
        f"{GRAFANA}/api/serviceaccounts/{account_id}/tokens",
        method="POST",
        basic=admin,
        body={"name": "uione-estate"},
    )
    if status not in (200, 201) or not isinstance(payload, dict):
        raise SystemExit(f"could not create a Grafana token: {status} {payload}")
    token = str(payload["key"])
    print(f"  token {token[:12]}…")

    datasource_uid = _grafana_datasource(admin)
    folder_uid = _grafana_folder(admin)
    _grafana_alert_rule(admin, datasource_uid, folder_uid)
    return token


def _grafana_datasource(admin: tuple[str, str]) -> str:
    status, existing = request(f"{GRAFANA}/api/datasources/name/testdata", basic=admin)
    if status == 200 and isinstance(existing, dict):
        return str(existing["uid"])

    _, created = request(
        f"{GRAFANA}/api/datasources",
        method="POST",
        basic=admin,
        body={
            "name": "testdata",
            "type": "grafana-testdata-datasource",
            "access": "proxy",
            "isDefault": True,
        },
    )
    return str(created["datasource"]["uid"])


def _grafana_folder(admin: tuple[str, str]) -> str:
    for folder in request(f"{GRAFANA}/api/folders", basic=admin)[1] or []:
        if folder.get("title") == "Payments":
            return str(folder["uid"])
    _, created = request(
        f"{GRAFANA}/api/folders", method="POST", basic=admin, body={"title": "Payments"}
    )
    return str(created["uid"])


def _grafana_alert_rule(admin: tuple[str, str], datasource_uid: str, folder_uid: str) -> None:
    title = "Settlement failure rate above threshold"
    for rule in request(f"{GRAFANA}/api/v1/provisioning/alert-rules", basic=admin)[1] or []:
        if rule.get("title") == title:
            print("  alert rule already present")
            return

    rule = {
        "title": title,
        "ruleGroup": "payments",
        "folderUID": folder_uid,
        "noDataState": "NoData",
        "execErrState": "Error",
        "for": "0s",
        "orgID": 1,
        "condition": "C",
        "labels": {"severity": "critical", "team": "payments"},
        "annotations": {
            "summary": "Settlement failures exceeded 5% for the last 10 minutes",
            "runbook_url": "http://wiki.local/runbooks/settlement",
        },
        "data": [
            {
                "refId": "A",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": datasource_uid,
                "model": {"refId": "A", "scenarioId": "random_walk", "startValue": 40, "spread": 1},
            },
            {
                "refId": "B",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "__expr__",
                "model": {"refId": "B", "type": "reduce", "reducer": "last", "expression": "A"},
            },
            {
                "refId": "C",
                "relativeTimeRange": {"from": 600, "to": 0},
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [{"evaluator": {"type": "gt", "params": [5]}}],
                },
            },
        ],
    }
    status, payload = request(
        f"{GRAFANA}/api/v1/provisioning/alert-rules",
        method="POST",
        basic=admin,
        body=rule,
        # Without this the rule is marked as provisioned and becomes read-only
        # in the UI, which is a poor demo: the first thing anyone does is open
        # the rule to see what it says.
        headers={"X-Disable-Provenance": "true"},
    )
    if status in (200, 201):
        print("  alert rule created (fires within ~60s)")
    else:
        print(f"  alert rule not created: {status} {payload}")


# -- Mattermost ------------------------------------------------------------


def _mm_login(username: str) -> str:
    """Log in and return the session token, which arrives in a *header*.

    Mattermost puts it in `Token`, not in the response body — the body is the
    user object. Reading the body for a token yields None and a confusing
    KeyError three calls later.
    """
    body = json.dumps({"login_id": username, "password": MM_PASSWORD}).encode()
    req = urllib.request.Request(f"{MATTERMOST}/api/v4/users/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.headers.get("Token", "")
    except (OSError, http.client.HTTPException):
        return ""


def _mm(
    path: str, *, method: str = "GET", body: object = None, token: str = ""
) -> tuple[int, object]:
    return request(
        f"{MATTERMOST}{path}",
        method=method,
        body=body,
        headers={"Authorization": f"Bearer {token}"} if token else None,
    )


def provision_mattermost() -> str:
    print("\nMattermost")
    # Slower than the others: the image is amd64-only, so on Apple silicon this
    # is an emulated first boot with a schema migration in it.
    wait_for("  mattermost", f"{MATTERMOST}/api/v4/system/ping", timeout_s=300)

    # The first account created on an open server becomes system admin. On a
    # second run this 400s because the username is taken, which is fine.
    _mm(
        "/api/v4/users",
        method="POST",
        body={"email": "uione@corp.example", "username": GITEA_USER, "password": MM_PASSWORD},
    )
    session = _mm_login(GITEA_USER)
    if not session:
        raise SystemExit("could not log in to Mattermost")

    me = _mm("/api/v4/users/me", token=session)[1]
    print(f"  user {me['username']}")

    # Tokens cannot be read back, so revoke and reissue, as elsewhere.
    for existing in _mm(f"/api/v4/users/{me['id']}/tokens", token=session)[1] or []:
        _mm(
            "/api/v4/users/tokens/revoke",
            method="POST",
            body={"token_id": existing["id"]},
            token=session,
        )
    status, payload = _mm(
        f"/api/v4/users/{me['id']}/tokens",
        method="POST",
        body={"description": "uione-estate"},
        token=session,
    )
    if status not in (200, 201) or not isinstance(payload, dict) or "token" not in payload:
        raise SystemExit(f"could not create a Mattermost token: {status} {payload}")
    token = str(payload["token"])
    print(f"  token {token[:10]}…")

    team = _seed_mattermost_team(session, me)
    _seed_mattermost_conversation(session, me, team)
    return token


def _seed_mattermost_team(session: str, me: dict) -> dict:
    status, team = _mm(
        "/api/v4/teams",
        method="POST",
        body={"name": "payments", "display_name": "Payments", "type": "O"},
        token=session,
    )
    if status not in (200, 201):
        for existing in _mm("/api/v4/users/me/teams", token=session)[1] or []:
            if existing.get("name") == "payments":
                return existing
        raise SystemExit("could not create or find the payments team")
    return team


def _seed_mattermost_conversation(session: str, me: dict, team: dict) -> None:
    status, channel = _mm(
        "/api/v4/channels",
        method="POST",
        body={
            "team_id": team["id"],
            "name": "payments-ops",
            "display_name": "Payments Ops",
            "type": "O",
        },
        token=session,
    )
    if status not in (200, 201):
        _, channel = _mm(f"/api/v4/teams/{team['id']}/channels/name/payments-ops", token=session)

    # Messages must come from somebody else — your own posts are read by
    # definition, so a single-user estate has nothing unread to demonstrate.
    status, other = _mm(
        "/api/v4/users",
        method="POST",
        body={"email": "bora@corp.example", "username": "bora", "password": MM_PASSWORD},
        token=session,
    )
    if status not in (200, 201):
        _, other = _mm("/api/v4/users/username/bora", token=session)
    _mm(
        f"/api/v4/teams/{team['id']}/members",
        method="POST",
        body={"team_id": team["id"], "user_id": other["id"]},
        token=session,
    )
    _mm(
        f"/api/v4/channels/{channel['id']}/members",
        method="POST",
        body={"user_id": other["id"]},
        token=session,
    )

    existing = _mm(f"/api/v4/channels/{channel['id']}/posts?per_page=50", token=session)[1]
    said = {p.get("message") for p in (existing or {}).get("posts", {}).values()}

    their_session = _mm_login("bora")
    for message in [
        "Acquirer confirmed a config change on their side at 06:10.",
        f"@{me['username']} can you take the settlement batch? PAY-1182 is still failing.",
        "I have paused the retry job until we hear back.",
    ]:
        if message not in said:
            _mm(
                "/api/v4/posts",
                method="POST",
                body={"channel_id": channel["id"], "message": message},
                token=their_session,
            )
    print("  #payments-ops seeded")


# -- output ----------------------------------------------------------------


def write_env(gitea_token: str, grafana_token: str, mattermost_token: str) -> None:
    ENV_FILE.write_text(
        "\n".join(
            [
                "# Written by scripts/estate.py. Gitignored, regenerated on every `up`.",
                "# Every credential here is disposable and every service is loopback-only.",
                "",
                f"UIONE_GITEA_URL={GITEA}",
                f"UIONE_GITEA_TOKEN={gitea_token}",
                "",
                f"UIONE_GRAFANA_URL={GRAFANA}",
                f"UIONE_GRAFANA_TOKEN={grafana_token}",
                "",
                f"UIONE_MATTERMOST_URL={MATTERMOST}",
                f"UIONE_MATTERMOST_TOKEN={mattermost_token}",
                "",
                "# Mocks — see docs/VENDOR_ACCESS.md for why these are not real systems.",
                "UIONE_SERVICENOW_URL=http://127.0.0.1:9102",
                "UIONE_SERVICENOW_USERNAME=uione",
                "UIONE_SERVICENOW_PASSWORD=mock",
                "UIONE_CLAIMS_URL=http://127.0.0.1:9103",
                "",
                "UIONE_TICKET_PREFIXES=PAY,INC,CLM",
                "UIONE_INTERNAL_DOMAINS=corp.example",
                "",
            ]
        )
    )
    ENV_FILE.chmod(0o600)
    print(f"\nWrote {ENV_FILE.name} (mode 600)")


def up() -> None:
    if compose("up", "-d") != 0:
        raise SystemExit("docker compose failed")

    gitea_token = provision_gitea()
    grafana_token = provision_grafana()
    mattermost_token = provision_mattermost()
    write_env(gitea_token, grafana_token, mattermost_token)

    print(
        "\nNext:\n"
        "  1. python -m uione.vendormocks          # the mocked half, in another shell\n"
        "  2. set -a; . ./.env.estate; set +a      # load the estate's settings\n"
        "  3. make run                             # http://127.0.0.1:8000/\n"
    )


def status() -> None:
    for name, url in [
        ("gitea", f"{GITEA}/api/v1/version"),
        ("grafana", f"{GRAFANA}/api/health"),
        ("mattermost", f"{MATTERMOST}/api/v4/system/ping"),
    ]:
        code, _ = request(url)
        print(f"  {name:10} {'up' if code == 200 else 'down'}  {url}")
    for name, port in [("servicenow", 9102), ("claims", 9103), ("gitea-mock", 9101)]:
        code, _ = request(f"http://127.0.0.1:{port}/")
        print(f"  {name:10} {'up' if code else 'down'}  http://127.0.0.1:{port}")
    print(f"  {'env':10} {'present' if ENV_FILE.exists() else 'absent'}  {ENV_FILE.name}")


def down() -> None:
    compose("down")
    print("Stopped. Data volumes kept — `destroy` removes them.")


def destroy() -> None:
    compose("down", "-v")
    if ENV_FILE.exists():
        ENV_FILE.unlink()
        print(f"Removed {ENV_FILE.name}")
    print("Estate destroyed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["up", "status", "down", "destroy"])
    args = parser.parse_args()
    {"up": up, "status": status, "down": down, "destroy": destroy}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
