# Which systems we can actually integrate against, and in what order

Every connector claim in this repo is verified against something real. That
discipline runs into a wall with enterprise software: most of it cannot be
obtained without a sales conversation. This document is the decision record for
how that wall is handled — which vendors are reachable, in what order they get
built, and what happens for the ones that are not reachable at all.

## The ranking, and why it is not the obvious one

For a SaaS product the obvious order is "biggest market share first". For an
**on-premise, air-gapped** product it is not, because the systems an air-gapped
enterprise runs are disproportionately the ones you can also download and run
yourself. So the order is:

| Tier | Access | Why it ranks here |
|---|---|---|
| **A. Self-hostable, open source** | `docker run`, no account, no expiry | Both free *and* the deployment model the product targets. A customer running us air-gapped is running these. Verifiable in CI, forever, with no credential to rotate. |
| **B. Perpetual free tier** | Account required, no expiry | Validates the API mapping against the real vendor cheaply. Cannot run in CI (needs a credential), so it is a manual verification step. |
| **C. Day-limited trial** | Account required, expires | Verifies once, then the evidence goes stale. Worth it only where A and B have no equivalent. |
| **D. No free access** | Sales conversation | Cannot be verified at all. Requires a mock, and requires saying so out loud. |

A tier-C verification has a shelf life, which is exactly why tier A is preferred
even when the tier-C product is the market leader: a test that can only be run
during a 14-day window is a test that stops running.

## Tier A — self-hostable, and therefore first

Run locally, seeded with real data, and integrated against over real HTTP.

| System | Domain | API | Status |
|---|---|---|---|
| **Gitea / Forgejo** | tasks, issues, code review | REST v1, `Authorization: token` | **built** — verified against a real instance |
| Redmine | tasks, projects, time | REST, `X-Redmine-API-Key` | planned |
| Zammad | helpdesk, tickets | REST, token | planned |
| Grafana | dashboards, alerting | REST, service-account token | planned — the natural home for "BI-triggered anomaly" |
| Metabase | BI, questions, dashboards | REST, session or API key | planned |
| Mattermost | messaging | REST v4, personal access token | planned |
| GLPI / iTop | ITSM, CMDB | REST | candidate |
| TheHive + Cortex | incident response | REST | candidate |
| Wazuh | security events | REST | candidate |

## Tier B — perpetual free tier, account required

**These require the operator to create an account.** That is not something this
repo does on anyone's behalf: signing up for a third-party service, accepting its
terms and holding its credentials is the user's decision, not the assistant's.
What the repo provides is the connector, the configuration, and a mock that
behaves like the vendor so the integration is finished and testable before any
account exists.

| Vendor | Free tier | API | Our path |
|---|---|---|---|
| **Jira Cloud** | 10 users, no expiry | REST v3, basic auth with an API token | connector + mock |
| Confluence Cloud | 10 users | REST v2 | after Jira; also the target for hierarchical ACLs |
| **ServiceNow** | Personal Developer Instance — free, hibernates when idle | Table API, basic or OAuth | connector + mock |
| **Slack** | free workspace | Web API, bot token | connector + mock |
| GitHub | free | REST + GraphQL, PAT | Gitea's API is close enough to share a connector shape |
| Linear | free, 250 issues | GraphQL | candidate |
| PagerDuty | 5 users after the 14-day trial | REST v2 | connector + mock |
| Sentry, Datadog, Notion, Trello, ClickUp, Asana, YouTrack, Freshservice | various free tiers | REST | candidates |
| Microsoft 365 Developer Program | 90-day renewable sandbox | Graph | the route to Teams and Planner |

Terms change. Every row here should be re-checked at the point someone signs up
rather than trusted from this table.

## Tier C — day-limited trials

Tableau (14d), Power BI Pro (60d), Snowflake (30d), Databricks (14d), SAP BTP
(90d), Splunk (60d self-hosted). Each has a tier-A equivalent for our purposes —
Grafana, Metabase, Superset — so none is on the critical path. Revisit only if a
specific customer's estate demands it.

## Tier D — no free access, so a mock is the only honest option

Insurance claim platforms (**Guidewire ClaimCenter**, Duck Creek), core banking
(Temenos, Murex), and the large ERP/CRM suites in their on-premise editions
(SAP S/4HANA, Oracle Siebel, Pega). "Claim management" is named in the product
brief and lives entirely in this tier.

For these the repo ships a **mock API**: a real HTTP server implementing the
vendor's documented request and response shapes, run as a subprocess in tests and
as part of the demo estate.

### What a mock is allowed to claim

A mock proves the *connector* is correct against a stated contract. It cannot
prove the contract matches the vendor, because nobody here has seen the vendor.
So:

* A mock is written from published API documentation, and the document it was
  written from is cited in the mock's own source.
* Anything the mock asserts about the vendor's behaviour that could not be
  confirmed is marked in the code as an assumption.
* A connector verified only against a mock is labelled as such in
  [CONNECTORS.md](CONNECTORS.md). It does not get to claim "verified against
  Guidewire".

That last rule is the point of this whole document. A mock makes the integration
buildable and testable; it does not make it verified, and conflating the two is
how a connector ships that has never spoken to the system it names.

## What "living" means here

The practical consequence: `make estate` brings up the tier-A systems in Docker
alongside the tier-D mocks, seeds them with a plausible working day, and points
the product at them. The assistant then reads real issues over real HTTP from a
real ticket system, and real claims from a mock one — and the difference is
recorded rather than blurred.
