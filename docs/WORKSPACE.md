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
