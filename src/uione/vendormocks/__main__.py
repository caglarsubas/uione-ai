"""Run the mock vendors as real servers.

    python -m uione.vendormocks

Starts each mock on its own port, seeded with a plausible working morning, so
the product can be pointed at them and *run* rather than described. The
connectors then speak real HTTP over real sockets — the same code path a
production deployment takes, with only the base URL different.

Bound to 127.0.0.1 and nothing else. These serve data to anyone who asks, by
design, so that starting the estate needs no secrets; that is only acceptable
because they are not reachable from off the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

import structlog
import uvicorn

from uione.vendormocks.claims import build_claims_mock, seed_claims
from uione.vendormocks.gitea import build_gitea_mock, seed_gitea
from uione.vendormocks.servicenow import build_servicenow_mock, seed_servicenow

log = structlog.get_logger(__name__)

#: Chosen well away from anything a developer machine usually runs, and
#: contiguous so the whole estate is one range in a firewall rule.
PORTS = {"gitea": 9101, "servicenow": 9102, "claims": 9103}


def build_all(user: str = "uione") -> dict:
    return {
        "gitea": build_gitea_mock(seed_gitea(owner=user)),
        "servicenow": build_servicenow_mock(seed_servicenow(user=user)),
        "claims": build_claims_mock(seed_claims(user=user)),
    }


async def serve(host: str, user: str, ports: dict[str, int]) -> None:
    apps = build_all(user)
    servers = [
        uvicorn.Server(
            uvicorn.Config(app, host=host, port=ports[name], log_level="warning", access_log=False)
        )
        for name, app in apps.items()
    ]

    for name, port in ports.items():
        print(f"  {name:12} http://{host}:{port}")
    print("\nPoint the product at them:\n")
    print(f"  UIONE_GITEA_URL=http://{host}:{ports['gitea']} UIONE_GITEA_TOKEN=mock \\")
    print(f"  UIONE_SERVICENOW_URL=http://{host}:{ports['servicenow']} \\")
    print("  UIONE_SERVICENOW_USERNAME=uione UIONE_SERVICENOW_PASSWORD=mock \\")
    print(f"  UIONE_CLAIMS_URL=http://{host}:{ports['claims']} \\")
    print("  .venv/bin/python -m uione\n")

    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mock vendor estate.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--user", default="uione", help="Whose queue the data belongs to.")
    parser.add_argument(
        "--i-understand-this-is-open",
        action="store_true",
        help="Allow binding a non-loopback address. For containers only.",
    )
    for name, port in PORTS.items():
        parser.add_argument(f"--{name}-port", type=int, default=port)
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.i_understand_this_is_open:
        # Refused rather than warned about. These endpoints are unauthenticated
        # on purpose, and one on a shared network is an open door with plausible
        # -looking corporate data behind it.
        #
        # The escape hatch exists for containers, where binding 0.0.0.0 means
        # "reachable inside this compose network" rather than "reachable from
        # the building", and the port publishing is what actually decides
        # exposure. It is deliberately verbose to type: nobody reaches for it
        # by accident, and it appears in `ps` output where a reviewer sees it.
        raise SystemExit(
            f"refusing to bind {args.host}: the mock estate is unauthenticated "
            "and must stay on the loopback interface. Inside a container, pass "
            "--i-understand-this-is-open and publish the ports to 127.0.0.1 only."
        )

    ports = {name: getattr(args, f"{name}_port") for name in PORTS}
    print("\nMock vendor estate — unauthenticated, loopback only, for evaluation.\n")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.host, args.user, ports))


if __name__ == "__main__":
    main()
