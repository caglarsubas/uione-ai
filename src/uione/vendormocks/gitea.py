"""A Gitea-shaped issue API.

Written from the Gitea v1 Swagger definition (`/api/swagger`), and — unusually
for the mocks in this package — **checked against a real Gitea 1.24 instance**.
The payloads below are the shapes that instance actually returned, which is why
this one is the reference for what a good mock looks like: same field names, same
nesting, same date format, same status codes for the same mistakes.

That check matters more than it sounds. The first draft of this file returned
`{"repo": ...}` on each issue, because that is the obvious name. Gitea returns
`repository`, and a connector written against the obvious name would have passed
every test here and failed on contact with the product it claims to support.

Kept deliberately small: the endpoints this product calls, not the API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

#: Fixed so a mock estate looks the same every morning. A demo whose data drifts
#: with the wall clock is one where "yesterday's incident" eventually becomes
#: "the incident from three months ago".
EPOCH = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _stamp(offset_minutes: int = 0) -> str:
    return (EPOCH + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """Everything the mock knows, in memory."""

    def __init__(self) -> None:
        self.user = {"id": 1, "login": "uione", "full_name": "UiOne", "email": "dev@corp.example"}
        self.issues: dict[tuple[str, str, int], dict] = {}
        self.comments: dict[tuple[str, str, int], list[dict]] = {}
        self.next_id = 1

    def add_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        *,
        state: str = "open",
        labels: list[str] | None = None,
        assignee: str | None = None,
        minutes: int = 0,
    ) -> dict:
        number = 1 + sum(1 for (o, r, _) in self.issues if (o, r) == (owner, repo))
        issue = {
            "id": self.next_id,
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "html_url": f"http://gitea.local/{owner}/{repo}/issues/{number}",
            "created_at": _stamp(minutes),
            "updated_at": _stamp(minutes),
            "comments": 0,
            "user": dict(self.user),
            "assignees": [{"login": assignee}] if assignee else [],
            "labels": [{"id": i, "name": n} for i, n in enumerate(labels or [], start=1)],
            "milestone": None,
            "due_date": None,
            # The field a connector written from intuition would call "repo".
            "repository": {
                "id": 1,
                "name": repo,
                "owner": owner,
                "full_name": f"{owner}/{repo}",
            },
        }
        self.next_id += 1
        self.issues[(owner, repo, number)] = issue
        return issue

    def add_comment(self, owner: str, repo: str, number: int, body: str, author: str) -> dict:
        key = (owner, repo, number)
        comment = {
            "id": len(self.comments.get(key, [])) + 1,
            "body": body,
            "user": {"login": author},
            "created_at": _stamp(),
            "html_url": f"http://gitea.local/{owner}/{repo}/issues/{number}#comment",
        }
        self.comments.setdefault(key, []).append(comment)
        if issue := self.issues.get(key):
            issue["comments"] = len(self.comments[key])
        return comment


class IssuePatch(BaseModel):
    state: str | None = None
    title: str | None = None
    body: str | None = None


class CommentBody(BaseModel):
    body: str


def build_gitea_mock(state: State | None = None) -> FastAPI:
    app = FastAPI(title="mock-gitea", docs_url="/api/swagger")
    app.state.data = state or State()

    def data(request: Request) -> State:
        return request.app.state.data

    @app.get("/api/v1/version")
    async def version() -> dict:
        return {"version": "1.24.7-mock"}

    @app.get("/api/v1/user")
    async def whoami(request: Request) -> dict:
        # A real Gitea 401s without a token. The mock accepts anything, so the
        # estate needs no secret — but it still *reads* the header, so a
        # connector that forgets to send one is caught here rather than in
        # production.
        if not request.headers.get("Authorization"):
            raise HTTPException(status_code=401, detail="token required")
        return data(request).user

    @app.get("/api/v1/repos/issues/search")
    async def search(
        request: Request,
        state: str = Query("open"),
        assigned: bool = Query(False),
        created_by: bool = Query(False),
        limit: int = Query(20),
    ) -> list[dict]:
        """Gitea's cross-repository search.

        `assigned` and `created_by` are separate filters that do not combine —
        passing both returns issues matching *neither* on a real instance, which
        is why the connector makes two calls and merges. Reproduced here so that
        behaviour stays tested.
        """
        store = data(request)
        issues = [i for i in store.issues.values() if state == "all" or i["state"] == state]

        me = store.user["login"]
        if assigned and created_by:
            return []
        if assigned:
            issues = [i for i in issues if any(a["login"] == me for a in i["assignees"])]
        elif created_by:
            issues = [i for i in issues if i["user"]["login"] == me]

        issues.sort(key=lambda i: i["updated_at"], reverse=True)
        return issues[:limit]

    @app.get("/api/v1/repos/{owner}/{repo}/issues/{number}")
    async def get_issue(request: Request, owner: str, repo: str, number: int) -> dict:
        issue = data(request).issues.get((owner, repo, number))
        if issue is None:
            raise HTTPException(status_code=404, detail="issue does not exist")
        return issue

    @app.get("/api/v1/repos/{owner}/{repo}/issues/{number}/comments")
    async def list_comments(
        request: Request, owner: str, repo: str, number: int, limit: int = Query(10)
    ) -> list[dict]:
        return data(request).comments.get((owner, repo, number), [])[:limit]

    @app.patch("/api/v1/repos/{owner}/{repo}/issues/{number}")
    async def patch_issue(
        request: Request, owner: str, repo: str, number: int, patch: IssuePatch
    ) -> dict:
        issue = data(request).issues.get((owner, repo, number))
        if issue is None:
            raise HTTPException(status_code=404, detail="issue does not exist")
        if patch.state is not None:
            if patch.state not in {"open", "closed"}:
                raise HTTPException(status_code=422, detail="state must be open or closed")
            issue["state"] = patch.state
        if patch.title is not None:
            issue["title"] = patch.title
        if patch.body is not None:
            issue["body"] = patch.body
        issue["updated_at"] = _stamp(1)
        return issue

    @app.post("/api/v1/repos/{owner}/{repo}/issues/{number}/comments", status_code=201)
    async def post_comment(
        request: Request, owner: str, repo: str, number: int, comment: CommentBody
    ) -> dict:
        store = data(request)
        if (owner, repo, number) not in store.issues:
            raise HTTPException(status_code=404, detail="issue does not exist")
        return store.add_comment(owner, repo, number, comment.body, store.user["login"])

    return app


def seed_gitea(state: State | None = None, *, owner: str = "uione") -> State:
    """A plausible working morning for the payments team.

    Written to be *usable* as a demo rather than exhaustive: one incident-shaped
    issue that a brief should lead with, one routine change, one thing already
    closed so the queue is not uniformly urgent, and one carrying a ticket key
    the work graph can join against mail.
    """
    store = state or State()
    repo = "payments-platform"

    store.add_issue(
        owner,
        repo,
        "Settlement batch PAY-1182 failing on retry",
        "Batch fails when the acquirer returns a soft decline. Ops escalated at 07:40.",
        labels=["incident", "payments"],
        assignee=owner,
        minutes=0,
    )
    store.add_issue(
        owner,
        repo,
        "Add idempotency key to refund endpoint",
        "Duplicate refunds observed on client retries. PAY-1190 tracks the rollout.",
        labels=["change"],
        assignee=owner,
        minutes=-90,
    )
    store.add_issue(
        owner,
        repo,
        "Quarterly reconciliation report is late",
        "Report generation exceeded its window three days running.",
        labels=["reporting"],
        minutes=-240,
    )
    store.add_issue(
        owner,
        repo,
        "Rotate acquirer sandbox credentials",
        "Completed during the maintenance window.",
        state="closed",
        assignee=owner,
        minutes=-1440,
    )
    store.add_comment(
        owner, repo, 1, "Acquirer confirmed a config change on their side at 06:10.", "ops-oncall"
    )
    return store
