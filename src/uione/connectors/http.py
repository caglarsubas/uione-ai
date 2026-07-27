"""The shared spine under every REST connector.

Six vendors, six auth schemes, six pagination styles, and one set of mistakes
that would otherwise be made six times. This is that set, made once:

**Credentials never reach a log.** Not in a URL, not in an exception, not in a
retry message. A connector that logs its own `Authorization` header at DEBUG is
a credential in a log aggregator that a hundred people can read.

**Every request has a deadline, and a bounded number of attempts.** A ticket
system having a slow morning must not hold up everyone's brief. Retries are for
`429` and `5xx` only — retrying a `400` just sends the same bad request again,
and retrying a `403` looks like a brute-force attempt to whoever is watching.

**`Retry-After` is honoured.** Vendors publish it precisely so clients back off
correctly, and a client that ignores it gets rate-limited harder. Capped, because
a header saying "come back in six hours" must not become a six-hour hang.

**Pagination is bounded.** A "fetch all issues" that follows pages until the
vendor runs out will one day meet a project with 80,000 of them and exhaust
memory on a machine that also has to answer chat.

**HTTP failure is a `ToolResult`, not an exception.** The agent loop shows the
model why a call failed so it can choose differently; a traceback cannot be
reasoned about, and one dead vendor is not an outage of the assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

#: How long to wait for a vendor, total. Generous for a slow enterprise system,
#: short enough that a brief does not stall behind it.
DEFAULT_TIMEOUT_S = 20.0

#: Attempts, not retries: 3 means the original plus two more.
DEFAULT_ATTEMPTS = 3

#: A vendor asking for a longer wait than this is telling us to give up for now.
MAX_RETRY_AFTER_S = 30.0

#: Pages followed before we stop and say so. Ten pages of a hundred items is
#: already more than any brief should carry.
MAX_PAGES = 10

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class VendorError(RuntimeError):
    """A call to a vendor system failed in a way the caller should surface."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS if self.status else True


@dataclass
class Auth:
    """How to authenticate to one vendor.

    Kept as data rather than a callable so a connector's configuration can be
    logged — everything here except the secret itself is safe to print, and
    `__repr__` is overridden so the secret cannot leak by accident.
    """

    #: "bearer" (Slack, Grafana), "token" (Gitea's `Authorization: token X`),
    #: "basic" (Jira, ServiceNow), or "header" (Redmine's `X-Redmine-API-Key`).
    scheme: str = "bearer"
    secret: str = ""
    username: str = ""
    header: str = "Authorization"

    def headers(self) -> dict[str, str]:
        match self.scheme:
            case "bearer":
                return {"Authorization": f"Bearer {self.secret}"}
            case "token":
                return {"Authorization": f"token {self.secret}"}
            case "basic":
                import base64

                raw = f"{self.username}:{self.secret}".encode()
                return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}
            case "header":
                return {self.header: self.secret}
            case "none":
                return {}
        raise ValueError(f"unknown auth scheme {self.scheme!r}")

    def __repr__(self) -> str:  # pragma: no cover — defensive, but the point
        return f"Auth(scheme={self.scheme!r}, username={self.username!r}, secret=<redacted>)"


@dataclass
class VendorConfig:
    name: str
    base_url: str
    auth: Auth = field(default_factory=Auth)
    timeout_s: float = DEFAULT_TIMEOUT_S
    attempts: int = DEFAULT_ATTEMPTS
    verify_tls: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def __repr__(self) -> str:
        return (
            f"VendorConfig(name={self.name!r}, base_url={self.base_url!r}, "
            f"auth={self.auth!r}, verify_tls={self.verify_tls})"
        )


