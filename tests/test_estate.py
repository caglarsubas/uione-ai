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
