"""IMAP protocol handling.

Exercised against a fake ``imaplib`` client rather than a live server, because the
bugs that matter here are protocol-level: using sequence numbers instead of UIDs,
misreading flags, losing ordering. A real server would confirm connectivity and
hide exactly these mistakes.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from uione.connectors.mail import MailAccount, MailError
from uione.connectors.mail.imap_backend import ImapMailBackend

INTERNAL = frozenset({"corp.example"})


def raw(subject: str, sender: str = "cfo@corp.example") -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Mon, 27 Jul 2026 08:30:00 +0000"
    message.set_content(f"Body of {subject}")
    return message.as_bytes()


class FakeImap:
    """Records commands so the test can assert on the protocol, not just results."""

    def __init__(self, *, messages: dict[str, tuple[bytes, bytes]] | None = None) -> None:
        # uid -> (flags, raw)
        self.messages = messages or {}
        self.commands: list[tuple] = []
        self.selected: str | None = None
        self.readonly: bool | None = None
        self.closed = False
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        self.readonly = readonly
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            criteria = args[1:]
            if "UNSEEN" in criteria:
                hits = [u for u, (flags, _) in self.messages.items() if b"\\Seen" not in flags]
            else:
                hits = list(self.messages)
            return "OK", [" ".join(sorted(hits, key=int)).encode()]
        if command == "FETCH":
            uid = args[0]
            if uid not in self.messages:
                return "OK", [None]
            flags, body = self.messages[uid]
            return "OK", [(flags, body)]
        if command == "STORE":
            uid = args[0]
            if uid in self.messages:
                flags, body = self.messages[uid]
                self.messages[uid] = (flags + b" \\Seen", body)
                return "OK", [b""]
            return "NO", [b"no such uid"]
        return "NO", [b"unknown"]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


@pytest.fixture
def account() -> MailAccount:
    return MailAccount(
        host="mail.corp.example",
        username="alice@corp.example",
        password="secret",
        internal_domains=INTERNAL,
    )


def backend_with(account: MailAccount, fake: FakeImap) -> ImapMailBackend:
    backend = ImapMailBackend(account)
    backend._connect = lambda: fake  # type: ignore[method-assign]
    return backend


# -- UID discipline --------------------------------------------------------


async def test_reads_use_uid_commands_not_sequence_numbers(account: MailAccount) -> None:
    """Sequence numbers are renumbered on expunge; a stale one hits another message."""
    fake = FakeImap(messages={"41": (b"", raw("First"))})

    await backend_with(account, fake).list_unread()

    assert all(cmd[0] in {"SEARCH", "FETCH"} for cmd in fake.commands)
    assert ("FETCH", "41", "(RFC822 FLAGS)") in fake.commands


async def test_mark_read_stores_against_the_uid(account: MailAccount) -> None:
    fake = FakeImap(messages={"41": (b"", raw("First"))})

    await backend_with(account, fake).mark_read("41")

    assert ("STORE", "41", "+FLAGS", "(\\Seen)") in fake.commands
    assert b"\\Seen" in fake.messages["41"][0]


async def test_mark_read_opens_the_mailbox_writable(account: MailAccount) -> None:
    """A read-only SELECT would make the STORE silently ineffective on some servers."""
    fake = FakeImap(messages={"41": (b"", raw("x"))})

    await backend_with(account, fake).mark_read("41")

    assert fake.readonly is False


async def test_reads_open_the_mailbox_readonly(account: MailAccount) -> None:
    fake = FakeImap(messages={"41": (b"", raw("x"))})

    await backend_with(account, fake).list_unread()

    assert fake.readonly is True


# -- flags and ordering ----------------------------------------------------


async def test_seen_flag_is_respected(account: MailAccount) -> None:
    fake = FakeImap(
        messages={
            "1": (b"\\Seen", raw("Read one")),
            "2": (b"", raw("Unread one")),
        }
    )

    messages = await backend_with(account, fake).list_unread()

    assert [m.subject for m in messages] == ["Unread one"]


async def test_newest_first_ordering(account: MailAccount) -> None:
    fake = FakeImap(messages={str(i): (b"", raw(f"Message {i}")) for i in range(1, 6)})

    messages = await backend_with(account, fake).list_unread()

    assert [m.uid for m in messages] == ["5", "4", "3", "2", "1"]


async def test_limit_takes_the_newest_not_the_oldest(account: MailAccount) -> None:
    fake = FakeImap(messages={str(i): (b"", raw(f"Message {i}")) for i in range(1, 11)})

    messages = await backend_with(account, fake).list_unread(limit=3)

    assert [m.uid for m in messages] == ["10", "9", "8"]


# -- search ----------------------------------------------------------------


async def test_search_quotes_the_query(account: MailAccount) -> None:
    fake = FakeImap(messages={"1": (b"", raw("x"))})

    await backend_with(account, fake).search('budget" (DELETED) "')

    search = next(c for c in fake.commands if c[0] == "SEARCH")
    assert search[-1].startswith('"') and search[-1].endswith('"')
    assert '\\"' in search[-1]


async def test_search_uses_the_text_criterion(account: MailAccount) -> None:
    fake = FakeImap(messages={"1": (b"", raw("x"))})

    await backend_with(account, fake).search("budget")

    assert ("SEARCH", None, "TEXT", '"budget"') in fake.commands


# -- input validation ------------------------------------------------------


@pytest.mark.parametrize("bad_uid", ["1 OR 1", "abc", "*", "1:100", ""])
async def test_non_numeric_uids_are_rejected(account: MailAccount, bad_uid: str) -> None:
    """UIDs are numeric; anything else is a model error or an injection attempt."""
    fake = FakeImap()
    backend = backend_with(account, fake)

    with pytest.raises(MailError):
        await backend.get_message(bad_uid)

    assert fake.commands == []


async def test_non_numeric_uid_rejected_before_a_store(account: MailAccount) -> None:
    fake = FakeImap()

    with pytest.raises(MailError):
        await backend_with(account, fake).mark_read("1 2 3")

    assert fake.commands == []


# -- resilience ------------------------------------------------------------


async def test_missing_fetch_result_is_skipped_not_fatal(account: MailAccount) -> None:
    """Servers occasionally return OK with no envelope; one gap is not an outage."""
    fake = FakeImap(messages={"1": (b"", raw("Present"))})
    original = fake.uid

    def flaky(command, *args):
        if command == "SEARCH":
            return "OK", [b"1 2"]
        return original(command, *args)

    fake.uid = flaky  # type: ignore[method-assign]

    messages = await backend_with(account, fake).list_unread()

    assert [m.subject for m in messages] == ["Present"]


async def test_connection_is_always_released(account: MailAccount) -> None:
    fake = FakeImap(messages={"1": (b"", raw("x"))})

    await backend_with(account, fake).list_unread()

    assert fake.closed and fake.logged_out


async def test_connection_released_even_when_the_operation_fails(account: MailAccount) -> None:
    fake = FakeImap()
    fake.select = lambda *_a, **_k: ("NO", [b"nope"])  # type: ignore[method-assign]

    with pytest.raises(MailError):
        await backend_with(account, fake).list_unread()

    assert fake.logged_out


async def test_external_senders_are_classified_from_account_config(account: MailAccount) -> None:
    fake = FakeImap(
        messages={
            "1": (b"", raw("Internal", "cfo@corp.example")),
            "2": (b"", raw("External", "supplier@outside.example")),
        }
    )

    messages = await backend_with(account, fake).list_unread()

    by_subject = {m.subject: m.external for m in messages}
    assert by_subject == {"Internal": False, "External": True}