class VendorClient:
    """One vendor's HTTP API, with the mistakes above already made."""

    def __init__(self, config: VendorConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owned = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=httpx.Timeout(self.config.timeout_s, connect=5.0),
                verify=self.config.verify_tls,
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            await self._client.aclose()
            self._client = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.config.extra_headers}
        headers.update(self.config.auth.headers())
        headers.update(extra or {})
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """One call, with retries. Returns parsed JSON, or raises `VendorError`."""
        client = await self._http()
        url = path if path.startswith("http") else "/" + path.lstrip("/")
        last: VendorError | None = None

        for attempt in range(1, self.config.attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(headers),
                )
            except httpx.HTTPError as exc:
                # Deliberately `type(exc).__name__` rather than `str(exc)`: httpx
                # puts the full URL in some messages, and a URL can carry a token
                # for vendors that still accept one in a query string.
                last = VendorError(f"{self.config.name} unreachable ({type(exc).__name__})")
                log.warning(
                    "vendor.transport_error",
                    vendor=self.config.name,
                    method=method,
                    path=url,
                    error=type(exc).__name__,
                    attempt=attempt,
                )
            else:
                if response.status_code < 400:
                    return _parse(response)

                last = VendorError(
                    _describe(self.config.name, response),
                    status=response.status_code,
                )
                log.warning(
                    "vendor.http_error",
                    vendor=self.config.name,
                    method=method,
                    path=url,
                    status=response.status_code,
                    attempt=attempt,
                )
                if response.status_code not in RETRYABLE_STATUS:
                    # A 400 retried is the same bad request; a 403 retried looks
                    # like brute force to whoever is watching the vendor's logs.
                    raise last
                await self._wait(response, attempt)
                continue

            if attempt < self.config.attempts:
                await asyncio.sleep(min(2.0**attempt * 0.2, 4.0))

        raise last or VendorError(f"{self.config.name} failed")

    async def _wait(self, response: httpx.Response, attempt: int) -> None:
        """Back off, preferring the vendor's own instruction."""
        after = response.headers.get("Retry-After")
        if after:
            try:
                # Capped: a header saying "six hours" must not become a six-hour
                # hang inside somebody's morning brief.
                await asyncio.sleep(min(float(after), MAX_RETRY_AFTER_S))
                return
            except ValueError:
                # The date form of Retry-After. Not worth parsing; back off
                # normally rather than guessing at a clock difference.
                pass
        await asyncio.sleep(min(2.0**attempt * 0.2, 4.0))

    async def get(self, path: str, **kwargs) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> Any:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs) -> Any:
        return await self.request("PATCH", path, **kwargs)

    async def put(self, path: str, **kwargs) -> Any:
        return await self.request("PUT", path, **kwargs)

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_param: str = "page",
        limit_param: str = "limit",
        page_size: int = 50,
        start_page: int = 1,
        max_pages: int = MAX_PAGES,
        extract=lambda payload: payload,
    ) -> list[Any]:
        """Follow numbered pages, bounded.

        The bound is not politeness. A "fetch all issues" that follows pages
        until the vendor runs out will one day meet a project with 80,000 of
        them, on a machine that also has to answer chat.
        """
        items: list[Any] = []
        for page in range(start_page, start_page + max_pages):
            payload = await self.get(
                path, params={**(params or {}), page_param: page, limit_param: page_size}
            )
            batch = list(extract(payload) or [])
            items.extend(batch)
            if len(batch) < page_size:
                return items

        log.info(
            "vendor.pagination_capped",
            vendor=self.config.name,
            path=path,
            pages=max_pages,
            collected=len(items),
        )
        return items


def _parse(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        # A vendor answering 200 with HTML is almost always a login page — the
        # session expired, and reporting "invalid JSON" would send whoever reads
        # it looking in entirely the wrong place.
        text = response.text.strip()
        if text[:1] == "<":
            raise VendorError(
                "expected JSON and received HTML — the credential is probably rejected or expired",
                status=response.status_code,
            ) from None
        raise VendorError(f"could not parse response: {text[:120]}") from None


def _describe(vendor: str, response: httpx.Response) -> str:
    """A message that says what to do about it.

    Status codes alone send people to a search engine. These map to the thing
    that is actually wrong, in the words the person fixing it would use.
    """
    hint = {
        400: "the request was rejected as malformed",
        401: "the credential was not accepted",
        403: "the credential is valid but lacks permission for this",
        404: "no such object, or the credential cannot see it",
        409: "conflicts with the current state",
        422: "the values were rejected as invalid",
        429: "rate limited",
    }.get(response.status_code, f"HTTP {response.status_code}")

    detail = ""
    try:
        payload = response.json()
        for key in ("message", "error", "error_description", "errorMessages", "detail"):
            if value := payload.get(key) if isinstance(payload, dict) else None:
                detail = f": {value if isinstance(value, str) else str(value)[:120]}"
                break
    except ValueError:
        pass

    return f"{vendor} {hint}{detail}"


def chunked(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
