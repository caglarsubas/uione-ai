/* UiOne workspace.
 *
 * Vanilla, no build step, no dependencies. For an air-gapped product a
 * node_modules tree is a supply chain the customer's security team has to
 * review and we have to patch; this file is the whole client.
 *
 * Identity comes from an httpOnly session cookie the browser sends on its own,
 * so no token is ever readable from here. The X-User-* headers below are used
 * only when the server is in dev auth mode, which it refuses to be outside a
 * development environment.
 */

const DEV_USER = { id: "alice", roles: "analyst", name: "Alice" };

let AUTH = { mode: "unknown", login_url: null };
let ME = null;

function headers() {
  const base = { "Content-Type": "application/json" };
  if (AUTH.mode === "dev") {
    base["X-User-Id"] = DEV_USER.id;
    base["X-User-Roles"] = DEV_USER.roles;
    base["X-User-Name"] = DEV_USER.name;
  }
  return base;
}

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

class NotAuthenticated extends Error {}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: headers(),
    // Send the session cookie. Same-origin only: this client never talks to
    // anything but its own server.
    credentials: "same-origin",
    ...options,
  });
  if (res.status === 401) {
    // The identity headers below only work when the server runs in dev auth
    // mode. Against an OIDC deployment this page needs a bearer token, which
    // the login flow (not yet built) supplies.
    throw new NotAuthenticated("this deployment requires a signed-in session");
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

function authNotice() {
  const node = notice("warn", "Not signed in.", "");
  if (AUTH.login_url) {
    const button = el("button", "primary", "Sign in");
    button.style.marginLeft = "10px";
    button.addEventListener("click", () => {
      // Come back to whatever the user was looking at.
      const here = window.location.pathname + window.location.search;
      window.location.href = `${AUTH.login_url}?return_to=${encodeURIComponent(here)}`;
    });
    node.append(button);
  } else {
    node.append(
      el("span", null, " This deployment authenticates elsewhere; no login is available here."),
    );
  }
  return node;
}

/* ---- navigation ---- */

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) =>
      b.setAttribute("aria-selected", String(b === btn)),
    );
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    $(`#panel-${btn.dataset.panel}`).classList.add("active");
    REFRESH[btn.dataset.panel]?.();
  });
});

/* ---- brief ---- */

/* Minimal markdown. Deliberately not a full parser: the brief is short, the
 * model emits a narrow subset, and pulling in a markdown library would defeat
 * the point of shipping no dependencies. Everything is escaped first, so model
 * output cannot inject markup — it is untrusted text like any other. */
/* ---- presence ----------------------------------------------------------
 *
 * The avatar and the scope tiles are readouts of the event stream. Nothing here
 * starts an animation on a guess: `working` is entered because a `tool` event
 * arrived and left because its `tool_result` did, and the mouth moves only
 * while tokens are actually landing. A model that stalls mid-sentence looks
 * stalled, which is the honest thing for it to look like.
 */

/* One glyph per system, drawn inline. No icon font and no sprite sheet: an
   air-gapped deployment should have nothing here to fetch, and a missing icon
   font degrades to empty boxes, which looks broken rather than plain. */
const SCOPE_ICONS = {
  mail: '<path d="M2 4h12v8H2z"/><path d="M2 5l6 4 6-4"/>',
  calendar: '<rect x="2" y="3" width="12" height="11" rx="1"/><path d="M2 7h12M6 2v3M10 2v3"/>',
  tasks: '<path d="M3 8l2.5 2.5L13 4"/><path d="M3 13h10"/>',
  incidents: '<path d="M8 2l6 11H2z"/><path d="M8 7v3M8 11.5v.5"/>',
  claims: '<path d="M4 2h6l3 3v9H4z"/><path d="M9 2v4h4"/>',
  chat: '<path d="M2 3h12v8H6l-4 3z"/>',
  bi: '<path d="M3 13V7M8 13V3M13 13v-4"/>',
  knowledge: '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/>',
  documents: '<path d="M4 2h5l3 3v9H4z"/><path d="M6 8h4M6 11h4"/>',
};

