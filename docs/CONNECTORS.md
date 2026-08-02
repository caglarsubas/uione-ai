# Connectors

A connector turns an enterprise system into governed tools. Every one must
declare, explicitly and in code, three things the gateway relies on:

| Declaration | Why it cannot be inferred |
|---|---|
| **Risk class** per tool | Only the connector author knows whether an update is reversible in *that* system. Unclassified tools default to `IRREVERSIBLE` (F3.8). |
| **`returns_untrusted_content`** | Determines whether reading taints the session. Get this wrong and injection containment never engages for that connector. |
| **Undo builder**, or its absence | A tool with no registered undo is treated as irreversible, which is the safe direction. |

## Mail (IMAP/SMTP) — Wave 1

The critical-path connector. The strategy research found on-prem mail has the
*weakest* MCP ecosystem support of anything in the estate — no server at all for
Zimbra, only a thin community EWS bridge for on-prem Exchange — while the morning
ritual depends on mail entirely. So it is a first-party build.

Speaks plain IMAP and SMTP, which reaches Zimbra, Dovecot, Cyrus, and Exchange
with IMAP enabled — the on-prem estates that have no other option.

### Tools

| Tool | Risk | Untrusted | Notes |
|---|---|---|---|
| `mail.list_unread` | `READ` | yes | Newest first, limit clamped to 50 |
| `mail.search` | `READ` | yes | IMAP `TEXT` search, query quoted |
| `mail.get_message` | `READ` | yes | Fuller body (4000 chars) than listings |
| `mail.mark_read` | `REVERSIBLE_WRITE` | — | Undo registered |
| `mail.send_reply` | `EXTERNAL_FACING` | — | Egress-checked, never auto-runs while tainted |

### Design decisions

**Standard library, not an async IMAP package.** This ships into air-gapped
estates where every dependency is something the customer's security team reviews
and we patch. `imaplib`/`smtplib` are synchronous, so calls run in a worker
thread.

**UIDs, never sequence numbers.** Sequence numbers are renumbered on expunge, so
a brief that reads message 4 and later marks message 4 as read can mark a
*different* message. Every command uses the UID variants, and a test asserts it.

**A connection per operation.** IMAP connections are stateful and not
thread-safe; a pooled connection that silently drops is a bug class that appears
only under load. Login costs a few hundred milliseconds and the brief makes one
or two calls. Pooling is a later optimisation with a benchmark behind it.

**Attachment contents are never read.** Attachments are untrusted bytes of
unknown size — a token disaster and an injection vector. Only metadata is
surfaced, and the rendering says "not read" so the model does not assume it has
seen them. Document understanding is a separate, explicit pipeline (F1.7).

**Parsing is total.** Real mailboxes contain RFC 2047 headers in half a dozen
charsets, declared encodings that lie, HTML-only messages, and the occasional
message that is simply broken. Every branch produces a message; a parser that
raises on one malformed item takes the whole morning brief down with it.

### IMAP command injection

The search term originates from a model, which means it is attacker-influenced
whenever untrusted content is in context — which, for a mail connector, is
routinely. An unescaped `"` would close the quoted string and let the remainder
be read as IMAP command tokens.

`quote_imap()` escapes backslash and quote, strips CR/LF (which would terminate
the command line entirely), and bounds length. UIDs are separately validated as
numeric before any `FETCH` or `STORE`, so a value like `1:100` or `1 OR 1` is
rejected before it reaches the server.

### Verified against a real server

Beyond unit tests, the connector was exercised over a real socket against an
IMAP4rev1 server:

```
--- list_unread ---
  uid=102 ext=True  subj='Invoice INV-88213'      from=supplier@outside.example
  uid=101 ext=False subj='Q3 bütçe toplantısı'    from=cfo@corp.example
--- get_message 101 ---
  subject='Q3 bütçe toplantısı'   body='Perşembe 14:00.'
--- mark_read 101 ---
  remaining unread: ['102']
--- injection attempt: search 'x" (DELETED) "' ---
  server healthy, 0 hits;  post-injection unread: ['102']   # nothing deleted
```

Turkish subjects and bodies round-trip correctly (gap **G18** — localisation is
verified, not assumed), external senders are classified from configuration, and
the injection attempt changed nothing.

### Configuration

```bash
UIONE_MAIL_IMAP_HOST=mail.corp.example
UIONE_MAIL_IMAP_PORT=993
UIONE_MAIL_USERNAME=alice@corp.example
UIONE_MAIL_PASSWORD=...            # from the secrets manager, never a prompt
UIONE_MAIL_SMTP_HOST=smtp.corp.example
UIONE_INTERNAL_DOMAINS=corp.example,corp.local
```

With no `UIONE_MAIL_IMAP_HOST`, the fixture connector is used, so a fresh
checkout runs with no infrastructure.

`UIONE_INTERNAL_DOMAINS` drives both external-sender detection and the egress
allowlist. Left empty, **every** address is external — outbound mail is refused
rather than quietly allowed anywhere, which is the safe direction to be wrong in.

## Calendar (CalDAV) — Wave 1

Speaks CalDAV (RFC 4791) and iCalendar (RFC 5545), reaching Nextcloud, Radicale,
Baikal, SOGo, Zimbra and anything else standards-compliant — most of the on-prem
calendar estate that has no MCP server and no prospect of one.

### Tools

| Tool | Risk | Untrusted | Notes |
|---|---|---|---|
| `calendar.today` | `READ` | yes | Today's entries |
| `calendar.upcoming` | `READ` | yes | Next N days, grouped, clamped to 14 |
| `calendar.availability` | `READ` | **no** | Free working-hour slots — times only |

