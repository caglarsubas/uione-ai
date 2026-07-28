# Running the whole product in containers

```bash
make up          # everything, UI on http://127.0.0.1:8000/
```

That is enough to use the assistant. Incidents and claims come from the mocked
vendors, mail and calendar from fixtures, and the UI, streaming, approvals and
retrieval all work.

To connect the *real* Gitea, Grafana and Mattermost as well:

```bash
make provision                 # creates accounts and tokens
docker compose restart app     # the app picks them up
```

Port 8000 is the most contested port on any developer's machine, so it is
overridable: `UIONE_HTTP_PORT=8081 make up`.

## What is in the image, and what is not

**Two stages.** The build stage carries a compiler and a package index, and
neither belongs on a machine in a datacentre with no internet. What ships is the
virtualenv and the source.

**The dependency list is compiled from `pyproject.toml`**, not repeated in the
Dockerfile. The first draft did repeat it and omitted `sse-starlette` and
`jsonschema` — an image that builds and then fails to import.

**Not root.** A container writing to a mounted file share as root writes
root-owned files onto somebody's NAS, and the permission model this product is
built around then describes files nobody can fix.

**No inference engine.** It runs on the host and the app reaches it through
`host.docker.internal`. Shipping one would mean several gigabytes of weights to
run a demo, and an air-gapped customer already has an engine — the premise is
that we talk to theirs. Point `UIONE_MODEL_PLANE_URL` wherever yours lives.

## Why provisioning is a separate command

Creating Gitea's first admin needs `gitea admin user create` **inside** that
container. The alternative is handing a provisioning container the host's Docker
socket, which is control of the whole daemon — a far worse trade than one extra
command in a product whose entire argument is about least privilege.

Grafana and Mattermost are pure API and could have been provisioned in-network;
splitting them from Gitea would have meant two mechanisms for one job.

## Three things this exercise found

**The project could not be built as a wheel.** `pyproject.toml` had a
`force-include` for `src/uione/web/static` on top of `packages = ["src/uione"]`,
which added every static file twice and failed the build outright. Invisible for
as long as every install was editable, and the first thing to happen when the
project was finally packaged.

**The provisioner reported "user already exists" when the container was
missing.** It ran `docker exec uione-gitea`; compose names the container
`uione-gitea-1`. Any non-zero exit was read as "already exists", so an
infrastructure failure arrived as a reassuring message and a confusing 401 three
lines later. It now distinguishes the two and looks up the container by name.

**Grafana refused the admin password and the code raised `KeyError: 'id'`.**
Assuming success and indexing the response meant the actual message — *invalid
username or password* — never reached anyone. The mismatch was a typo in this
compose file; the bug was that it took ten minutes to see.

## Ports

| | |
|---|---|
| UiOne | http://127.0.0.1:8000/ |
| Gitea | http://127.0.0.1:3300/ |
| Grafana | http://127.0.0.1:3400/ (admin / uione-dev-pw) |
| Mattermost | http://127.0.0.1:8065/ |
| Mocked vendors | 9101 Gitea-shaped, 9102 ServiceNow, 9103 claims |

Everything binds to 127.0.0.1. None of it should be reachable from the network
while it holds demo credentials.
