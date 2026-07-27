"""Mail as an ingestion source.

Mail is the right first source to mirror permissions from, because its access
model is unambiguous: a personal mailbox is readable by exactly one person. That
makes it the case where a mistake is obvious rather than subtle — if a colleague's
message ever surfaces in someone else's search, the bug is visible immediately.

Shared mailboxes and distribution lists are a different, harder problem, and are
deliberately not handled here rather than approximated.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from uione.connectors.mail.backend import MailBackend
from uione.knowledge.documents import AccessControl, Document
from uione.knowledge.ingest import CallableSource

log = structlog.get_logger(__name__)


def build_mail_ingestion(
    backend: MailBackend, *, owner_id: str, name: str = "mail", limit: int = 50
) -> CallableSource:
    """Index a personal mailbox, readable only by its owner."""

    async def fetch(since: datetime | None) -> list[Document]:
        # `since` is accepted for the incremental contract but not yet used: the
        # backend has no server-side date filter, and pretending otherwise would
        # silently return the same messages while claiming to be incremental.
        messages = await backend.list_unread(limit=limit)

        return [
            Document(
                id=f"mail:{owner_id}:{message.uid}",
                title=message.subject or "(no subject)",
                body=message.body,
                source=name,
                # The whole point: one mailbox, one reader. Not a group, not a
                # role — the individual, so no role change can widen it.
                acl=AccessControl.for_users(owner_id),
                updated_at=message.date or datetime.now(tz=None).astimezone(),
                metadata={
                    "from": message.from_address,
                    "external": message.external,
                    "uid": message.uid,
                },
            )
            for message in messages
        ]

    # No ACL reader: a personal mailbox's owner does not change. Returning None
    # rather than an empty mapping is what stops re-sync deleting the lot.
    return CallableSource(name=name, fetcher=fetch)
