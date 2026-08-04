# Design System — UiOne AI

> Read this before making any visual or UI decision in this repository.

## Product Context

- **What this is:** A fully on-premise enterprise assistant running on open-weight
  models inside the customer's own datacentre. It does not just answer — it
  **writes to real systems**: mail, chat, tasks, incidents, claims, calendar,
  dashboards, file shares. Every change is held for human approval, audited, and
  reversible.
- **Who it's for:** Knowledge workers at regulated enterprises — banks, insurers,
  public sector, telcos, defence-adjacent. Ops-heavy roles first: incident
  response, claims operations, service desks, finance ops. The buyer is a
  CIO/CISO; the user keeps this open eight hours a day.
- **Space:** Enterprise AI assistants, adjacent to ops instruments (Linear,
  Datadog, Sentry, Grafana) rather than to chat products.
- **Project type:** Internal tool. Dense, long-session, keyboard-driven.

### The one thing to remember

> **"It acts on my real systems."**

Not a chatbot. It writes to your tracker, sends your mail, books your meetings.
It should feel consequential and instrumented — closer to a trading terminal than
to a messenger. Every decision below serves that sentence. When a future choice is
ambiguous, pick whichever option makes the product feel more like an instrument
and less like a conversation.

## Aesthetic Direction

- **Direction:** Industrial / Utilitarian, executed at high finish. Function-first,
  data-dense, monospace-forward, muted. Not brutalist — brutalism excuses
  roughness, and this is a precision instrument that is beautiful *because* it is
  exact.
- **Decoration level:** Minimal, with exactly one exception. Typography does all
  the work. The single texture in the product is a 45° hatch, reserved for taint
  and degradation.
- **Mood:** A lit control room at 06:50. Matte graphite and warm paper, hairlines
  instead of shadows, everything at rest until something is actually true, and the
  only saturated pixels in the building reserved for things about to change a real
  system.
- **Reference points:** Linear (`#08090A`, identifiers as mono chips, properties
  column), Warp (mono as institutional voice), Railway (near-black surfaces),
  Grafana (instrument density). Sentry is the counter-example: weight 700 and
  normal tracking is why it reads dated.

## Typography

**System stacks only.** No webfonts, no CDN, no `.woff2` in the package. This is a
deliberate, reconsidered choice: an air-gapped deployment should have nothing here
for a security team to review beyond the file itself, and that is worth more than
the last few percent of typographic polish.

The usual objection — that a system stack reads as "I gave up on typography" — is
answered by the four rules below, not by ignoring it. Type here is *authored*
through weight discipline, numerics, tracking and measure. And the heavy lifting is
done by the **mono** stack, which is far more consistent across platforms than the
sans stack is.

```css
--ui:   ui-sans-serif, system-ui, -apple-system, "Segoe UI Variable Text",
        "Segoe UI", "Noto Sans", "DejaVu Sans", sans-serif;
--mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas,
        "Liberation Mono", "DejaVu Sans Mono", monospace;
```

### The four rules

1. **Two weights only: 400 and 600.** Never 500 — it silently resolves to 400 on
   several Windows and Linux installs, and the hierarchy dissolves on a third of an
   enterprise estate. No italics: system italics diverge violently across
   platforms. Emphasis is weight, colour, **or** mono — never all three at once.
2. **`font-variant-numeric: tabular-nums slashed-zero` globally**, not just in
   tables. Counts update in place (5 unread → 6 unread) and proportional figures
   make the whole rail twitch. Slashed zero because `INC-0010001` gets read aloud
   on a bridge call.
3. **Tracking is inverted from the default.** System faces are drawn tight for body
   copy, so: negative above 18px, zero at body, positive below 12px. Applied
   consistently, this is the single strongest signal of deliberate typesetting.
4. **Prose capped at 68ch.** The brief is the only real prose on screen. Give it a
   measure and it stops looking like a log.

### Scale

| Role | Face | Size / line-height | Weight | Tracking | Notes |
|---|---|---|---|---|---|
| The one thing on fire | mono | 20 / 26 | 600 | −0.01em | one line, top of centre |
| Panel title (h1) | ui | 22 / 28 | 600 | −0.02em | |
| Section label | mono | 12 / 16 | 600 | +0.08em | uppercase |
| Brief prose | ui | 14 / 21 | 400 | −0.006em | max 68ch |
| Dense row | ui | 14 / 20 | 400 | 0 | truncates |
| **Identifier** | **mono** | 12.5 / 16 | 400 | +0.01em | **never truncates** |
| Status / priority chip | mono | 11 / 11 | 600 | +0.06em | uppercase |
| Numeral display | mono | 20 / 20 | 600 | −0.01em | tabular |
| Meta (time, ACL, latency) | mono | 11 / 14 | 400 | +0.02em | muted |
| Verbatim payload | mono | 12.5 / 19 | 400 | 0 | preserves whitespace |

Wordmark: `UIONE` in mono, 12px, 600, +0.16em, with the health indicator as a
**6×6px square**. Circles read consumer; squares read instrument.

## Colour