Titles are marked untrusted because meeting subjects are written by other people,
including external senders whose invitations land in the calendar. `availability`
is *not*, and that distinction is load-bearing: it returns only slot times, no
text anyone authored, so an A2A availability answer can use it without tainting
the session.

### Parsing

iCalendar is parsed with the `icalendar` library, not by hand. The format looks
simple and is not — folded lines, escaped separators, per-property timezones,
all-day events expressed as dates rather than datetimes. Hand-rolling produces
something that works on the developer's calendar and fails on everyone else's.

**All-day events are the trap.** They arrive as a date; treating one as midnight
UTC shifts it into the wrong day for anyone outside UTC, which in a morning brief
means the wrong day entirely.

### Recurrence, and what it refuses to guess

DAILY, WEEKLY (with `BYDAY`) and MONTHLY-by-date are expanded, honouring `COUNT`
and `UNTIL`. `BYSETPOS`, `BYMONTHDAY` lists and "third Thursday" rules are **not**
— those are where a naive expander starts inventing meetings, and an invented
meeting in a brief is worse than a missing one.

Unsupported rules keep their original instance and carry a visible note:
`[repeats yearly; occurrences not expanded]`. Expansion is bounded, so a
malformed rule cannot spin.

### Verified against a real server

Over a socket against a live CalDAV server, not a mock:

```
--- 5 instances over 3 days ---
  Mon 27 09:30  Incident review — INC-4471
  Mon 27 14:00  Daily standup
  Mon 27 16:00  Board meeting   [repeats yearly; occurrences not expanded]
  Tue 28 14:00  Daily standup   [recurring instance]
  Wed 29 14:00  Daily standup   [recurring instance]

--- free slots on the 27th (times only) ---
  ['10:00', '11:00', '12:00', '13:00', '15:00', '17:00']

server log: REPORT from alice; time-range present: True
```

The 09:30, 14:00 and 16:00 entries correctly remove those hours. Time-range
filtering happens server-side — the difference between fetching one day and
fetching someone's entire calendar history.

### What this fixed in A2A

`GatewayAnswerer._free_slots` previously regexed times out of the *rendered* day.
A meeting whose title contained "14:00" would have marked the wrong hour busy —
and a title is exactly what a disclosure contract may forbid us from seeing. It
now calls `calendar.availability`, which computes from event times and returns
only times.

### Configuration

```bash
UIONE_CALENDAR_URL=https://nextcloud.corp.example/remote.php/dav/calendars/alice/personal/
UIONE_CALENDAR_USERNAME=alice        # falls back to the mail credentials
UIONE_CALENDAR_PASSWORD=...
UIONE_BRIEF_TIMEZONE=Europe/Istanbul # also the calendar's display timezone
```

## Fixtures, and when they are used

Every connector here has a real backend. Each also has a fixture in
`connectors/demo.py`, and the fixture is what runs when that system is not
configured — `UIONE_GITEA_URL` unset means the `tasks` server is the fixture one.
The real source *replaces* the fixture rather than joining it, because two
servers named `tasks` would shadow each other in the catalog and which one
answered would depend on registration order.

So "fixture" is a statement about one deployment's configuration, not about the
connector. The question worth asking is the next section's: what has each been
verified against.

## What each connector has actually been verified against

The distinction this table draws is the one that matters: a connector tested
only against a fixture someone wrote is a connector that agrees with its author.

| Connector | Verified against | Runs in CI |
|---|---|---|
| Mail (IMAP/SMTP) | Two throwaway IMAP servers and a real SMTP exchange | yes |
| Calendar (CalDAV) | A real CalDAV server, **including writing meetings to Radicale in CI** | yes |
| File share | Real files, real `chmod`, real symlinks | yes |
| Identity (OIDC) | A throwaway IdP with real JWKS, authorize and token endpoints | yes |
| MCP (stdio) | A server built with the official SDK, plus a hostile one | yes |
| **Tasks (Gitea/Forgejo)** | **A real Gitea 1.24 instance in Docker**, and a mock in CI | yes (mock), opt-in (real) |
| Incidents (ServiceNow) | A mock only — a PDI is free but needs an account | yes (mock) |
| Claims (Guidewire-shaped) | A mock only — **no free access exists in this category** | yes (mock) |
| **BI (Grafana)** | **A real Grafana 11.6 in Docker** with a rule firing, and a mock in CI | yes (mock), opt-in (real) |
| **Chat (Mattermost)** | **A real Mattermost 10.5 in Docker** with two users and real unread state, and a mock in CI | yes (mock), opt-in (real) |

Since 2026-08-02 each of these also has a **golden task** in the `connectors`
eval suite, running the agent against the vendor mock — so the invariant each
connector's module documents (ServiceNow's three-shaped `state`, Guidewire's
money-as-string, Gitea's key form) is checked end to end rather than only in
unit tests. WhatsApp is the exception and `docs/EVALS.md` says why: it pushes
rather than polls, so a golden task would be testing the webhook.

The last two rows are the honest version of "we support ServiceNow and
Guidewire". What the tests prove is that the connector handles the *shapes*
correctly, and the shapes are the part that transfers between a mock and the
real thing: ServiceNow's three-way-polymorphic fields, Guidewire's
optimistic-locking checksum, and money that must never become a float. Field
names may well need adjusting on first contact with a real instance. Those
behaviours will not.

The Gitea row is the pattern for every vendor connector from here. The mock runs
everywhere; the real instance runs when `UIONE_TEST_GITEA_URL` and
`UIONE_TEST_GITEA_TOKEN` are set, and it is what keeps the mock honest. The first
draft of that mock returned `repo` on each issue because that is the obvious
name. Gitea returns `repository`. Every mock test passed.

See [VENDOR_ACCESS.md](VENDOR_ACCESS.md) for which systems can be reached at all,
and what a mock is and is not allowed to claim.
