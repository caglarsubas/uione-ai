"""Database commands for an operator.

Deliberately four commands and no more. Everything here is something a person
runs on a machine they cannot easily restore, so each one either does an obvious
thing or refuses.

**There is no downgrade.** Alembic offers one and this does not expose it. The
initial revision's downgrade drops every table — the audit log, the approval
history, the undo journal — and a flag that destroys those is a flag somebody
eventually passes at 3am while trying to fix something else. An operator who
needs to go back restores a backup, which is a decision made deliberately.

Run as ``python -m uione.storage.cli <command>``.
"""

from __future__ import annotations

import asyncio
import sys

from uione.config import get_settings
from uione.storage.database import Database, _safe_url, head_revision

USAGE = """usage: python -m uione.storage.cli <command>

  status    what revision this database is at, and whether that is current
  upgrade   run every outstanding migration
  stamp     record the database as current WITHOUT running anything
  sql       print the SQL an upgrade would run, and change nothing

`stamp` is for one situation: a deployment that predates migrations and whose
schema already matches. Using it on a database that is genuinely behind marks it
current while leaving it broken, and the failure appears later somewhere else.
"""


async def _status() -> int:
    database = Database()
    try:
        current = await database.current_revision()
        head = head_revision()
        print(f"database: {_safe_url(get_settings().database_url)}")
        print(f"current:  {current or '(none — never migrated)'}")
        print(f"head:     {head}")
        if current == head:
            print("\nup to date")
            return 0
        if current is None:
            print("\nnot initialised. Run: python -m uione.storage.cli upgrade")
        else:
            print("\nbehind. Run: python -m uione.storage.cli upgrade")
        return 1
    finally:
        await database.dispose()


async def _upgrade() -> int:
    database = Database()
    try:
        before = await database.current_revision()
        after = await database.upgrade()
        if before == after:
            print(f"already at {after}; nothing to do")
        else:
            print(f"upgraded {before or '(empty)'} -> {after}")
        return 0
    finally:
        await database.dispose()


async def _stamp() -> int:
    database = Database()
    try:
        current = await database.current_revision()
        if current is not None:
            print(f"already stamped at {current}; refusing to overwrite")
            return 1
        await database.stamp()
        print(f"stamped as {head_revision()} without running any migration")
        return 0
    finally:
        await database.dispose()


def _sql() -> int:
    """Print the statements instead of running them.

    The mode a change-controlled datacentre uses: a DBA reviews the SQL before
    anything touches production.
    """
    from alembic import command

    from uione.storage.database import _alembic_config

    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(config, "head", sql=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv[1:]) or []
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    match args[0]:
        case "status":
            return asyncio.run(_status())
        case "upgrade":
            return asyncio.run(_upgrade())
        case "stamp":
            return asyncio.run(_stamp())
        case "sql":
            return _sql()
        case unknown:
            print(f"unknown command {unknown!r}\n\n{USAGE}", file=sys.stderr)
            return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
