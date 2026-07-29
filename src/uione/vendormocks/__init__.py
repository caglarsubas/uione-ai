"""Vendor APIs, faked faithfully enough to build against.

Most enterprise software cannot be obtained without a sales conversation. That
is a problem for a project whose rule is that every connector is verified
against something real: for a Guidewire or an SAP there is no "something real"
to be had. These mocks are the answer, and their limits are stated rather than
glossed over — see [docs/VENDOR_ACCESS.md](../../../docs/VENDOR_ACCESS.md).

**What a mock is for.** Two things. It lets a connector be written and tested
before anyone has an account, and it lets the whole product be *run* — the demo
estate — so an evaluator sees a working assistant instead of a fixture.

**What a mock proves.** That the connector is correct against a stated contract.
Not that the contract matches the vendor, because nobody here has seen the
vendor. Every mock cites the documentation it was written from, and marks any
behaviour that could not be confirmed as an assumption. A connector verified only
against a mock says so in `docs/CONNECTORS.md`.

**What a mock is not.** Production software. These serve unauthenticated data to
anyone who asks, on purpose, so that starting the estate needs no secrets. They
are never registered in the app's own routing and never listen outside localhost
unless someone goes out of their way.

Each is a FastAPI app, which means it can be driven two ways: in-process over
ASGI for tests, where it is fast and needs no port, and as a real server over a
real socket for the estate, where the connector's own HTTP handling is exercised
end to end.
"""

from uione.vendormocks.claims import build_claims_mock, seed_claims
from uione.vendormocks.gitea import build_gitea_mock, seed_gitea
from uione.vendormocks.grafana import build_grafana_mock, seed_grafana
from uione.vendormocks.mattermost import build_mattermost_mock, seed_mattermost
from uione.vendormocks.servicenow import build_servicenow_mock, seed_servicenow
from uione.vendormocks.whatsapp import build_whatsapp_mock, inbound_payload, sign

__all__ = [
    "build_claims_mock",
    "build_gitea_mock",
    "build_grafana_mock",
    "build_mattermost_mock",
    "build_whatsapp_mock",
    "inbound_payload",
    "sign",
    "build_servicenow_mock",
    "seed_claims",
    "seed_gitea",
    "seed_grafana",
    "seed_mattermost",
    "seed_servicenow",
]
