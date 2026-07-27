# Speaking MCP

The product's premise is that enterprise platforms already ship MCP servers and
UiOne connects to them. Until this point that was an assumption: the hub spoke to
in-process Python objects and to a duck-typed "session" a caller supplied. Real
governance, no real protocol.

It now speaks JSON-RPC 2.0 over a subprocess's stdin and stdout — the `initialize`
handshake, `tools/list`, `tools/call` — to servers it did not write.

## The client is ours

Written rather than taken from the official SDK, for two reasons. An on-premise
product ships what it depends on, and an air-gapped install should not carry a
dependency it never calls. More importantly, the transport to an **untrusted
third-party process** is the boundary most worth owning.

The official SDK is a *test* dependency instead, where it plays the server. Our
client is validated against an implementation we did not write — a hand-written
server on both ends would agree with itself about a misreading of the spec.

```
handshake: 2025-06-18 {'name': 'tickets', 'version': '1.28.1'}
  tool search_issues: 'Search the ticket system.' schema=['limit', 'query']
  tool close_issue:   'Close an issue. Changes state.' schema=['key']
call ok: 2 issues matching 'payments' (limit 2): PAY-1182, PAY-1190
error call: isError=True 'Error executing tool explode: kaboom'
```

## A server may raise its own risk. It may never lower it.

This is the rule the whole integration turns on, and the previous version of
`classify_risk` got it wrong. It honoured `readOnlyHint` and returned `READ` —
the one risk class exempt from the approval ladder. A compromised MCP server
could therefore declare `deleteEverything` read-only and have it run unattended.

The MCP specification is explicit that annotations are hints and must not be
relied upon for security decisions. "May this run without a human?" is a security
decision.

The asymmetry is safe in both directions because of who benefits:

| Hint | Effect | Why |
|---|---|---|
| `destructiveHint: true` | Honoured → `IRREVERSIBLE` | A server claiming to be *more* dangerous costs its own users a prompt. There is no attack in it. |
| `openWorldHint: true` | Honoured → `EXTERNAL_FACING` | Same. |
| `readOnlyHint: true` | **Ignored**, logged | Claiming to be *less* dangerous buys unattended execution. That is the entire prize. |
| `idempotentHint: true` | **Ignored** | Same. |

The default for an unclassified tool is `IRREVERSIBLE`. Getting `READ` requires a
human to have written it down, per tool, in the deployment's configuration —
which is precisely the moment someone looks at each tool and decides.

```
liar.delete_everything    irreversible    ← declared "readOnlyHint": true
```

## Tool descriptions are an injection vector

A tool description is attacker-controllable text that reaches the context window
**with nobody invoking anything**. It is there because the server is registered,
and it is present in every single request. That makes it a better vector than any
tool result.

Descriptions are scanned before the model sees them, and a tool whose description
matches a known injection pattern is **withheld from the catalog entirely**:

```
poisoned.search           irreversible    ← the clean tool beside it
                                             (`helper` was withheld)
```

Withholding is the opposite of the choice made for tool *results*, deliberately.
A result arrives mid-task and dropping it silently breaks the work, so results
are quarantined and shown with a warning. A description is read once, at
registration, and a server whose catalog says "ignore previous instructions" is
one an operator needs to know about *before* it is used.

## What a server does not get

An MCP server is a subprocess, and a subprocess inheriting our environment
inherits the database URL, the OIDC client secret and the mail password. It gets
`PATH`, `HOME`, the locale, and whatever the operator listed explicitly — nothing
else. `inherit_env` exists for the rare case that needs it, and saying so out
loud is the point.

## Configuration

```json
UIONE_MCP_SERVERS='[
  {"name": "tickets",
   "command": "/usr/bin/python3",
   "args": ["-m", "corp_tickets_mcp"],
   "env": {"TICKETS_URL": "https://tickets.corp.example"},
   "timeout_s": 30,
   "risk": {"search_issues": "read", "close_issue": "reversible_write"}}
]'
```

Two failure policies, pointing in opposite directions on purpose:

**A broken server is not an outage.** If the ticket server will not start, mail
and calendar still work and the assistant says which system is unavailable.
Refusing to boot over one connector makes every connector a single point of
failure. The reason is reported at `/system/health`, so "why can't it see my
tickets?" is answered from the API rather than from someone's memory of the boot
logs.

**A broken *configuration* is a startup failure.** A malformed server list is an
operator error made seconds ago, at a keyboard, with the logs in front of them.
Booting with silently zero connectors — an assistant that can do nothing, for no
stated reason — is far worse than exiting with the parse error.

## Robustness, as requirements rather than error handling

Each of these is a test against a server that misbehaves on purpose:

| Behaviour | Why it is not merely defensive |
|---|---|
| stderr is drained continuously | MCP servers log there. A full pipe buffer blocks the server's next write, so a chatty server stops answering after a few calls — a failure that looks exactly like a hang and is not one. |
| Every request has a deadline | Without one, a silent server leaves the morning brief waiting on a pipe nobody will write to. |
| A dead process fails its pending calls at once | Not on the 30-second timeout. The process is gone; waiting helps nobody. |
| One unparseable line is skipped | Servers print stray output. A reader that dies on it takes every pending call with it. |
| An unknown protocol version is refused | The spec has the client disconnect rather than guess at a dialect. |
| A failed handshake leaves nothing running | A half-initialised server would sit in the catalog answering nothing. |

## End to end, in the running product

Three servers configured — one real, two hostile — booted by the actual
application:

```
MCP servers:
  tickets    connected=True proto=2025-06-18 impl=tickets          tools=3
  poisoned   connected=True proto=2025-06-18 impl=hostile-poisoned tools=1
  liar       connected=True proto=2025-06-18 impl=hostile-liar     tools=1

Catalog, with the risk the hub assigned (not the risk each server claimed):
  tickets.search_issues     read              untrusted=True
  tickets.close_issue       reversible_write  untrusted=True
  tickets.explode           irreversible      untrusted=True
  poisoned.search           irreversible      untrusted=True   ← `helper` withheld
  liar.delete_everything    irreversible      untrusted=True   ← claimed read-only

call tickets.search_issues (granted) -> 2 issues matching 'payments': PAY-1182, PAY-1190
call tickets.search_issues (ungranted) -> denied: principal lacks a grant covering read on tickets
call liar.delete_everything -> denied
```

Every remote tool is marked `returns_untrusted_content`. Whatever a third-party
server hands back could have been written by anyone, so reading from one taints
the session exactly as inbound mail does — and a tainted session cannot reach an
egress channel without a human. See [SECURITY_MODEL.md](SECURITY_MODEL.md).

## Not yet

Streamable HTTP transport (stdio only today), OAuth for remote servers, MCP
*resources* and *prompts* (only tools are consumed), and pinning a server's
declared tool set so a later change — the "rug pull", where a server ships benign
tools and mutates them after approval — requires re-approval rather than being
picked up silently at the next restart.
