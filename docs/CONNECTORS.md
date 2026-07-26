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

## Still fixtures

`calendar`, `tasks`, `incidents` remain fixtures (`connectors/demo.py`). They
follow the same declaration discipline, so replacing them with real backends is a
backend swap rather than a rewrite.
