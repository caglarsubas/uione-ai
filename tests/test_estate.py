"""The estate must not leak its own credentials.

`scripts/estate.py` writes real tokens to `.env.estate`. The failure this guards
against is not subtle — it is somebody committing that file — but it is silent,
and it happens at exactly the moment a reader is deciding whether to trust this
project with their mailbox.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_estate_env_file_is_ignored_by_git() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", ".env.estate"], cwd=ROOT, check=False)

    assert result.returncode == 0, ".env.estate holds live tokens and must be gitignored"


def test_no_estate_env_file_is_tracked() -> None:
    """The belt to the gitignore's braces: a file added with `-f` stays added."""
    tracked = subprocess.run(
        ["git", "ls-files", ".env.estate", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    assert not tracked, f"credential files are tracked: {tracked}"


def test_the_mock_estate_refuses_a_non_loopback_bind() -> None:
    """The mocks are unauthenticated on purpose so the estate needs no secrets.

    That is only acceptable while they cannot be reached from off the machine.
    """
    source = (ROOT / "src" / "uione" / "vendormocks" / "__main__.py").read_text()

    assert "refusing to bind" in source
    assert '"127.0.0.1", "localhost", "::1"' in source


def test_estate_ports_avoid_the_usual_developer_collisions() -> None:
    """3000 is occupied on most developer machines, and an estate that collides
    fails in a way that looks like a bug in this project."""
    compose = (ROOT / "estate" / "docker-compose.yml").read_text()

    assert "127.0.0.1:3300:3000" in compose
    assert "127.0.0.1:3400:3000" in compose
    assert '\n      - "3000:3000"' not in compose


def test_every_estate_service_restarts_itself() -> None:
    """A Docker Desktop restart must not leave half an estate running.

    Found from a screenshot of the product: `app` and `mocks` carried
    `restart: unless-stopped` and gitea, grafana and mattermost did not. The
    daemon went away overnight, the assistant came back, and three connectors
    stayed dead — so the product returned *looking healthy* while every call to
    a third of the estate failed with ConnectError.

    Half an estate coming back is worse than none coming back, because none is
    obvious and half is not.
    """
    import yaml

    for path in (ROOT / "compose.yaml", ROOT / "estate" / "docker-compose.yml"):
        services = yaml.safe_load(path.read_text())["services"]
        missing = sorted(n for n, s in services.items() if s.get("restart") != "unless-stopped")

        assert not missing, f"{path.name}: {missing} would not come back after a daemon restart"


def test_the_policy_is_unless_stopped_rather_than_always() -> None:
    """`docker compose stop` must still mean stop. `always` would fight the
    operator, which is how a demo estate becomes something people kill by hand."""
    import yaml

    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]

    assert {s.get("restart") for s in services.values()} == {"unless-stopped"}


def test_the_app_port_avoids_the_usual_developer_collisions() -> None:
    """The same reasoning as the estate ports above, applied to the app.

    That test has existed since the estate shipped, and the app was never
    covered by it — while `compose.yaml` carried a comment calling 8000 "the
    most contested port on any developer's machine" and then defaulted to it.

    It cost a real afternoon: `docker compose up -d` collided with an unrelated
    service on 8000 and failed with a wall of build output ending in `bind:
    address already in use`, which reads as a bug in this project rather than as
    two things wanting one port.
    """
    compose = (ROOT / "compose.yaml").read_text()

    assert "${UIONE_HTTP_PORT:-8800}:8000" in compose
    assert ":-8000}" not in compose, "the published default is the contested port again"


def test_the_makefile_and_compose_agree_on_the_default_port() -> None:
    """Otherwise `make up` prints a URL that nothing is listening on."""
    import re

    makefile = (ROOT / "Makefile").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    declared = re.search(r"UIONE_HTTP_PORT \?= (\d+)", makefile).group(1)
    published = re.search(r"\$\{UIONE_HTTP_PORT:-(\d+)\}", compose).group(1)

    assert declared == published
