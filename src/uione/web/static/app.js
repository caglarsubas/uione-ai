/* UiOne workspace.
 *
 * Vanilla, no build step, no dependencies. For an air-gapped product a
 * node_modules tree is a supply chain the customer's security team has to
 * review and we have to patch; this file is the whole client.
 *
 * The identity headers below stand in for the SSO that replaces them (F5.1).
 * They are deliberately obvious rather than made to look like real auth.
 */

const USER = { id: "alice", roles: "analyst", name: "Alice" };

const H = {
  "Content-Type": "application/json",
  "X-User-Id": USER.id,
  "X-User-Roles": USER.roles,
  "X-User-Name": USER.name,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

async function api(path, options = {}) {
  const res = await fetch(path, { headers: H, ...options });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
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
function renderBrief(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped
    .replace(/^#{1,6}\s*(.+)$/gm, "<h3>$1</h3>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^\s*[-*]\s+/gm, "• ")
    .replace(/^---+$/gm, "");
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
    body.append(notice("danger", "Could not load your brief.", String(err)));
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

  body.innerHTML = renderBrief(brief.body || "(no brief)");

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

  try {
    const reply = await api("/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    pending.textContent = reply.reply || "(no reply)";

    if (reply.tool_calls.length) {
      const trace = el("div", "tool-trace");
      for (const call of reply.tool_calls) {
        const line = el("div");
        line.append(el("span", "tool-name", call.tool || "unknown"));
        line.append(
          el(
            "span",
            null,
            call.held ? " — held for your approval" : call.ok ? " ✓" : ` — ${call.detail || "failed"}`,
          ),
        );
        if (call.repairs?.length) {
          line.append(el("div", "repair", `repaired: ${call.repairs.join("; ")}`));
        }
        trace.append(line);
      }
      pending.append(trace);
    }

    if (reply.notice) {
      pending.append(notice(reply.pending_approvals.length ? "warn" : "info", reply.notice));
    }
    await loadApprovals();
  } catch (err) {
    pending.textContent = "";
    pending.append(notice("danger", "That request failed.", String(err)));
  } finally {
    $("#send").disabled = false;
    input.focus();
  }
}

$("#send").addEventListener("click", send);
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

$("#who").textContent = `${USER.name} · ${USER.roles}`;
loadBrief();
loadApprovals().catch(() => {});
loadSystems().catch(() => {});