**Approach: restrained to the point of severity.** Chrome is fully achromatic. If a
pixel is saturated, something either changed a real system, is about to, or is on
fire. Nothing decorative gets a hue — not the nav, not the brand, not gridlines.
That is what keeps amber legible as a siren after eight hours instead of becoming
wallpaper.

| Token | Light | Dark | Meaning |
|---|---|---|---|
| `--bg` | `#ECECE8` | `#101214` | app shell |
| `--surface` | `#F7F7F5` | `#171A1D` | panels, rows |
| `--surface-2` | `#E4E4DF` | `#1F2327` | hover, selected |
| `--verbatim` | `#FFFFFF` | `#08090A` | **the literal bytes only** |
| `--border` | `#D6D6D0` | `#262A2E` | hairlines |
| `--border-strong` | `#B9B9B1` | `#3A4147` | dividers, hatch |
| `--text` | `#16181A` | `#E6E9EC` | |
| `--text-muted` | `#5E6266` | `#9AA3AA` | |
| `--text-faint` | `#767C81` | `#79838A` | |
| `--hold` | `#A85F00` | `#E9A33A` | awaiting your signature |
| `--hold-tint` | `#FBEFD9` | `#2A2013` | |
| `--crit` | `#A81E17` | `#F2685C` | P1, breach, denied, blocked |
| `--crit-tint` | `#FAE3E1` | `#2B1513` | |
| `--ok` | `#1B6B45` | `#4FBF8B` | executed, reversible |
| `--ok-tint` | `#DDEEE4` | `#10241C` | |
| `--prov` | `#1D5DA6` | `#6FA8F0` | provenance, system identity |
| `--focus` | `#1D5DA6` | `#6FA8F0` | focus ring |

Three rules that matter more than the values:

- **The verbatim surface.** Pure white / near-black is reserved for exactly one
  thing: the literal payload that will be sent, and diffs. It appears nowhere else,
  so users learn "this rectangle is the actual bytes." This is the approvals
  promise — *exactly what would be sent, not a summary of it* — made visual.
- **Taint and degradation are textures, not hues.** A session that read untrusted
  content, and an unreachable connector, get a 45° 3px hatch plus a mono chip
  (`CONTAINED`, `UNREACHABLE`). A fourth and fifth alarm colour would devalue the
  two that matter, and hatching survives greyscale printing and colour-blindness —
  which the CISO's accessibility review will ask about.
- **Priority is one hue plus mass, not five hues.** P1 = filled 18×18 block with a
  white numeral. P2 = outlined block. P3–P5 = neutral mono text. Five priority
  colours across 200 rows is confetti; mass is legible peripherally.

**Amber is the signature, and it appears in exactly three places, ever:** the
approvals count, the left edge of a held action, and the composer's "this will
write" indicator. Nowhere else. Every future feature must resist it.

Light is warm paper — **never `#FFF` as canvas**, which halates over eight hours.
Dark is cool graphite, and dark is authored *first*, because incident bridges
happen at 03:00. Dark is not an inversion of light; both are authored against the
same semantic tokens. All text pairs clear 4.5:1; `--text` clears 14:1 in both.

## Spacing

- **Base unit:** 8px, with a 4px sub-step.
- **Density:** compact. Target ~38 rows of live information above the fold at
  1440×900. It was ~44 until 2026-08-04; six rows bought the row height that makes
  this read as an application rather than a terminal, and that trade is the point
  of the amendment.
- **Scale:** 4 · 8 · 12 · 16 · 24 · 32 · 48
- **Fixed heights:** header 44px · dense row **32px** · approval row 36px.
  The approval row stays at 36 deliberately: it is now only 4px taller than a
  dense row, and if that stops reading as a distinct class it should grow to 40
  rather than the dense row shrinking back.

## Layout

- **Approach:** grid-disciplined. Three fixed zones, no floating anything.
- **Left rail, 216px.** Text nav, not icons. Each item carries a right-aligned mono
  tabular count. **Zero counts render in `--text-faint`, never hidden** — a zero is
  information.
- **Centre column, fluid, 640–860px content.** The day.
- **Right rail, 360px, pinned, always present.** The consequence rail: approvals on
  top, undo journal below. Never a modal, never collapsed by default.
- **Border radius:** **3px on chips**, **6px on controls** (buttons, inputs, and
  floating surfaces), **0px on rows and tables**. A chip keeps the tighter radius
  because a chip is a *label*, and a label shaped like a button invites a click
  that does nothing.
- **One elevation, and only one.** `--shadow-float` exists for surfaces that
  genuinely float above the page: menus, popovers, the approval expand. Never on
  rows, cards, or the rail — those are zones, and a zone that appears to hover is
  lying about its position. Separation everywhere else is still 1px hairlines and
  one surface step.

  This replaces "zero box-shadows in the entire product", which held until
  2026-08-04. The rule it replaces was right about decoration and wrong about
  hierarchy: a menu that reads as part of the page is a menu whose state the user
  has to infer. **Nothing floats yet**, so this is a rule waiting for its first
  surface rather than a change you can currently see.

### Row grammar

