# The demo estate

    make estate     # real Gitea and Grafana in Docker, provisioned and seeded
    make mocks      # the vendors nobody can reach without a contract
    set -a; . ./.env.estate; set +a
    make run        # http://127.0.0.1:8000/

Three commands and the product is reading a real ticket system, a real alerting
system, and mocked ITSM and claims platforms — over real HTTP, through the same
code path a production deployment takes, with only the base URLs different.

## Why it is half real

The split follows [VENDOR_ACCESS.md](VENDOR_ACCESS.md). Gitea and Grafana are
tier A: open source, self-hostable, no account, no expiry. They are what an
air-gapped customer actually runs, so they are what this project verifies
against. ServiceNow and the claims platform are tier B and D — an account or a
contract stands between us and them — so they are mocked, and the estate makes
the difference visible rather than blurring it.

| Service | What it is | Port |
|---|---|---|
| Gitea | **real**, in Docker | 3300 |
| Grafana | **real**, in Docker, with an alert rule that fires | 3400 |
| Mattermost | **real**, in Docker, with two users and a real mention | 8065 |
| Gitea mock | for tests without Docker | 9101 |
| ServiceNow mock | no PDI account here | 9102 |
| Claims mock | no free access exists at all | 9103 |

## What `make estate` actually does

Everything in `scripts/estate.py` was first done by hand against these systems.
That is the only reason it can be trusted — each call is one that was watched to
succeed, not one that looked right.

It creates a Gitea admin user and an API token, a repository and three issues; a
Grafana Viewer service account and token, a test datasource, a folder, and an
alert rule that starts firing within about a minute; and a Mattermost admin, a
team, a channel, **a second person**, and three messages from them — one of which
mentions you. The second person is not decoration: your own posts are read by
definition, so a single-user estate has nothing unread to demonstrate. Then it
writes `.env.estate` and stops.

Mattermost's image for this tag is amd64-only, so on Apple silicon it boots
emulated and takes a minute or two. The script waits for readiness rather than
assuming a fixed startup time.

**It is idempotent.** Running `up` twice does not duplicate a repository, an
issue or an alert rule — because the second run is usually somebody debugging
the first. Tokens are the exception: they cannot be read back after creation, so
each run revokes the old one and issues a new one.

**Everything binds to 127.0.0.1.** The mocks are unauthenticated by design, so
that starting the estate needs no secrets, and `python -m uione.vendormocks`
refuses to bind anything but the loopback interface. Grafana holds a well-known
password. On one machine that is fine; on a network it is an open door with
plausible-looking corporate data behind it.

**Credentials go to `.env.estate`, mode 600, gitignored**, and are printed only
as prefixes. A demo that leaves a token in the repository teaches the wrong habit
at precisely the moment somebody is deciding whether to trust this thing with
their mailbox.

## Verified

From a cold start — containers removed, volumes deleted:

```
$ make estate
Gitea    ready · user created · token 1077565e… · repository created · issues: 3
Grafana  ready · service account 2 (Viewer) · token glsa_HCmyNd7… · alert rule created

$ set -a; . ./.env.estate; set +a
servers: bi, calendar, claims, incidents, knowledge, mail, tasks
  bi.firing_alerts        allowed   54ms   [critical] Settlement failure rate above threshold
  tasks.my_open_issues    allowed   35ms   uione/payments-platform#3 — Quarterly reconciliation…
  incidents.my_incidents  allowed   17ms   INC0010002 [On Hold] Refund API returning 500…
  claims.my_claims        allowed   16ms   CLM-004401 [open] Northgate Logistics BV — collision
```

## Teardown

    make estate-down      # stop, keep the data volumes
    make estate-destroy   # stop, delete volumes and .env.estate
