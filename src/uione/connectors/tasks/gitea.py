"""Gitea and Forgejo issues as the task system.

First real task connector, and the choice is deliberate. Gitea is a single Go
binary an enterprise already runs behind its own firewall — the deployment model
this product targets — so it can be started, seeded and integrated against in
CI, forever, with no vendor account and no credential to rotate. Forgejo is a
fork with the same API surface, so this connector serves both.

The API is also close enough to GitHub's that the shape here is the one a GitHub
connector would take, which makes this the cheapest possible way to learn whether
the abstraction is right before writing five more.

**What is deliberately not done here:**

*No cross-repository fan-out beyond one call.* `/repos/issues/search` answers
"everything this token can see" in one request. Walking repositories and asking
each for its issues turns a morning brief into ninety HTTP calls against a server
that is also serving git.

*No issue bodies in the list.* A queue is a list of titles and states; pulling
every description to render a summary line wastes the server's time and fills the
model's context with text nobody asked for. Bodies arrive with `fetch`, per issue.

*Closing is not deleting.* The only write here moves state, and Gitea keeps the
issue, so the undo journal can genuinely put it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from uione.connectors.http import Auth, VendorClient, VendorConfig, VendorError
from uione.mcphub import InMemoryToolSource, RiskClass, ToolResult

log = structlog.get_logger(__name__)

#: Issues fetched for a queue view. More than this is not a queue, it is a
#: backlog, and nobody starts their day by reading one.
DEFAULT_LIMIT = 20


def gitea_config(
    base_url: str, token: str, *, verify_tls: bool = True, timeout_s: float = 20.0
) -> VendorConfig:
    """Gitea authenticates with ``Authorization: token <sha1>``, not Bearer."""
    return VendorConfig(
        name="gitea",
        base_url=base_url.rstrip("/") + "/api/v1",
        auth=Auth(scheme="token", secret=token),
        verify_tls=verify_tls,
        timeout_s=timeout_s,
    )


class GiteaTasks:
    """The calls this product needs, and no more."""

    def __init__(self, config: VendorConfig, **kwargs: Any) -> None:
        self._client = VendorClient(config, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def whoami(self) -> dict:
        return await self._client.get("/user")

    async def my_issues(self, *, limit: int = DEFAULT_LIMIT, state: str = "open") -> list[dict]:
        """Open issues assigned to, or created by, the authenticated user.

        Two calls rather than one because Gitea's search treats `assigned` and
        `created_by` as separate filters and combining them returns neither.
        Merged here by id: an issue you raised *and* own should appear once.
        """
        assigned = await self._client.get(
            "/repos/issues/search",
            params={"state": state, "assigned": True, "limit": limit},
        )
        created = await self._client.get(
            "/repos/issues/search",
            params={"state": state, "created_by": True, "limit": limit},
        )

        merged: dict[int, dict] = {}
        for issue in [*(assigned or []), *(created or [])]:
            merged[issue["id"]] = issue

        ordered = sorted(merged.values(), key=lambda i: i.get("updated_at", ""), reverse=True)
        return ordered[:limit]

    async def issue(self, owner: str, repo: str, number: int) -> dict:
        return await self._client.get(f"/repos/{owner}/{repo}/issues/{number}")

    async def comments(self, owner: str, repo: str, number: int, *, limit: int = 10) -> list[dict]:
        comments = await self._client.get(
            f"/repos/{owner}/{repo}/issues/{number}/comments", params={"limit": limit}
        )
        return list(comments or [])[-limit:]

    async def set_state(self, owner: str, repo: str, number: int, *, state: str) -> dict:
        """Open or close an issue.

        `PATCH`, not `DELETE`: the issue survives, which is what makes this
        reversible and therefore something the autonomy ladder can eventually
        let run unattended.
        """
        if state not in {"open", "closed"}:
            raise VendorError(f"state must be open or closed, not {state!r}")
        return await self._client.patch(
            f"/repos/{owner}/{repo}/issues/{number}", json_body={"state": state}
        )

    async def comment(self, owner: str, repo: str, number: int, *, body: str) -> dict:
        return await self._client.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments", json_body={"body": body}
        )


# -- rendering -------------------------------------------------------------


def issue_key(issue: dict) -> str:
    """A key a person would recognise and the work graph can match on.

    ``uione/payments-platform#3`` rather than the database id, because the id
    appears nowhere a human will ever see it — not in the UI, not in a commit
    message, not in the sentence someone types into chat.
    """
    repository = issue.get("repository") or {}
    full_name = repository.get("full_name") or repository.get("name") or "?"
    return f"{full_name}#{issue.get('number')}"


def render_issue(issue: dict) -> str:
    parts = [f"{issue_key(issue)} — {issue.get('title', '(no title)')} [{issue.get('state')}]"]
    if labels := [label["name"] for label in issue.get("labels") or []]:
        parts.append(f"  labels: {', '.join(labels)}")
    if assignees := [a["login"] for a in issue.get("assignees") or []]:
        parts.append(f"  assigned: {', '.join(assignees)}")
    if milestone := issue.get("milestone"):
        parts.append(f"  milestone: {milestone.get('title')}")
    if due := issue.get("due_date"):
        # Only ever from the field. Open-weight models invent due dates when
        # asked to summarise, which is the recurring finding in docs/EVALS.md;
        # the defence is that the number in the text came from the payload.
        parts.append(f"  due: {due}")
    return "\n".join(parts)


def parse_ref(reference: str) -> tuple[str, str, int]:
    """Split ``owner/repo#number``, the form the tools accept.

    Strict on purpose. A model that passes ``#3`` or ``PAY-1182`` gets a message
    naming the format rather than a silent guess at which repository was meant.
    """
    text = str(reference).strip()
    if "#" not in text or "/" not in text.split("#", 1)[0]:
        raise ValueError(f"expected owner/repo#number, got {reference!r}")
    path, _, number = text.partition("#")
    owner, _, repo = path.partition("/")
    if not (owner and repo and number.isdigit()):
        raise ValueError(f"expected owner/repo#number, got {reference!r}")
    return owner, repo, int(number)


# -- the governed tools ----------------------------------------------------


def build_gitea_source(
    tasks: GiteaTasks, *, name: str = "tasks", undo_note: str = ""
) -> InMemoryToolSource:
    """Expose Gitea through the gateway, with risks we assign.

    Every tool that returns issue text is marked as returning untrusted content.
    Issue bodies and comments are written by whoever can file an issue — which on
    an internal tracker includes contractors, and on a public one includes
    anybody at all.
    """
    source = InMemoryToolSource(name)

    async def my_open_issues(args: dict) -> ToolResult:
        try:
            limit = max(1, min(int(args.get("limit", DEFAULT_LIMIT)), 50))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        try:
            issues = await tasks.my_issues(limit=limit)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        if not issues:
            return ToolResult.success("No open issues assigned to you.", {"count": 0})

        return ToolResult.success(
            "\n".join(render_issue(i) for i in issues),
            {
                "count": len(issues),
                "keys": [issue_key(i) for i in issues],
                "fetched_at": datetime.now(UTC).isoformat(),
            },
        )

    async def get_issue(args: dict) -> ToolResult:
        try:
            owner, repo, number = parse_ref(args.get("issue", ""))
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        try:
            issue = await tasks.issue(owner, repo, number)
            comments = await tasks.comments(owner, repo, number)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        body = issue.get("body") or "(no description)"
        text = f"{render_issue(issue)}\n\n{body}"
        if comments:
            rendered = "\n".join(
                f"  {(c.get('user') or {}).get('login', '?')}: {(c.get('body') or '')[:400]}"
                for c in comments
            )
            text += f"\n\nLast {len(comments)} comment(s):\n{rendered}"

        return ToolResult.success(
            text,
            {"key": issue_key(issue), "state": issue.get("state"), "url": issue.get("html_url")},
        )

    async def update_issue(args: dict) -> ToolResult:
        try:
            owner, repo, number = parse_ref(args.get("issue", ""))
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        state = str(args.get("state", "")).strip().lower()
        if state not in {"open", "closed"}:
            return ToolResult.failure("state must be 'open' or 'closed'")

        try:
            issue = await tasks.set_state(owner, repo, number, state=state)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        return ToolResult.success(
            f"{issue_key(issue)} is now {issue.get('state')}."
            + (f" {undo_note}" if undo_note else ""),
            {"key": issue_key(issue), "state": issue.get("state")},
        )

    async def comment_on_issue(args: dict) -> ToolResult:
        try:
            owner, repo, number = parse_ref(args.get("issue", ""))
        except ValueError as exc:
            return ToolResult.failure(str(exc))

        body = str(args.get("body", "")).strip()
        if not body:
            return ToolResult.failure("body is required")

        try:
            comment = await tasks.comment(owner, repo, number, body=body)
        except VendorError as exc:
            return ToolResult.failure(str(exc))

        return ToolResult.success(
            f"Commented on {owner}/{repo}#{number}.", {"url": comment.get("html_url")}
        )

    issue_arg = {
        "type": "string",
        "description": "Issue reference as owner/repo#number, e.g. uione/payments-platform#3.",
    }

    source.register(
        "my_open_issues",
        my_open_issues,
        description="List open issues assigned to or raised by the user.",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "1-50, default 20."}},
        },
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "get_issue",
        get_issue,
        description="Read one issue in full, with its most recent comments.",
        parameters={"type": "object", "properties": {"issue": issue_arg}, "required": ["issue"]},
        risk=RiskClass.READ,
        returns_untrusted_content=True,
    )
    source.register(
        "update_issue",
        update_issue,
        description="Open or close an issue.",
        parameters={
            "type": "object",
            "properties": {
                "issue": issue_arg,
                "state": {"type": "string", "enum": ["open", "closed"]},
            },
            "required": ["issue", "state"],
        },
        # Reversible: the issue survives and the state can be put back, which is
        # what lets this tool eventually earn unattended execution.
        risk=RiskClass.REVERSIBLE_WRITE,
    )
    source.register(
        "comment_on_issue",
        comment_on_issue,
        description="Add a comment to an issue.",
        parameters={
            "type": "object",
            "properties": {"issue": issue_arg, "body": {"type": "string"}},
            "required": ["issue", "body"],
        },
        # A comment notifies watchers and cannot be unsent from their inbox, so
        # it is not treated as merely reversible even though it can be deleted.
        risk=RiskClass.IRREVERSIBLE,
    )
    return source