const SCOPE_LABELS = {
  mail: "Mail",
  calendar: "Calendar",
  tasks: "Tasks",
  incidents: "Incidents",
  claims: "Claims",
  chat: "Chat",
  bi: "Dashboards",
  knowledge: "Documents",
  documents: "Writing",
};

const presence = {
  set(state, text) {
    const node = $("#presence");
    if (!node) return;
    node.dataset.state = state;
    if (text !== undefined) $("#presence-state").textContent = text;
  },

  /* The tape is the presence. Each line is an actual call, written when it
     starts and completed when its result arrives — so it cannot claim work that
     did not happen, and when the model stalls the tape stalls with it. */
  start(name) {
    const tape = $("#tape");
    if (!tape) return;
    const line = el("div", "running");
    line.dataset.call = name;
    line.textContent = name;
    tape.append(line);
    tape.scrollTop = tape.scrollHeight;
  },

  finish(name, suffix) {
    const line = [...($("#tape")?.children ?? [])]
      .reverse()
      .find((n) => n.dataset.call === name && n.classList.contains("running"));
    if (!line) return;
    line.classList.remove("running");
    line.textContent = `${name} → ${suffix}`;
  },

  clear() {
    const tape = $("#tape");
    if (tape) tape.innerHTML = "";
  },
};

/** Build a tile for each system this deployment actually has. */
async function loadScopes() {
  const strip = $("#scopes");
  if (!strip) return;
  try {
    const health = await api("/system/health");
    // `connectors` is an object of name → status, not a list. Spreading it as
    // an array yields nothing and the strip renders empty, which looks like
    // "this deployment has no systems" rather than like a bug.
    const servers = Object.keys(health.connectors || {}).sort();
    strip.innerHTML = "";
    for (const server of servers) {
      const tile = el("span", "scope");
      tile.dataset.server = server;
      tile.dataset.state = "idle";
      tile.innerHTML =
        `<svg viewBox="0 0 16 16">${SCOPE_ICONS[server] || SCOPE_ICONS.knowledge}</svg>` +
        `<span>${SCOPE_LABELS[server] || server}</span><span class="scope-count"></span>`;
      strip.append(tile);
    }
  } catch {
    // A workspace without the strip is still a working workspace.
  }
}

function scopeTile(server) {
  return server ? $(`.scope[data-server="${CSS.escape(server)}"]`) : null;
}

function resetScopes() {
  for (const tile of document.querySelectorAll(".scope")) {
    tile.dataset.state = "idle";
    tile.querySelector(".scope-count").textContent = "";
  }
}

/**
 * The smallest markdown that makes model output readable.
 *
 * One function for the brief and the chat answer both. There used to be two,
 * differing in which rules they applied — the brief rendered headings, the chat
 * rendered nothing italic — so the same sentence looked different depending on
 * which tab you read it in.
 *
 * Escaping happens first and unconditionally. This text can contain whatever
 * the model read out of an email, and an email is written by anyone; treating
 * any of it as markup is how a stranger gets HTML into this page.
 *
 * Deliberately no links and no images. Beyond the safety argument, a rendered
 * link or image in an on-premise product is an outbound request to whatever
 * host the text names — exactly the phone-home an air-gapped deployment exists
 * to avoid.
 */
