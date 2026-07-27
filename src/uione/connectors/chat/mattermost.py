"""Mattermost — the messaging half of "reading and writing emails or messages".

Mattermost is a single Go binary an enterprise runs behind its own firewall, so
it is tier A: verified against a real instance rather than a fixture. Slack's
Web API is close enough in shape that this is also the cheapest way to find out
whether the abstraction holds before writing that one.

**Three things about this API that a first attempt gets wrong**, each of which
fails quietly rather than loudly:

*Posts arrive as a map, and the order is in a separate list.* The response is
`{"order": [id, id, ...], "posts": {id: post}}`. Iterating `posts.values()`
compiles, runs, and returns a conversation in arbitrary order — which reads as a
plausible but wrong summary of who said what to whom.

*There is no "unread count" field.* Unread is `channel.total_msg_count` minus
`member.msg_count`, computed per channel. A connector that reports
`total_msg_count` reports every message ever sent as unread, forever, and the
number only ever grows.

*A direct message channel has no display name.* Its `name` is
`{user_id}__{user_id}` and the human name has to be resolved from the *other*
participant. Rendering `display_name` verbatim shows a 52-character hex string
where a colleague's name belongs.

**What this connector is for, and what it refuses to be.** It answers "what did I
miss, and who needs me" — mentions and direct messages. It is not a firehose:
pulling every message from every channel someone belongs to fills a brief with
lunch plans and a model's context with noise. Channels are read on request, one
at a time.
"""

from __future__ import annotations

from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 20

#: Mattermost channel types. "O" open, "P" private, "D" direct, "G" group DM.
DIRECT_TYPES = frozenset({"D", "G"})

#: Posts Mattermost writes itself — joins, leaves, header changes, pinned
#: notices. A user post has an empty `type`. Rendering these attributes
#: "uione joined the channel" *to uione*, as though it were something a
#: colleague said, and in a channel with any churn they outnumber the
#: conversation. Found by reading a real instance; a fixture would never have
#: contained one.
SYSTEM_POST_PREFIX = "system_"


def mattermost_config(
    base_url: str, token: str, *, verify_tls: bool = True, timeout_s: float = 20.0
) -> VendorConfig:
    """A personal access token, presented as a Bearer credential."""
    return VendorConfig(
        name="mattermost",
        base_url=base_url.rstrip("/"),
        auth=Auth(scheme="bearer", secret=token),
        verify_tls=verify_tls,
        timeout_s=timeout_s,
        extra_headers={"Content-Type": "application/json"},
    )


