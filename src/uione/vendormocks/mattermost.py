"""A Mattermost-shaped chat API.

**Checked against a real Mattermost 10.5 instance**, which is why the awkward
parts are here rather than a tidier invention:

* `GET /channels/{id}/posts` returns `{"order": [id...], "posts": {id: post}}` —
  a map plus a separate newest-first ordering, not a list;
* unread is not a field. It is `channel.total_msg_count` minus
  `member.msg_count`, per channel, and `mention_count` is separate again;
* a direct-message channel has an empty `display_name` and a `name` of
  `{user_id}__{user_id}`;
* the channel history is full of `system_join_channel` and `system_add_to_channel`
  posts that a naive reader renders as though a colleague had said them.

That last one was found by reading a real instance and would never have appeared
in a mock written from imagination — which is the argument for tier-A systems in
one line.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

#: Mattermost timestamps are milliseconds since the epoch, as integers.
EPOCH_MS = 1785225600000


class State:
    def __init__(self) -> None:
        self.me = {"id": "u" + "1" * 25, "username": "uione", "email": "dev@corp.example"}
        self.users: dict[str, dict] = {self.me["id"]: self.me}
        self.teams: list[dict] = []
        self.channels: dict[str, dict] = {}
        self.members: dict[str, dict] = {}
        self.posts: dict[str, list[dict]] = {}

    def add_user(self, username: str) -> dict:
        user_id = "u" + str(len(self.users)).zfill(25)
        user = {"id": user_id, "username": username, "email": f"{username}@corp.example"}
        self.users[user_id] = user
        return user

    def add_team(self, name: str, display_name: str = "") -> dict:
        team = {
            "id": "t" + str(len(self.teams)).zfill(25),
            "name": name,
            "display_name": display_name or name.title(),
        }
        self.teams.append(team)
        return team

    def add_channel(
        self,
        team: dict,
        name: str,
        *,
        display_name: str = "",
        kind: str = "O",
        total: int = 0,
        read: int = 0,
        mentions: int = 0,
    ) -> dict:
        channel_id = "c" + str(len(self.channels)).zfill(25)
        channel = {
            "id": channel_id,
            "team_id": team["id"],
            "name": name,
            # Empty for direct messages, exactly as a real instance leaves it.
            "display_name": display_name if kind == "O" else "",
            "type": kind,
            "total_msg_count": total,
        }
        self.channels[channel_id] = channel
        self.members[channel_id] = {
            "channel_id": channel_id,
            "user_id": self.me["id"],
            "msg_count": read,
            "mention_count": mentions,
        }
        self.posts.setdefault(channel_id, [])
        return channel

    def add_post(self, channel_id: str, user_id: str, message: str, *, kind: str = "") -> dict:
        posts = self.posts.setdefault(channel_id, [])
        post = {
            "id": "p" + f"{channel_id[-3:]}{len(posts)}".zfill(25),
            "channel_id": channel_id,
            "user_id": user_id,
            "message": message,
            "type": kind,
            "create_at": EPOCH_MS + len(posts) * 60000,
        }
        posts.append(post)
        if not kind:
            self.channels[channel_id]["total_msg_count"] += 1
        return post


class PostBody(BaseModel):
    channel_id: str
    message: str


def build_mattermost_mock(state: State | None = None) -> FastAPI:
    app = FastAPI(title="mock-mattermost")
    app.state.data = state or State()

    def data(request: Request) -> State:
        return request.app.state.data

    @app.get("/api/v4/system/ping")
    async def ping() -> dict:
        return {"status": "OK"}

    @app.get("/api/v4/users/me")
    async def me(request: Request) -> dict:
        if not request.headers.get("Authorization"):
            raise HTTPException(status_code=401, detail="token required")
        return data(request).me

    @app.post("/api/v4/users/ids")
    async def users_by_id(request: Request, ids: list[str]) -> list[dict]:
        store = data(request)
        return [store.users[i] for i in ids if i in store.users]

    @app.get("/api/v4/users/me/teams")
    async def my_teams(request: Request) -> list[dict]:
        return data(request).teams

    @app.get("/api/v4/users/me/teams/{team_id}/channels")
    async def my_channels(request: Request, team_id: str) -> list[dict]:
        return [c for c in data(request).channels.values() if c["team_id"] == team_id]

    @app.get("/api/v4/users/{user_id}/teams/{team_id}/channels/members")
    async def my_memberships(request: Request, user_id: str, team_id: str) -> list[dict]:
        store = data(request)
        return [
            m
            for cid, m in store.members.items()
            if store.channels[cid]["team_id"] == team_id and m["user_id"] == user_id
        ]

    @app.get("/api/v4/channels/{channel_id}")
    async def channel(request: Request, channel_id: str) -> dict:
        found = data(request).channels.get(channel_id)
        if found is None:
            raise HTTPException(status_code=404, detail="channel not found")
        return found

    @app.get("/api/v4/teams/{team_id}/channels/name/{name}")
    async def channel_by_name(request: Request, team_id: str, name: str) -> dict:
        for found in data(request).channels.values():
            if found["team_id"] == team_id and found["name"] == name:
                return found
        raise HTTPException(status_code=404, detail="channel not found")

    @app.get("/api/v4/channels/{channel_id}/posts")
    async def channel_posts(request: Request, channel_id: str, per_page: int = Query(60)) -> dict:
        store = data(request)
        if channel_id not in store.channels:
            raise HTTPException(status_code=404, detail="channel not found")
        posts = store.posts.get(channel_id, [])[-per_page:]
        return {
            # Newest first, as a real instance returns it. A connector that
            # trusts the map's iteration order instead is only accidentally
            # right.
            "order": [p["id"] for p in reversed(posts)],
            "posts": {p["id"]: p for p in posts},
        }

    @app.post("/api/v4/posts", status_code=201)
    async def create_post(request: Request, body: PostBody) -> dict:
        store = data(request)
        if body.channel_id not in store.channels:
            raise HTTPException(status_code=404, detail="channel not found")
        return store.add_post(body.channel_id, store.me["id"], body.message)

    return app


def seed_mattermost(state: State | None = None) -> State:
    """A morning's worth of chat: one channel that needs an answer, one that
    does not, and a direct message."""
    store = state or State()
    bora = store.add_user("bora")
    team = store.add_team("payments", "Payments")

    ops = store.add_channel(team, "payments-ops", display_name="Payments Ops", read=0, mentions=1)
    store.add_post(
        ops["id"], store.me["id"], "uione joined the channel.", kind="system_join_channel"
    )
    store.add_post(ops["id"], bora["id"], "Acquirer confirmed a config change at 06:10.")
    store.add_post(
        ops["id"], bora["id"], "@uione can you take the settlement batch? PAY-1182 still failing."
    )
    store.add_post(ops["id"], bora["id"], "I have paused the retry job until we hear back.")

    random_channel = store.add_channel(team, "random", display_name="Random", read=0)
    store.add_post(random_channel["id"], bora["id"], "Coffee machine is fixed.")

    direct = store.add_channel(team, f"{store.me['id']}__{bora['id']}", kind="D", read=0)
    store.add_post(direct["id"], bora["id"], "Are you joining the 09:30 bridge?")

    quiet = store.add_channel(team, "announcements", display_name="Announcements", total=4, read=4)
    store.add_post(quiet["id"], bora["id"], "Nothing new here.")
    # Read up to date, so this must not appear as unread.
    store.members[quiet["id"]]["msg_count"] = store.channels[quiet["id"]]["total_msg_count"]
    return store
