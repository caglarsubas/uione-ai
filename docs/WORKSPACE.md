# The web workspace

Everything the platform does was previously invisible: the API was complete and
no human could use it. This is the surface.

## Zero build step, zero dependencies

Three files — `index.html`, `app.js`, `styles.css` — served straight by FastAPI.
No npm, no bundler, no CDN, no web fonts.

That is a security decision before it is an engineering one. For an air-gapped
product a `node_modules` tree is a supply chain the customer's security team must
review and we must patch forever. Here the entire client is three files they can
read in an afternoon. A test asserts no asset references an external origin.

## The screens, and what each is for

**Today** — the morning brief, served from the pre-generated copy in
milliseconds. Below it, a row of chips: which tool produced each section, which
identifiers the work graph linked across systems, whether externally-authored
content is present, and which model wrote it. Provenance is not an admin detail;
it is what makes a claim checkable in one glance.

**Assistant** — chat, with the tool trace shown inline rather than hidden. When
the reliability layer repairs an argument, that is displayed too. An assistant
that acts invisibly is one nobody audits.

**Approvals** — the screen the governance story lives or dies on.

**What it may do** — the transparency surface (gap G15). The autonomy ladder per
tool with progress toward unattended execution, and every action taken on the
user's behalf with whether it can still be undone.

**Systems** — connector reachability and the brief schedule.

## Two deliberate UI decisions

**Degradation is shown above the content, never below.** A user who reads "no
incidents" and relaxes has been misled by omission. The banner names which
systems could not be checked before they read a word of the brief.

**The approval card shows the payload verbatim**, in monospace, exactly as it
will be sent — not a friendly summary. Approving a summary that differs from the
payload is the precise failure this screen exists to prevent, and a changed
character has to be visible. Risk class is colour-coded: reversible writes amber,
external-facing and irreversible red.

Colour is otherwise reserved. This is a tool people look at for eight hours; the
only things that should draw the eye are state that needs attention.

## Verified in a browser

Loaded against a live server with `ministral-3:8b`:

- brief rendered from the pre-generated copy, with all four provenance chips,
  both work-graph links (`INC-4471`, `INV-88213`), and the external-content flag
- two pending approvals shown with distinct risk badges and full payloads
- **Approve and run** executed the action, advanced the autonomy record to 1/5,
  wrote a journal entry, removed the card, and decremented the sidebar badge

## Note on the identity headers

`app.js` sends `X-User-Id` / `X-User-Roles` headers, matching the placeholder
auth on the server (F5.1 replaces both with OIDC). It is written plainly at the
top of the file rather than dressed up as a session, because a placeholder that
looks like real auth is how a placeholder reaches production.

## Not yet

Streaming responses, mobile layout beyond a single breakpoint, keyboard
navigation past tab order, and localisation — the copy is English-only while
`UIONE` targets EN+TR at MVP (gap G18).


## Streaming

The reply used to arrive all at once, after the whole agent loop finished — four
to nine seconds of spinner for a question that touched two systems.

**Progress is the point, not tokens.** For an agent the tool calls take most of
the wall clock: a mailbox round trip dwarfs the time to write a sentence about
it. A user watching *"reading your mail…"* learns more than one watching the
first sentence appear eight seconds later. Tokens stream too, but they are the
smaller half of the improvement.

```
[ 0.00s] step 1
[ 2.17s] calling incidents.my_incidents …   ✓
[ 2.18s] calling mail.list_unread …         ✓
[ 2.18s] step 2
        You have **3 open incidents**: …
[ 4.31s] done: completed after 2 step(s)
```

### Server-sent events, not a WebSocket

Everything flows one way, so the second direction would be unused protocol. SSE
is plain HTTP, which matters for a product deployed behind somebody else's
reverse proxy: no upgrade handshake to be refused, no separate timeout to tune,
and it survives the corporate middleboxes that quietly drop WebSocket upgrades.

`X-Accel-Buffering: no` is not decoration. Without it nginx buffers the whole
response and delivers it at once, turning a stream back into the wait it
replaced — and it is invisible in development, because nobody runs nginx there.

### `done` is the completion signal

A client that never receives one knows its answer is truncated. Without that
distinction a dropped connection produces a half-answer that *looks* finished,
and the reader has no way to tell — worse than an error. A stream that dies
leaves whatever arrived in place and says it stopped early, rather than
discarding it.

### A held action is not a failure

`tool_result` carries `held` separately from `ok`. An action waiting for approval
shown as an error teaches people to distrust the approval queue.

### Markdown, escaped first

The answer is rendered with the smallest markdown that makes it readable — bold,
inline code, bullets — and the escaping happens first and unconditionally. An
answer can contain text the model read out of an email, and an email is written
by anyone, so treating any of it as markup is how a stranger puts HTML in this
page.

**No links and no images**, for a second reason beyond safety: a rendered link or
image in an on-premise product is an outbound request to whatever host the text
names, which is exactly the phone-home an air-gapped deployment exists to avoid.

### Two bugs the browser caught that tests did not

The stream hardcoded `Content-Type` instead of calling the page's `headers()`
helper, so in dev auth mode it 401'd while every other call worked. And the token
accumulator was named `raw`, colliding with the SSE frame's own `raw` — a
`TypeError: Assignment to constant variable` that only appears when a token
actually arrives.