class MattermostChat:
    def __init__(self, config: VendorConfig, **kwargs: Any) -> None:
        self._client = VendorClient(config, **kwargs)
        self._me: dict | None = None
        #: user_id → username, filled as channels are resolved. A direct message
        #: channel names its participants by id, and looking each up on every
        #: render would be one HTTP call per line of the brief.
        self._names: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def me(self) -> dict:
        if self._me is None:
            self._me = await self._client.get("/api/v4/users/me")
            self._names[self._me["id"]] = self._me.get("username", "me")
        return self._me

    async def teams(self) -> list[dict]:
        return list(await self._client.get("/api/v4/users/me/teams") or [])

    async def _resolve_names(self, user_ids: list[str]) -> None:
        """Fetch usernames for ids we have not seen, in one call.

        Mattermost takes a list, so this is one request however many names are
        missing — as opposed to the obvious loop, which is one per participant
        per channel and turns a brief into forty round trips.
        """
        missing = [uid for uid in set(user_ids) if uid and uid not in self._names]
        if not missing:
            return
        users = await self._client.post("/api/v4/users/ids", json_body=missing)
        for user in users or []:
            self._names[user["id"]] = user.get("username", user["id"][:8])

    async def unread(self, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Channels with something waiting, mentions first.

        Unread is arithmetic, not a field: the channel's total message count
        minus the count this member has read.
        """
        me = await self.me()
        results: list[dict] = []

        for team in await self.teams():
            members = await self._client.get(
                f"/api/v4/users/{me['id']}/teams/{team['id']}/channels/members"
            )
            read_counts = {m["channel_id"]: m for m in members or []}

            channels = await self._client.get(f"/api/v4/users/me/teams/{team['id']}/channels")
            for channel in channels or []:
                member = read_counts.get(channel["id"])
                if member is None:
                    continue
                unread = int(channel.get("total_msg_count", 0)) - int(member.get("msg_count", 0))
                mentions = int(member.get("mention_count", 0))
                if unread <= 0 and mentions <= 0:
                    continue
                results.append(
                    {
                        "channel": channel,
                        "team": team,
                        "unread": max(unread, 0),
                        "mentions": mentions,
                    }
                )

        # Direct messages carry participant ids rather than names.
        await self._resolve_names(
            [
                uid
                for r in results
                if r["channel"].get("type") in DIRECT_TYPES
                for uid in str(r["channel"].get("name", "")).split("__")
            ]
        )

        # Mentions first: being named is a request, an unread channel is not.
        results.sort(key=lambda r: (-r["mentions"], -r["unread"]))
        return results[:limit]

    async def posts(self, channel_id: str, *, limit: int = 20) -> list[dict]:
        """Recent posts, oldest first.

        The ordering is the point. `order` holds ids newest-first; `posts` is a
        map. Reading the map directly produces a conversation in whatever order
        the JSON happened to serialise.
        """
        payload = await self._client.get(
            f"/api/v4/channels/{channel_id}/posts", params={"per_page": limit}
        )
        posts = payload.get("posts") or {}
        order = payload.get("order") or []
        ordered = [
            posts[pid]
            for pid in reversed(order)
            if pid in posts and not str(posts[pid].get("type", "")).startswith(SYSTEM_POST_PREFIX)
        ]

        await self._resolve_names([p.get("user_id", "") for p in ordered])
        return ordered

    async def channel(self, channel_id: str) -> dict:
        return await self._client.get(f"/api/v4/channels/{channel_id}")

    async def find_channel(self, team_id: str, name: str) -> dict | None:
        """Look a channel up by the name a person types, without the leading #."""
        try:
            return await self._client.get(
                f"/api/v4/teams/{team_id}/channels/name/{name.lstrip('#')}"
            )
        except VendorError as exc:
            if exc.status == 404:
                return None
            raise

    async def post(self, channel_id: str, message: str) -> dict:
        return await self._client.post(
            "/api/v4/posts", json_body={"channel_id": channel_id, "message": message}
        )

    def name_of(self, user_id: str) -> str:
        return self._names.get(user_id, user_id[:8] if user_id else "?")

    def channel_label(self, channel: dict, *, me: str = "") -> str:
        """What to call a channel in a sentence a person reads.

        A direct message has no display name; its identity is whoever else is in
        it, which has to be dug out of the channel's `name`.
        """
        if channel.get("type") in DIRECT_TYPES:
            others = [uid for uid in str(channel.get("name", "")).split("__") if uid and uid != me]
            if others:
                return "DM with " + ", ".join(self.name_of(uid) for uid in others)
            return "DM"
        return "#" + str(channel.get("name", channel.get("display_name", "?")))


# -- the governed tools ----------------------------------------------------


def build_mattermost_source(chat: MattermostChat, *, name: str = "chat") -> InMemoryToolSource:
    source = InMemoryToolSource(name)

    async def unread_messages(args: dict) -> ToolResult:
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            me = await chat.me()
            waiting = await chat.unread(limit=limit)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not waiting:
            return ToolResult.success("Nothing unread in chat.", {"count": 0, "mentions": 0})

        lines = []
        for item in waiting:
            label = chat.channel_label(item["channel"], me=me["id"])
            parts = [f"{label}: {item['unread']} unread"]
            if item["mentions"]:
                parts.append(f"{item['mentions']} mention(s) of you")
            lines.append(" — ".join(parts))

        return ToolResult.success(
            "\n".join(lines),
            {
                "count": len(waiting),
                "mentions": sum(i["mentions"] for i in waiting),
                "channels": [i["channel"]["id"] for i in waiting],
                # The names a person would recognise, so a follow-up call can be
                # made without the model having to remember an opaque id.
                "labels": [chat.channel_label(i["channel"], me=me["id"]) for i in waiting],
            },
        )

    async def read_channel(args: dict) -> ToolResult:
        reference = str(args.get("channel", "")).strip()
        if not reference:
            return ToolResult.failure("channel is required")
        try:
            limit = max(1, min(int(args.get("limit", 20)), 50))
        except (TypeError, ValueError):
            limit = 20

        try:
            channel = await _resolve_channel(chat, reference)
            if channel is None:
                return ToolResult.failure(f"no channel {reference!r} visible to you")
            posts = await chat.posts(channel["id"], limit=limit)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not posts:
            return ToolResult.success("No messages in that channel.", {"count": 0})

        me = await chat.me()
        rendered = "\n".join(
            f"{chat.name_of(p.get('user_id', ''))}: {(p.get('message') or '')[:500]}" for p in posts
        )
        return ToolResult.success(
            f"{chat.channel_label(channel, me=me['id'])}\n{rendered}",
            {"count": len(posts), "channel_id": channel["id"]},
        )

    async def send_message(args: dict) -> ToolResult:
        reference = str(args.get("channel", "")).strip()
        message = str(args.get("message", "")).strip()
        if not reference:
            return ToolResult.failure("channel is required")
        if not message:
            return ToolResult.failure("message is required")

        try:
            channel = await _resolve_channel(chat, reference)
            if channel is None:
                return ToolResult.failure(f"no channel {reference!r} visible to you")
            posted = await chat.post(channel["id"], message)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        me = await chat.me()
        return ToolResult.success(
            f"Posted to {chat.channel_label(channel, me=me['id'])}.",
            {"post_id": posted.get("id"), "channel_id": channel["id"]},
        )

    channel_arg = {
        "type": "string",
        "description": "Channel name such as #payments-ops, or a channel id.",
    }

    source.register(
        "unread_messages",
        unread_messages,
        description="Channels with unread messages, those mentioning the user first.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 20."}},
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "read_channel",
        read_channel,
        description="Read recent messages in one channel, oldest first.",
        parameters={
            "type": "object",
            "properties": {
                "channel": channel_arg,
                "limit": {"type": "integer", "description": "1-50, default 20."},
            },
            "required": ["channel"],
        },
        risk=RiskClass.READ,
        # Anyone in the channel can write here, including guests, and a message
        # is read straight into the model's context.
        returns_untrusted_content=True,
    )
    source.register(
        "send_message",
        send_message,
        description="Post a message to a channel.",
        parameters={
            "type": "object",
            "properties": {"channel": channel_arg, "message": {"type": "string"}},
            "required": ["channel", "message"],
        },
        # Deleting a post does not unsend the notification that already reached
        # everyone's phone. This can never be merely reversible.
        risk=RiskClass.IRREVERSIBLE,
    )
    return source


async def _resolve_channel(chat: MattermostChat, reference: str) -> dict | None:
    """Accept "#name", "name", or an id."""
    text = reference.lstrip("#")
    # Mattermost ids are 26-character alphanumerics; a name that long is not a
    # name. Checked by shape rather than by trying the id endpoint first, which
    # would put a 404 in the vendor's logs for every lookup by name.
    if len(text) == 26 and text.isalnum():
        try:
            return await chat.channel(text)
        except VendorError as exc:
            if exc.status == 404:
                return None
            raise

    for team in await chat.teams():
        if found := await chat.find_channel(team["id"], text):
            return found
    return None