Every fact on screen, from every system, uses the same skeleton:

```
[3px provenance spine][2-char system tag][identifier, mono][status chip][title, truncates][owner][age, right]
```

System tags: `MA` mail · `CH` chat · `TK` tasks · `IN` incidents · `CL` claims ·
`CA` calendar · `BI` dashboards · `KN` knowledge · `DO` documents.

**Identifiers and ages never compress.** Titles are what you throw away when the
window narrows.

### First thing the eye hits

A single 20px mono line at the top of the centre column, framed by the only large
area of empty space on the page — the one live P1, in the form an ops person would
say out loud:

```
INC-4471 · P1 · 7h 12m · payment gateway p99 · yours
```

If nothing is on fire it reads `NOTHING ON FIRE` in `--text-muted` and **holds its
space**. A stable landmark beats a collapsing one.

Degradation is chrome-level: when the API returns `complete: false`, a 24px
hatched notice sits directly under the header, rendered from the structured field
and never from the model's prose.

## Motion

- **Approach:** minimal-functional. Motion is evidence, never decoration.
- **Duration:** 120ms ease-out on state change, 160ms ceiling. **Nothing loops.**
- **The single continuous animation permitted in the product** is the tool tape
  while a tool is genuinely executing. When the model stalls, the tape stalls.
- `prefers-reduced-motion` drops all durations to 0. Every state must remain
  legible in colour and text alone.

### Presence

The assistant has **presence but no face**. A face is a claim of intent, and this
product's whole pitch is that it has no intent it has not shown you. Presence is a
state glyph plus the **tool tape** — a live mono line of the actual calls:

```
■ WORKING
  incidents.my_incidents → 3
  mail.list_unread → 5 untrusted
```

The tape carries strictly more information than an expression and cannot
misrepresent anything.

## Deliberate departures

Recorded so a future session does not "fix" them:

1. **Mono is a display voice, not a code detail.** The screen is ~70% identifiers;
   mono is the honest face for it, and it reads as infrastructure rather than as
   chat.
2. **The transcript is demoted.** Assistant replies use the *same row grammar* as
   facts from ServiceNow and IMAP, distinguished by one glyph in the provenance
   spine. An eight-hour session is searchable by `PAY-1182`, not by scrollback.
   Conversation is a poor index into work; identifiers are the real one.
3. **Consequence is permanent furniture.** No approval modals, ever — modals train
   reflexive clicking, and reflexive clicking is exactly the failure the threat
   model depends on not happening. Held actions live in a pinned rail, expand in
   place onto the verbatim surface, and after execution stay in position and change
   state (`HELD → SENT · undo`) rather than vanishing behind a toast. Positional
   persistence is what trust is made of.

## Anti-patterns — never introduce

- Purple or violet gradients. Gradient CTAs. Glassmorphism. Decorative blobs.
- Three-column icon grids. Centred-everything.
- Uniform bubbly border-radius.
- Box-shadows for elevation.
- A chat-bubble transcript.
- An anthropomorphic face.
- Colour used for anything except consequence and severity.
- Amber anywhere outside its three permitted places.

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-28 | Initial design system created | `/design-consultation`, grounded in live research of Linear, Grafana, Railway, Warp, Sentry plus an independent blind design pass |
| 2026-07-28 | Memorable thing = "it acts on my real systems" | User chose it over "it already did the work"; it is the harder claim and the real differentiator — everyone ships a morning digest, almost nobody writes back into governed systems |
| 2026-07-28 | System font stacks, no shipped webfont | User kept the strict reading of the air-gap constraint. Mono carries the identity instead, and it is the more platform-consistent stack anyway |
| 2026-07-28 | Presence keeps a state glyph, loses the face | An independent pass argued a face is a claim of intent this product deliberately lacks; the tool tape carries more information and cannot lie |
| 2026-08-04 | Density relaxed: dense row 28→32px, dense-row body 13/18→14/20 | The industrial thesis was sound; five numbers made it read as a terminal rather than as the instrument DESIGN.md describes. Row height is the strongest of them. Costs ~6 rows above the fold |
| 2026-08-04 | Section labels 11/14 +0.10em → 12/16 +0.08em | Wide-tracked micro-caps are the most terminal thing on the page and the least load-bearing — no rule depends on the tracking |
| 2026-08-04 | Control radius 6px, chips stay 3px, rows stay 0px | Buttons read sharp next to the softer surfaces they sit on. Rows keep 0px because a row is not a card, which is the row grammar's whole argument |
| 2026-08-04 | One elevation token for floating surfaces; "zero box-shadows" retired | The old rule was right about decoration and wrong about hierarchy. Scoped to things that genuinely float, so it cannot become ambient depth |
| 2026-08-04 | Accent colour REJECTED | Proposed a low-saturation accent for focus and selection and rejected it. Saturation means consequence is what keeps amber legible as a siren; spending that rule to look contemporary is the worst trade on offer |
| 2026-07-28 | Identifier chip is the atom of the system | Category designs the chat bubble because chat products render prose; this product emits typed records, so the identifier gets the typographic treatment |
