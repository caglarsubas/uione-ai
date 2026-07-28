"""Multi-turn conversation.

The bug this closes was found by using the product: the assistant asked "would
you like details on either?", the user said "yes", and it replied "what would you
like me to check?" — because `history` had been a parameter since the agent loop
was written and no caller had ever passed it. Every turn was a fresh
conversation.

Two of the tests here matter more than the rest. Trimming must never orphan a
tool result, or the engine rejects the replay. And taint must survive across
turns, because replaying history puts the same untrusted text back into context.
"""

from __future__ import annotations

import pytest

from uione.config import Settings
from uione.modelplane import ChatMessage
from uione.modelplane.types import ToolCall
from uione.storage import ConversationStore, Database


@pytest.fixture
async def store(tmp_path):
    database = Database(Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"))
    await database.create_schema()
    yield ConversationStore(database)
    await database.dispose()


def user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def assistant(text: str) -> ChatMessage:
    return ChatMessage(role="assistant", content=text)


# -- the bug -----------------------------------------------------------------


async def test_a_second_turn_can_see_the_first(store: ConversationStore) -> None:
    """ "Would you like details?" / "yes" has to resolve to something."""
    await store.append("alice", [user("incidents"), assistant("Two are open. Details?")])

    history, _ = await store.history("alice")

    assert [m.content for m in history] == ["incidents", "Two are open. Details?"]


async def test_conversations_do_not_bleed_between_people(store: ConversationStore) -> None:
    await store.append("alice", [user("my incidents")])
    await store.append("bob", [user("my claims")])

    alice, _ = await store.history("alice")

    assert [m.content for m in alice] == ["my incidents"]


async def test_starting_again_forgets(store: ConversationStore) -> None:
    await store.append("alice", [user("one"), assistant("two")])

    cleared = await store.clear("alice")
    history, _ = await store.history("alice")

    assert cleared == 2
    assert history == []


async def test_an_empty_conversation_is_not_an_error(store: ConversationStore) -> None:
    history, tainted = await store.history("nobody")

    assert history == []
    assert tainted is False


async def test_order_is_preserved_across_appends(store: ConversationStore) -> None:
    """Sequence, not timestamp: two messages written in the same millisecond
    must still come back in the order they were said."""
    for i in range(12):
        await store.append("alice", [user(f"m{i}")])

    history, _ = await store.history("alice")

    assert [m.content for m in history] == [f"m{i}" for i in range(12)]


# -- trimming ----------------------------------------------------------------


async def test_history_is_bounded(store: ConversationStore) -> None:
    """One tool result can be a whole document, so the budget is characters
    rather than a message count."""
    for _ in range(50):
        await store.append("alice", [user("x" * 500), assistant("y" * 500)])

    history, _ = await store.history("alice", budget=4000)

    assert history, "some history must survive"
    assert sum(len(m.content or "") for m in history) <= 4200


async def test_the_most_recent_turns_are_the_ones_kept(store: ConversationStore) -> None:
    for i in range(20):
        await store.append("alice", [user(f"turn-{i}")])

    history, _ = await store.history("alice", budget=400)

    assert history[-1].content == "turn-19"
    assert "turn-0" not in [m.content for m in history]


async def test_trimming_never_leaves_an_orphan_tool_result(store: ConversationStore) -> None:
    """The trap. An OpenAI-compatible engine rejects a `tool` message whose
    assistant parent is missing, so a naive "keep the last N" produces a 400 the
    first time the cut lands mid-pair — which is exactly when a conversation has
    got long enough to matter.
    """
    await store.append("alice", [user("padding " * 400)])
    await store.append(
        "alice",
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="mail.list_unread", arguments="{}")],
            ),
            ChatMessage(role="tool", tool_call_id="c1", name="mail.list_unread", content="5"),
            assistant("You have five."),
        ],
    )

    history, _ = await store.history("alice", budget=200)

    assert history, "something must survive"
    assert history[0].role != "tool", "a tool result cannot lead the replay"


async def test_a_kept_tool_call_keeps_its_result(store: ConversationStore) -> None:
    """Cutting from the front means an assistant turn that survives still has
    its results behind it — this asserts the pairing rather than assuming it."""
    await store.append(
        "alice",
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="mail.list_unread", arguments="{}")],
            ),
            ChatMessage(role="tool", tool_call_id="c1", name="mail.list_unread", content="5"),
        ],
    )

    history, _ = await store.history("alice")

    calls = [m for m in history if m.tool_calls]
    results = {m.tool_call_id for m in history if m.role == "tool"}
    for message in calls:
        for call in message.tool_calls:
            assert call.id in results, "an assistant tool call must keep its result"


async def test_tool_calls_survive_the_round_trip(store: ConversationStore) -> None:
    await store.append(
        "alice",
        [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="tasks.my_open_issues", arguments='{"limit":5}')
                ],
            ),
        ],
    )

    history, _ = await store.history("alice")

    call = history[0].tool_calls[0]
    assert (call.id, call.name, call.arguments) == (
        "c1",
        "tasks.my_open_issues",
        '{"limit":5}',
    )


# -- taint -------------------------------------------------------------------


async def test_taint_survives_into_the_next_turn(store: ConversationStore) -> None:
    """The security half. Replaying history puts the same untrusted text back
    into the context window, so a conversation that read a poisoned email on
    turn one is still carrying it on turn three — and must not report clean.
    """
    await store.append(
        "alice",
        [user("read my mail"), ChatMessage(role="tool", content="…", tool_call_id="c1")],
        tainted=True,
    )

    _, tainted = await store.history("alice")

    assert tainted is True


async def test_a_clean_conversation_reports_clean(store: ConversationStore) -> None:
    await store.append("alice", [user("hello"), assistant("hi")])

    _, tainted = await store.history("alice")

    assert tainted is False


async def test_starting_again_clears_the_taint(store: ConversationStore) -> None:
    """The only way out of a tainted conversation is a new one."""
    await store.append(
        "alice",
        [ChatMessage(role="tool", content="poisoned", tool_call_id="c1")],
        tainted=True,
    )

    await store.clear("alice")
    _, tainted = await store.history("alice")

    assert tainted is False


async def test_only_tool_output_carries_taint(store: ConversationStore) -> None:
    """A user's own message is not untrusted content, whatever else the turn
    touched — marking it so would make every later turn of every conversation
    look tainted."""
    await store.append("alice", [user("what did that email say?")], tainted=True)

    _, tainted = await store.history("alice")

    assert tainted is False


# -- the runtime seeds from it ----------------------------------------------


async def test_the_runtime_accepts_a_pre_existing_taint() -> None:
    from uione.governance.containment import TaintTracker

    assert TaintTracker(tainted=True).tainted is True
    assert TaintTracker().tainted is False