function renderMarkdown(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  return (
    escaped
      .replace(/^#{1,6}\s*(.+)$/gm, "<h3>$1</h3>")
      // Bold before italic: **x** would otherwise be read as an empty italic
      // wrapped around another, and the asterisks would survive on screen.
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      // The content may not begin or end with a space, so "2 * 3 * 4" stays
      // arithmetic instead of becoming italics around a 3.
      .replace(/(^|[\s(])\*(\S|\S[^*\n]*\S)\*/g, "$1<em>$2</em>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      // Identifiers are typed objects, not emphasised words. This is the atom
      // of the design system (see DESIGN.md): the screen is roughly 70%
      // identifiers, and rendering INC0010002 as bold prose makes it
      // typographically identical to an emphasised English word.
      //
      // Runs after the bold pass, and the lookbehind deliberately permits `>`
      // so `**INC0010002**` — already `<strong>INC0010002</strong>` by now —
      // still gets chipped. That is the common case, because models bold
      // identifiers, and whether one is chipped must not depend on the model's
      // formatting. Safe because escaping ran first, so the only angle brackets
      // present are ones this function inserted.
      //
      // Scoped to shapes that are unambiguously keys: INC-4471, INC0010001,
      // PAY-1182, CLM-004401, owner/repo#12. Bare numbers (4471, 2300), times
      // (14:00) and dates (2026-07-28) are left alone.
      .replace(
        /(?<![\w-])((?:[A-Z]{2,6}-?\d{3,10})|(?:[a-z0-9][\w.-]*\/[\w.-]+#\d+))(?![\w-])/g,
        '<span class="id">$1</span>',
      )
      .replace(/^\s*[-*]\s+/gm, "• ")
      .replace(/^---+$/gm, "")
  );
}

async function loadBrief(refresh = false) {
  const body = $("#brief-body");
  body.innerHTML = '<span class="spinner"></span>';
  $("#brief-notices").innerHTML = "";
  $("#brief-meta").innerHTML = "";

  let brief;
  try {
    brief = await api(`/brief${refresh ? "?refresh=true" : ""}`);
  } catch (err) {
    body.innerHTML = "";
    body.append(
      err instanceof NotAuthenticated
        ? authNotice()
        : notice("danger", "Could not load your brief.", String(err)),
    );
    return;
  }

  $("#brief-subtitle").textContent = brief.pregenerated
    ? `Prepared for you ${humanAge(brief.age_seconds)} ago.`
    : "Just generated.";

  // Degradation is shown before the content, never after: a user who reads
  // "no incidents" and relaxes has been misled by omission.
  if (!brief.complete) {
    $("#brief-notices").append(
      notice(
        "warn",
        "Some systems could not be checked.",
        `${brief.unavailable.join(", ")} — anything from them is missing from this brief.`,
      ),
    );
  }
  if (brief.notice) {
    $("#brief-notices").append(notice("info", brief.notice));
  }

  body.innerHTML = renderMarkdown(brief.body || "(no brief)");

  const meta = $("#brief-meta");
  for (const section of brief.sections) {
    meta.append(
      chip(
        section.available ? "ok" : "bad",
        `${section.section}: ${section.available ? section.source : "unavailable"}`,
      ),
    );
  }
  for (const link of brief.connections || []) {
    meta.append(chip("link", `linked: ${link}`));
  }
  if (brief.untrusted_content_seen) {
    meta.append(chip("", "contains external content"));
  }
  if (brief.model) meta.append(chip("", brief.model));
}

function humanAge(seconds) {
  if (seconds == null) return "moments";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

function notice(kind, title, detail) {
  const node = el("div", `notice ${kind}`);
  node.append(el("strong", null, title));
  if (detail) node.append(el("span", null, ` ${detail}`));
  return node;
}

function chip(kind, text) {
  return el("span", `chip ${kind}`.trim(), text);
}

$("#refresh-brief").addEventListener("click", () => loadBrief(true));

/* ---- chat ---- */

function addMessage(role, text) {
  const msg = el("div", `msg ${role}`);
  msg.append(el("div", "avatar", role === "user" ? "You" : "Ui"));
  const body = el("div", "msg-body", text);
  msg.append(body);
  $("#thread").append(msg);
  msg.scrollIntoView({ behavior: "smooth", block: "end" });
  return body;
}

async function send() {
  const input = $("#composer");
  const message = input.value.trim();
  if (!message) return;

  input.value = "";
  $("#send").disabled = true;
  addMessage("user", message);
  const pending = addMessage("assistant", "");
  pending.innerHTML = '<span class="spinner"></span>';

  // Progress lives above the answer, so the answer stays readable as it grows.
  const progress = el("div", "tool-trace");
  const answer = el("div", "answer");
  pending.innerHTML = "";
  pending.append(progress, answer);
  progress.append(el("div", "step", "thinking…"));
  resetScopes();
  presence.clear();
  presence.set("thinking", "Thinking");

  try {
    await streamChat(message, { progress, answer });
    await loadApprovals();
  } catch (err) {
    // A stream that dies leaves whatever arrived in place and says so, rather
    // than replacing it: a partial answer the reader knows is partial is more
    // use than an error that discards it.
    answer.append(notice("danger", "The reply stopped early.", String(err)));
    presence.set("error", "Stopped");
  } finally {
    $("#send").disabled = false;
    input.focus();
  }
}

function renderAnswer(node, text) {
  node.innerHTML = renderMarkdown(text);
}

/** What this turn touched, for the line under the avatar once it is done. */
function touchedSummary() {
  const used = [...document.querySelectorAll('.scope[data-state="done"]')].map(
    (t) => t.querySelector("span:not(.scope-count)").textContent,
  );
  if (!used.length) return "Ask about your work.";
  return `Read ${used.join(", ").toLowerCase()}.`;
}

/** Consume the SSE stream, rendering progress and tokens as they arrive. */
async function streamChat(message, { progress, answer }) {
  // headers(), not a hand-written Content-Type: in dev auth mode this carries
  // the identity headers, and hardcoding the content type meant the stream
  // 401'd while every other call in the page worked.
  const res = await fetch("/chat/stream", {
    method: "POST",
    headers: headers(),
    credentials: "same-origin",
    body: JSON.stringify({ message }),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answerText = "";
  let finished = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line. Anything after the last one is
    // an incomplete frame and stays in the buffer — a chunk boundary can land
    // mid-frame, and parsing half a frame drops a token silently.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const kind = frame.match(/^event: (.+)$/m)?.[1];
      const raw = frame.match(/^data: (.+)$/m)?.[1];
      if (!kind || !raw) continue;
      const payload = JSON.parse(raw);

      if (kind === "step") {
        progress.firstChild.textContent = payload.step === 1 ? "thinking…" : `step ${payload.step}`;
        if (!answerText) presence.set("thinking", "Thinking");
      } else if (kind === "tool") {
        const line = el("div", "step");
        line.append(el("span", "tool-name", payload.name), el("span", null, " …"));
        line.dataset.tool = payload.name;
        progress.append(line);

        const tile = scopeTile(payload.server);
        if (tile) tile.dataset.state = "active";
        presence.set("working", "Working");
        presence.start(payload.name);
      } else if (kind === "tool_result") {
        const line = [...progress.children].reverse().find((n) => n.dataset.tool === payload.name);
        const status = payload.held
          ? " — held for your approval"
          : payload.ok
            ? " ✓"
            : ` — ${payload.error || "failed"}`;
        if (line) line.lastChild.textContent = status;

        const tile = scopeTile(payload.server);
        if (tile) {
          tile.dataset.state = payload.held ? "held" : payload.ok ? "done" : "failed";
          // The count comes from the tool's structured result, so the tile
          // says "Mail 5" only when mail actually reported five things.
          if (typeof payload.count === "number") {
            tile.querySelector(".scope-count").textContent = String(payload.count);
          }
        }
        // The tape records what the call actually returned: a count when the
        // tool reported one, HELD when it needs a signature, the failure
        // otherwise. Never a summary of intent.
        presence.finish(
          payload.name,
          payload.held
            ? "HELD"
            : !payload.ok
              ? "FAILED"
              : typeof payload.count === "number"
                ? String(payload.count)
                : "ok",
        );
        if (payload.held) presence.set("held", "Waiting on you");
      } else if (kind === "token") {
        // The raw text is kept and re-rendered, because markdown cannot be
        // formatted one fragment at a time — `**bo` is not bold yet.
        answerText += payload.text;
        renderAnswer(answer, answerText);
        if ($("#presence").dataset.state !== "speaking") {
          presence.set("speaking", "Answering");
        }
      } else if (kind === "done") {
        finished = true;
        progress.firstChild.textContent = payload.reason === "completed" ? "" : payload.reason;
        if (!answerText) renderAnswer(answer, payload.final || "(no reply)");
        const held = document.querySelectorAll('.scope[data-state="held"]').length;
        presence.set(held ? "held" : "idle", held ? "Waiting on you" : "Ready");
      } else if (kind === "error") {
        // `busy` is not a failure: the engine is working and fully committed,
        // and "try again" is different advice from "something went wrong".
        const busy = payload.reason === "busy";
        presence.set(busy ? "busy" : "error", busy ? "Busy" : "Stopped");
        throw new Error(payload.message);
      }
    }
  }

  // `done` is the completion signal. Without this check a dropped connection
  // produces a half-answer that looks finished, and the reader has no way to
  // tell — which is worse than an error.
  if (!finished) throw new Error("the connection closed before the reply finished");
}

$("#send").addEventListener("click", send);

/* Starting again is a real action, not a screen wipe: the server forgets, so the
   next turn genuinely has no history — including any taint the old conversation
   was carrying. The audit log keeps everything that was said and done. */
$("#new-conversation")?.addEventListener("click", async () => {
  await api("/chat/new", { method: "POST" }).catch(() => {});
  $("#thread").innerHTML = "";
  resetScopes();
  presence.clear();
  presence.set("idle", "Ready");
});
$("#composer").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
});

/* ---- approvals ---- */

async function loadApprovals() {
  const container = $("#approvals");
  const pending = await api("/approvals");

  const badge = $("#approval-count");
  badge.textContent = pending.length || "";
  badge.dataset.count = String(pending.length);

  container.innerHTML = "";
  if (!pending.length) {
    container.append(el("div", "empty", "Nothing is waiting on you."));
    return;
  }

  for (const action of pending) {
    const card = el("div", "card approval");

    const head = el("div", "card-head");
    head.append(el("div", "card-title", action.tool));
    head.append(el("span", `risk ${action.risk}`, action.risk.replace(/_/g, " ")));
    card.append(head);

    card.append(el("div", "reason", action.reason));
    card.append(el("div", "payload", action.preview));

    const actions = el("div", "actions");
    const approve = el("button", "approve", "Approve and run");
    const reject = el("button", "reject", "Reject");

    approve.addEventListener("click", () => decide(action.id, "approve", card));
    reject.addEventListener("click", () => decide(action.id, "reject", card));
    actions.append(approve, reject);
    card.append(actions);

    container.append(card);
  }
}

async function decide(id, verdict, card) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const result = await api(`/approvals/${id}/${verdict}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    card.querySelector(".actions").replaceWith(
      notice(
        verdict === "approve" ? "info" : "warn",
        verdict === "approve"
          ? result.executed
            ? "Done."
            : "Approved, but it did not run."
          : "Rejected.",
        verdict === "approve" ? result.result || "" : "",
      ),
    );
    setTimeout(loadApprovals, 900);
  } catch (err) {
    card.append(notice("danger", "That did not work.", String(err)));
  }
}

/* ---- autonomy ---- */

async function loadAutonomy() {
  const data = await api("/me/autonomy");
  const table = $("#autonomy-table");

  const tools = Object.entries(data.tools || {});
  if (!tools.length) {
    table.innerHTML = "";
    table.append(
      el(
        "div",
        "empty",
        "Nothing yet. Every action will ask you first until a track record builds up.",
      ),
    );
  } else {
    table.innerHTML =
      "<table><thead><tr><th>Tool</th><th>Runs unattended</th>" +
      "<th>Approved</th><th>Rejected</th><th>Progress</th></tr></thead><tbody>" +
      tools
        .map(([tool, r]) => {
          const pct = Math.min(100, (r.toward_auto / 5) * 100);
          return `<tr><td class="mono">${tool}</td>
            <td>${r.auto ? "yes" : "no — asks first"}</td>
            <td>${r.approvals}</td><td>${r.rejections}</td>
            <td><span class="progress"><span style="width:${pct}%"></span></span></td></tr>`;
        })
        .join("") +
      "</tbody></table>";
  }

  const journal = $("#journal");
  const recent = data.recent_actions || [];
  journal.innerHTML = recent.length
    ? "<table><thead><tr><th>Tool</th><th>When</th><th>Risk</th><th>Undo</th></tr></thead><tbody>" +
      recent
        .map(
          (a) =>
            `<tr><td class="mono">${a.tool}</td><td>${new Date(a.at).toLocaleString()}</td>
             <td>${a.risk.replace(/_/g, " ")}</td>
             <td>${a.reversible ? "available" : "not reversible"}</td></tr>`,
        )
        .join("") +
      "</tbody></table>"
    : "";
  if (!recent.length) journal.append(el("div", "empty", "Nothing has been done on your behalf yet."));
}

/* ---- systems ---- */

async function loadSystems() {
  const health = await api("/system/health");
  const container = $("#systems");

  container.innerHTML =
    "<table><thead><tr><th>Connector</th><th>Status</th></tr></thead><tbody>" +
    Object.entries(health.connectors)
      .map(
        ([name, status]) =>
          `<tr><td class="mono">${name}</td><td>${
            status === "ok" ? "reachable" : "<strong>unavailable</strong>"
          }</td></tr>`,
      )
      .join("") +
    "</tbody></table>";

  $("#health-dot").className = health.degraded.length ? "dot degraded" : "dot";

  const schedule = await api("/me/schedule");
  const box = $("#schedule");
  box.innerHTML = "";
  if (!schedule.length) {
    box.append(el("div", "empty", "No schedule set — your brief is built when you ask for it."));
  } else {
    const job = schedule[0];
    box.innerHTML = `<table><tbody>
      <tr><td>Runs at</td><td class="mono">${job.at} ${job.timezone}</td></tr>
      <tr><td>Next</td><td class="mono">${
        job.next_run ? new Date(job.next_run).toLocaleString() : "disabled"
      }</td></tr>
      <tr><td>Prepared</td><td>${job.runs} time(s)${
        job.failures ? `, ${job.failures} failed` : ""
      }</td></tr></tbody></table>`;
  }
}

/* ---- boot ---- */

const REFRESH = {
  brief: () => loadBrief(),
  approvals: loadApprovals,
  autonomy: loadAutonomy,
  systems: loadSystems,
};

async function boot() {
  // Ask how this deployment authenticates before doing anything that needs it,
  // so the first thing a signed-out user sees is a sign-in button rather than a
  // wall of failed panels.
  try {
    AUTH = await (await fetch("/auth/mode", { credentials: "same-origin" })).json();
  } catch {
    AUTH = { mode: "unknown", login_url: null };
  }

  try {
    ME = await api("/auth/me");
    $("#who").textContent = `${ME.display_name} · ${ME.roles.join(", ") || "no roles"}`;
    if (AUTH.mode !== "dev") {
      const out = el("button", "ghost", "Sign out");
      out.style.marginTop = "8px";
      out.addEventListener("click", async () => {
        await api("/auth/logout", { method: "POST" }).catch(() => {});
        window.location.reload();
      });
      $("#who").append(document.createElement("br"), out);
    }
  } catch (err) {
    if (err instanceof NotAuthenticated) {
      $("#brief-notices").append(authNotice());
      $("#brief-body").textContent = "";
      $("#brief-subtitle").textContent = "Sign in to see your brief.";
      $("#who").textContent = "not signed in";
      return;
    }
  }

  const params = new URLSearchParams(window.location.search);
  if (params.get("login") === "failed") {
    $("#brief-notices").append(
      notice("danger", "Sign-in did not complete.", "Please try again."),
    );
  }

  loadBrief();
  loadApprovals().catch(() => {});
  loadSystems().catch(() => {});
  // The tiles are built from what this deployment actually has, so a workspace
  // with no chat connector shows no chat tile rather than one that can never
  // light up.
  loadScopes().catch(() => {});
}

boot();
