"""The Mattermost chat connector.

Against a mock in CI, and against a real instance when `UIONE_TEST_MATTERMOST_URL`
and `UIONE_TEST_MATTERMOST_TOKEN` are set. Every awkward assertion below exists
because the real API does something a reasonable person would not have guessed.
"""

from __future__ import annotations

import os

import httpx
import pytest

from uione.connectors.chat import MattermostChat, build_mattermost_source, mattermost_config
from uione.mcphub import RiskClass
from uione.vendormocks.mattermost import State, build_mattermost_mock, seed_mattermost

REAL_URL = os.environ.get("UIONE_TEST_MATTERMOST_URL", "")
REAL_TOKEN = os.environ.get("UIONE_TEST_MATTERMOST_TOKEN", "")


def _chat(state: State | None = None) -> MattermostChat:
    app = build_mattermost_mock(state if state is not None else seed_mattermost())
    return MattermostChat(
        mattermost_config("http://mm.mock", "token"),
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mm.mock"),
    )


@pytest.fixture
def chat() -> MattermostChat:
    return _chat()


# -- unread is arithmetic, not a field -------------------------------------


async def test_channels_with_something_waiting_are_listed(chat: MattermostChat) -> None:
    result = await build_mattermost_source(chat).call("unread_messages", {})

    assert result.ok
    assert "#payments-ops" in result.content
    assert result.structured["mentions"] == 1


async def test_a_channel_read_up_to_date_is_not_unread(chat: MattermostChat) -> None:
    """There is no unread field. It is total_msg_count minus msg_count, and a
    connector that reports the total reports every message ever sent as unread,
    forever, with a number that only grows."""
    result = await build_mattermost_source(chat).call("unread_messages", {})

    assert "announcements" not in result.content


async def test_a_channel_that_mentions_you_comes_first(chat: MattermostChat) -> None:
    """Being named is a request. An unread channel is not."""
    result = await build_mattermost_source(chat).call("unread_messages", {})

    assert result.structured["labels"][0] == "#payments-ops"


async def test_nothing_unread_says_so_plainly() -> None:
    result = await build_mattermost_source(_chat(State())).call("unread_messages", {})

    assert result.ok
    assert result.structured["count"] == 0


# -- the shapes that mislead -----------------------------------------------


async def test_a_conversation_reads_in_the_order_it_happened(chat: MattermostChat) -> None:
    """Posts arrive as a map with the ordering in a separate list. Iterating the
    map compiles, runs, and produces a plausible but wrong account of who said
    what to whom."""
    result = await build_mattermost_source(chat).call("read_channel", {"channel": "#payments-ops"})

    content = result.content
    assert content.index("Acquirer confirmed") < content.index("settlement batch")
    assert content.index("settlement batch") < content.index("paused the retry job")


async def test_system_posts_are_not_rendered_as_things_people_said(
    chat: MattermostChat,
) -> None:
    """A real channel's history is full of joins and adds. Rendered verbatim
    they attribute "uione joined the channel" to uione, and in a channel with
    any churn they outnumber the conversation.

    Found by reading a real instance — a mock written from imagination would
    never have contained one.
    """
    result = await build_mattermost_source(chat).call("read_channel", {"channel": "#payments-ops"})

    assert "joined the channel" not in result.content


async def test_a_direct_message_is_named_after_the_other_person(
    chat: MattermostChat,
) -> None:
    """A DM channel has an empty display_name and a name of `id__id`. Rendering
    it verbatim shows a 52-character hex string where a colleague belongs."""
    result = await build_mattermost_source(chat).call("unread_messages", {})

    assert any(label.startswith("DM with bora") for label in result.structured["labels"])
    assert "__" not in result.content


async def test_authors_are_named_rather_than_shown_as_ids(chat: MattermostChat) -> None:
    result = await build_mattermost_source(chat).call("read_channel", {"channel": "#payments-ops"})

    assert "bora:" in result.content


# -- addressing ------------------------------------------------------------


async def test_a_channel_can_be_named_with_or_without_the_hash(
    chat: MattermostChat,
) -> None:
    source = build_mattermost_source(chat)

    with_hash = await source.call("read_channel", {"channel": "#payments-ops"})
    without = await source.call("read_channel", {"channel": "payments-ops"})

    assert with_hash.ok and without.ok
    assert with_hash.content == without.content


async def test_an_unknown_channel_fails_rather_than_guessing(chat: MattermostChat) -> None:
    result = await build_mattermost_source(chat).call("read_channel", {"channel": "#nope"})

    assert not result.ok
    assert "no channel" in (result.error or "")


# -- writing ---------------------------------------------------------------


async def test_a_message_reaches_the_channel(chat: MattermostChat) -> None:
    source = build_mattermost_source(chat)

    sent = await source.call(
        "send_message", {"channel": "#payments-ops", "message": "Taking PAY-1182 now."}
    )
    read = await source.call("read_channel", {"channel": "#payments-ops"})

    assert sent.ok
    assert "Taking PAY-1182 now." in read.content


async def test_an_empty_message_is_refused(chat: MattermostChat) -> None:
    result = await build_mattermost_source(chat).call(
        "send_message", {"channel": "#payments-ops", "message": "   "}
    )

    assert not result.ok


async def test_posting_can_never_be_merely_reversible(chat: MattermostChat) -> None:
    """Deleting a post does not unsend the notification that already reached
    everyone's phone."""
    specs = {s.tool: s for s in await build_mattermost_source(chat).list_tools()}

    assert specs["send_message"].risk is RiskClass.IRREVERSIBLE
    assert specs["unread_messages"].risk is RiskClass.READ
    assert specs["read_channel"].risk is RiskClass.READ


async def test_everything_read_from_chat_is_untrusted(chat: MattermostChat) -> None:
    """Anyone in a channel can write, including guests, and it goes straight
    into the model's context."""
    specs = {s.tool: s for s in await build_mattermost_source(chat).list_tools()}

    assert specs["unread_messages"].returns_untrusted_content
    assert specs["read_channel"].returns_untrusted_content


# -- against a real Mattermost, when there is one --------------------------

real_mm = pytest.mark.skipif(
    not (REAL_URL and REAL_TOKEN),
    reason="set UIONE_TEST_MATTERMOST_URL and UIONE_TEST_MATTERMOST_TOKEN for the real thing",
)


@pytest.fixture
async def live_chat():
    client = MattermostChat(mattermost_config(REAL_URL, REAL_TOKEN))
    yield client
    await client.aclose()


@real_mm
async def test_real_mattermost_authenticates(live_chat: MattermostChat) -> None:
    assert (await live_chat.me()).get("username")


@real_mm
async def test_real_mattermost_returns_posts_as_a_map_with_a_separate_order(
    live_chat: MattermostChat,
) -> None:
    """The assertion the mock cannot make about itself."""
    teams = await live_chat.teams()
    if not teams:
        pytest.skip("the live instance has no teams for this token")

    channel = await live_chat.find_channel(teams[0]["id"], "payments-ops")
    if channel is None:
        pytest.skip("the live instance has no payments-ops channel")

    posts = await live_chat.posts(channel["id"], limit=10)
    assert all(not str(p.get("type", "")).startswith("system_") for p in posts)
    assert [p["create_at"] for p in posts] == sorted(p["create_at"] for p in posts)


@real_mm
async def test_real_mattermost_unread_is_computed_not_reported(
    live_chat: MattermostChat,
) -> None:
    waiting = await live_chat.unread()

    for item in waiting:
        assert item["unread"] <= item["channel"]["total_msg_count"]
