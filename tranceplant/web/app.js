"use strict";

// Same server routes as before: /api/status, /api/jobs, /api/capsules and the
// capsule files under /capsules/. Everything here is presentation: the CLI's
// own output is read back into rows, stages and the capsule view.

const $ = (sel, root = document) => root.querySelector(sel);
const form = $("#run-form");
const logEl = $("#log");
const statusEl = $("#job-status");

const STAGES = ["Scan", "Usage X-Ray", "Propose", "Twin sandbox trial", "Compare", "Exit capsule"];
const STATUS_TEXT = { running: "Running", ok: "Done", failed: "Failed", idle: "Idle" };
const DEFAULT_PICKS = [{ name: "jinja2" }, { name: "toml" }, { name: "appdirs" }];
const WORKING = {
  scan: "Reading the requirements files and checking each package on PyPI, OSV.dev and deps.dev…",
  xray: "Reading the repo's syntax tree for every import, alias, private call and monkeypatch…",
};
const ACTION_TEXT = { upgrade: "Upgrade", replace: "Replace", remove: "Remove", add: "Add", stdlib: "Move to the standard library" };

let currentJob = null;
let pollTimer = null;
let rendered = "";
let rawFor = null;
let capsuleIndex = [];
let openName = null;
const autoOpened = new Set();

// --- small DOM helpers -------------------------------------------------------

function h(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat(2)) if (kid != null && kid !== false) node.append(kid);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

const tagFor = (source) => (source ? h("span", { class: `tag ${source.toLowerCase()}`, text: source }) : null);
const canon = (name) => String(name || "").trim().toLowerCase().replace(/[-_.]+/g, "-");
const fileLink = (base, path, text) =>
  h("a", { class: "ev", href: `${base}/${path}`, target: "_blank", rel: "noopener", text: text || path.split("/").pop() });
const claim = (c, base) => [tagFor(c.source), c.claim, [...new Set(c.evidence || [])].map((p) => fileLink(base, p))];

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function showError(message) {
  const el = $("#form-error");
  el.textContent = message || "";
  el.hidden = !message;
}

function setBusy(busy) {
  form.querySelectorAll("button[data-command]").forEach((b) => (b.disabled = busy));
  document.querySelectorAll(".replay").forEach((b) => (b.disabled = busy));
  $("#stop").hidden = !busy;
}

// --- status and picks ---------------------------------------------------------

function setVital(sel, state, text) {
  const v = $(sel);
  v.className = `vital ${state}`;
  v.replaceChildren(h("i"), text);
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    setVital("#pill-docker", s.docker ? "ok" : "warn", s.docker ? "Sandbox · Docker" : "Sandbox · uv (Docker is off)");
    setVital("#pill-gemma", s.gemma_key_present ? "ok" : "warn",
      s.gemma_key_present ? "Gemma 4 · API key set" : "Gemma 4 · saved replies");
    if (!$("#repo").value) $("#repo").value = s.default_repo;
    if (s.gemma_key_present && !$("#gemma").dataset.touched) $("#gemma").value = "live";
  } catch (err) {
    showError(`Can't reach the Tranceplant server (${err.message}). Start it with "tranceplant ui".`);
  }
}

function syncSelection() {
  const dep = canon($("#dep").value);
  document.querySelectorAll("[data-dep]").forEach((b) => b.setAttribute("aria-pressed", String(canon(b.dataset.dep) === dep)));
}

function chooseDep(name) {
  $("#dep").value = name;
  syncSelection();
  $("#trial-button").classList.add("ready");
}

function renderPicks(items) {
  $("#picks").replaceChildren(...items.map(({ name, score }) =>
    h("button", { type: "button", class: "pick", "data-dep": name, onclick: () => chooseDep(name) },
      score != null ? h("b", { text: score }) : null, name)));
  syncSelection();
}

// --- reading the CLI's tables back into rows ---------------------------------

function parseTable(text) {
  let headers = null;
  const rows = [];
  for (const line of text.split("\n")) {
    if (line.includes("┃")) {
      const cells = line.split("┃").slice(1, -1).map((s) => s.trim());
      headers = headers ? headers.map((x, i) => `${x} ${cells[i] || ""}`.trim()) : cells;
      continue;
    }
    if (!headers || !line.includes("│")) continue;
    const cells = line.split("│").slice(1, -1).map((s) => s.trim());
    if (cells.length !== headers.length) continue;
    if (cells[0] === "" && rows.length) {
      const prev = rows[rows.length - 1];
      cells.forEach((c, i) => { if (c) prev[i] = prev[i] ? `${prev[i]} ${c}` : c; });
    } else {
      rows.push(cells);
    }
  }
  return headers ? { headers, rows } : null;
}

function column(table, prefix) {
  return table.headers.findIndex((x) => x.toLowerCase().startsWith(prefix));
}

function splitFindings(text) {
  return text.split(/\s(?=(?:OBSERVED|AI)\s)/).map((s) => s.trim()).filter(Boolean).map((s) => {
    const m = s.match(/^(OBSERVED|AI)\s+(.*)$/);
    return m ? { tag: m[1], text: m[2] } : { tag: null, text: s };
  });
}

function renderScan(job) {
  const table = parseTable(job.output || "");
  if (!table || !table.rows.length) return false;
  const [name, pinned, declared, score, why] = ["package", "pinned", "declared", "score", "why"].map((p) => column(table, p));
  const deps = table.rows.map((r) => ({
    name: r[name], pinned: r[pinned], declared: r[declared], score: Number(r[score]) || 0, why: splitFindings(r[why] || ""),
  }));
  const risky = deps.filter((d) => d.score >= 40);
  const rest = deps.filter((d) => d.score < 40);
  $("#readout").replaceChildren(
    h("div", { class: "triage" }, risky.map(triageRow)),
    rest.length ? h("details", { class: "more" },
      h("summary", { text: `Show ${rest.length} lower-risk ${rest.length === 1 ? "package" : "packages"}` }),
      h("div", { class: "triage" }, rest.map(triageRow))) : null,
    h("p", { class: "readout-note", text:
      "Pick a dependency, then run a trial. Scores add up fixed points per finding from PyPI, OSV.dev and deps.dev; 40 or more counts as risky." }),
  );
  renderPicks(deps.filter((d) => d.score >= 40).slice(0, 4));
  return true;
}

function triageRow(d) {
  const level = d.score >= 60 ? "high" : d.score >= 40 ? "medium" : "low";
  const first = d.why[0];
  const where = [d.pinned && d.pinned !== "-" ? d.pinned : "unpinned", d.declared].filter(Boolean).join(" · ");
  return h("button", { type: "button", class: "triage-row", "data-dep": d.name, onclick: () => chooseDep(d.name) },
    h("span", { class: `band ${level}` }),
    h("span", { class: "triage-body" },
      h("span", { class: "triage-name" }, d.name, h("small", { text: where })),
      h("span", { class: "triage-why" },
        first ? [tagFor(first.tag), first.text, d.why.length > 1 ? ` (+${d.why.length - 1} more)` : ""] : "No findings.")),
    h("span", { class: "triage-score" }, String(d.score), h("small", { text: `${level} risk` })));
}

function renderUsages(job) {
  const table = parseTable(job.output || "");
  if (!table) return false;
  const [where, kind, code, why] = ["where", "kind", "code", "why"].map((p) => column(table, p));
  const summary = ((job.output || "").match(/OBSERVED\s+(\d+ usage\(s\)[^\n]*)/) || [])[1];
  // one line of code can hold several usages (three names in one import): show it once
  const groups = [];
  for (const r of table.rows) {
    const last = groups[groups.length - 1];
    const note = why >= 0 && r[why] ? splitFindings(r[why])[0] : null;
    if (last && last.where === r[where] && last.code === r[code]) {
      last.kinds.push(r[kind]);
      last.note ||= note;
    } else {
      groups.push({ where: r[where], code: r[code], kinds: [r[kind]], note });
    }
  }
  $("#readout").replaceChildren(
    groups.length
      ? h("div", { class: "usages" }, groups.map((g) => h("div", { class: "usage" },
        h("code", { class: "where", text: g.where }),
        h("span", { class: "kinds" }, [...new Set(g.kinds)].map((k) => {
          const n = g.kinds.filter((x) => x === k).length;
          return h("span", { class: `kind ${k}`, text: `${k.replace("_", " ")}${n > 1 ? ` ×${n}` : ""}` });
        })),
        h("code", { class: "code", text: g.code }),
        g.note ? h("span", { class: "why" }, tagFor(g.note.tag), g.note.text) : null)))
      : h("p", { class: "empty", text: "The repo never imports or runs this dependency." }),
    summary ? h("p", { class: "readout-note" }, tagFor("OBSERVED"), summary) : null,
  );
  return true;
}

// --- trial progress -----------------------------------------------------------

function parseStages(text) {
  const details = {};
  let current = 0;
  const note = (n, line) => (details[n] ||= []).push(line);
  for (const line of text.split("\n")) {
    const stage = line.match(/^\[(\d)\/6\]\s+(.*)$/);
    const verdict = line.match(/^Verdict:\s+(\w+)/);
    if (stage) {
      current = Number(stage[1]);
      const colon = stage[2].indexOf(":");
      if (colon >= 0) note(current, stage[2].slice(colon + 1).trim());
    } else if (verdict) {
      note(6, `Verdict: ${verdict[1]}`);
    } else if (/^error:/i.test(line.trim())) {
      note(current || 1, line.trim());
    } else if (current && /^\s{4,}\S/.test(line)) {
      note(current, line.trim());
    }
  }
  return { current, details };
}

function renderStages(job) {
  const { current, details } = parseStages(job.output || "");
  $("#stages").replaceChildren(...STAGES.map((name, i) => {
    const n = i + 1;
    let state = "waiting";
    if (job.status === "ok" || n < current) state = "done";
    else if (n === Math.max(current, 1) && job.status === "failed") state = "halted";
    else if (n === current) state = "active";
    const lines = (details[n] || []).slice(-3);
    return h("li", { class: `stage ${state}` },
      h("span", { class: "name", text: name }),
      lines.length ? h("span", { class: "detail", text: lines.join(" · ") }) : null);
  }));
}

// --- jobs -----------------------------------------------------------------------

function render(job) {
  currentJob = job;
  const kind = job.label.split(" ")[0];
  statusEl.textContent = STATUS_TEXT[job.status] || job.status;
  statusEl.className = `status ${job.status}`;
  $("#job-time").textContent = `${job.seconds}s`;
  $("#job-command").textContent = `tranceplant ${job.command}`;
  $("#job-command").hidden = false;
  $("#idle").hidden = true;
  $("#raw").hidden = false;
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
  logEl.textContent = job.output || (job.status === "running" ? "Starting…" : "(no output)");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  $("#stages").hidden = kind !== "trial";
  const key = `${job.id}:${job.status}:${(job.output || "").length}`;
  if (key !== rendered) {
    rendered = key;
    if (kind === "trial") renderStages(job);
    let structured = kind === "scan" ? renderScan(job) : kind === "xray" ? renderUsages(job) : false;
    if (!structured && job.status === "running" && WORKING[kind]) {
      $("#readout").replaceChildren(h("p", { class: "working", text: WORKING[kind] }));
      structured = true;
    }
    $("#readout").hidden = !structured;
    if (rawFor !== job.id || (job.status !== "running" && !structured && kind !== "trial")) {
      $("#raw").open = !["scan", "xray", "trial"].includes(kind) || (job.status === "failed" && !structured);
      rawFor = job.id;
    }
  }
  setBusy(job.status === "running");
}

async function poll(id) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/jobs/${id}`);
    render(job);
    if (job.status === "running") {
      pollTimer = setTimeout(() => poll(id), 1000);
      return;
    }
    loadJobs();
    await loadCapsules();
    const [kind, name] = job.label.split(" ");
    const folder = canon(name);
    if (kind === "trial" && !autoOpened.has(job.id) && capsuleIndex.some((c) => c.name === folder)) {
      autoOpened.add(job.id);
      openCapsule(folder);
    }
  } catch (err) {
    showError(err.message);
    setBusy(false);
  }
}

async function start(payload) {
  showError("");
  $("#trial-button").classList.remove("ready");
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    render(job);
    poll(job.id);
  } catch (err) {
    showError(err.message);
  }
}

form.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-command]");
  if (!button) return;
  start({ ...Object.fromEntries(new FormData(form)), command: button.dataset.command });
});
$("#gemma").addEventListener("change", (e) => (e.target.dataset.touched = "1"));
$("#dep").addEventListener("input", syncSelection);
$("#stop").addEventListener("click", async () => {
  if (currentJob) await api(`/api/jobs/${currentJob.id}/stop`, { method: "POST" }).catch(() => {});
});

async function loadJobs() {
  const jobs = await api("/api/jobs").catch(() => []);
  $("#jobs").replaceChildren(...jobs.slice(0, 12).map((job) =>
    h("li", {}, h("button", { type: "button", onclick: () => poll(job.id) },
      h("span", { text: job.label }), h("span", { text: `${STATUS_TEXT[job.status] || job.status} · ${job.seconds}s` })))));
  return jobs;
}

// --- capsules ---------------------------------------------------------------------

async function loadCapsules() {
  capsuleIndex = await api("/api/capsules").catch(() => []);
  const box = $("#capsule-list");
  if (!capsuleIndex.length) {
    box.replaceChildren(h("p", { class: "empty", text: "No capsules yet. Run a trial and its evidence lands here." }));
    return;
  }
  box.replaceChildren(...capsuleIndex.map((c) =>
    h("button", { type: "button", class: "capsule-card", "aria-current": String(c.name === openName), onclick: () => openCapsule(c.name) },
      h("span", { class: "capsule-top" },
        h("span", { class: "capsule-name", text: c.name }), h("span", { class: `stamp ${c.verdict}`, text: c.verdict })),
      h("span", { class: "capsule-swap", text: `${ACTION_TEXT[c.action] || c.action || "Swap"} to ${c.replacement || "nothing"} · ${c.backend || "?"}` }),
      h("span", { class: "capsule-tally" },
        h("span", {}, h("b", { text: c.newly_fixed }), " fixed"),
        h("span", {}, h("b", { text: c.regressions }), " broken"),
        h("span", {}, h("b", { text: c.open_gaps }), " open gaps")))));
  setBusy(currentJob?.status === "running");
}

async function openCapsule(name) {
  const base = `/capsules/${encodeURIComponent(name)}`;
  let cap;
  try {
    const res = await fetch(`${base}/capsule.json`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    cap = await res.json();
  } catch (err) {
    showError(`Couldn't open the ${name} capsule (${err.message}).`);
    return;
  }
  const patch = await fetch(`${base}/migration.patch`, { cache: "no-store" }).then((r) => (r.ok ? r.text() : "")).catch(() => "");
  openName = name;
  document.querySelectorAll(".capsule-card").forEach((card) =>
    card.setAttribute("aria-current", String(card.querySelector(".capsule-name").textContent === name)));
  renderViewer(cap, name, base, patch);
  const viewer = $("#viewer");
  viewer.hidden = false;
  viewer.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function renderViewer(cap, name, base, patch) {
  const p = cap.proposal || {};
  const dep = cap.dependency || {};
  const entry = capsuleIndex.find((c) => c.name === name);
  const files = entry?.files || ["capsule.html", "capsule.json", "migration.patch", "replay.sh"];
  $("#viewer").replaceChildren(
    h("header", { class: "viewer-head" },
      h("div", {},
        h("p", { class: "label", text: "Exit capsule" }),
        h("h2", { id: "viewer-title", text: name }),
        h("p", { class: "swap" }, `${ACTION_TEXT[p.action] || "Swap"} `, h("code", { text: `${dep.name || name} ${dep.pinned || ""}`.trim() }),
          " → ", h("code", { text: p.target_spec || p.replacement || "nothing" }))),
      h("span", { class: `stamp big ${cap.verdict}`, text: cap.verdict }),
      cap.verdict_reason ? h("p", { class: "reason" }, claim(cap.verdict_reason, base)) : null),
    h("div", { class: "viewer-body" },
      h("section", {},
        h("h3", { class: "label section-title", text: "Crossmatch: every test, before and after the swap" }),
        crossmatch(cap, base)),
      h("div", { class: "facts" }, riskCard(cap, base), proposalCard(cap, base), gapsCard(cap, base)),
      patch.trim() ? h("section", {}, h("h3", { class: "label section-title", text: "Migration patch" }), diffView(patch)) : null),
    h("footer", { class: "viewer-foot" },
      files.map((f) => h("a", { href: `${base}/${f}`, target: "_blank", rel: "noopener", text: f === "capsule.html" ? "Open full report" : f })),
      h("button", { type: "button", class: "btn quiet small replay", onclick: () => start({ command: "replay", dep: name, gemma: "offline" }) },
        "Replay trial")),
  );
  setBusy(currentJob?.status === "running");
}

// The signature view: every test on one row, original result on the left and the
// swapped result on the right. A test that changed gets a wire that shifts colour
// from its old result to its new one.
function crossmatch(cap, base) {
  const control = new Map((cap.control?.results || []).map((r) => [r.name, r.outcome]));
  const donor = new Map((cap.donor?.results || []).map((r) => [r.name, r.outcome]));
  if (!control.size && !donor.size) {
    const logs = [cap.control?.log_file, cap.donor?.log_file].filter(Boolean);
    return h("div", { class: "no-results" },
      h("p", {}, h("strong", { text: "Neither sandbox produced test results." })),
      h("p", { text: "The tests never ran, so this verdict says nothing about the swap. The sandbox logs show why:" }),
      h("p", {}, logs.map((l) => fileLink(base, l, l))));
  }
  const cmp = cap.comparison || {};
  const change = new Map();
  for (const [field, key] of [["newly_fixed", "fixed"], ["regressions", "broken"], ["still_failing", "failing"],
    ["still_passing", "passing"], ["only_in_control", "gone"], ["only_in_donor", "added"]]) {
    for (const n of cmp[field] || []) change.set(n, key);
  }
  const rank = { broken: 0, fixed: 1, failing: 2, gone: 3, added: 4, passing: 5 };
  const names = [...new Set([...control.keys(), ...donor.keys()])]
    .sort((a, b) => (rank[change.get(a)] ?? 6) - (rank[change.get(b)] ?? 6) || a.localeCompare(b));
  const row = 30;
  const height = names.length * row;
  const short = (n) => n.split("::").pop();
  const isChanged = (n) => ["fixed", "broken"].includes(change.get(n));
  const dot = (o) => h("span", { class: `dot ${o === "passed" ? "" : o === "skipped" ? "other" : "fail"}`, title: o });
  const cell = (n, map, side) => {
    if (!map.has(n)) return h("div", { class: "xm-row missing" }, h("span", { class: "t", text: "not run" }));
    const c = change.get(n);
    const flag = side === "right" && c === "fixed" ? h("span", { class: "flag fixed", text: "newly fixed" })
      : side === "right" && c === "broken" ? h("span", { class: "flag broken", text: "newly broken" }) : null;
    const parts = side === "left" ? [h("span", { class: "t", text: short(n) }), dot(map.get(n))]
      : [dot(map.get(n)), flag, h("span", { class: "t", text: short(n) })];
    return h("div", { class: `xm-row ${isChanged(n) ? "changed" : ""}`, title: `${n}: ${map.get(n)}` }, parts);
  };
  const leftCol = h("div", { class: "xm-col xm-left" },
    h("div", { class: "xm-head" }, h("strong", { text: "Original" }), h("span", { text: `${cap.dependency?.name || ""} ${cap.dependency?.pinned || ""}` })),
    names.map((n) => cell(n, control, "left")));
  const rightCol = h("div", { class: "xm-col xm-right" },
    h("div", { class: "xm-head" }, h("strong", { text: "After swap" }), h("span", { text: cap.proposal?.target_spec || cap.proposal?.replacement || "" })),
    names.map((n) => cell(n, donor, "right")));

  const wires = svg("svg", { class: "xm-wires", height, "aria-hidden": "true" });
  const draw = () => {
    const width = wires.clientWidth || 160;
    wires.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const defs = svg("defs");
    for (const [id, from, to] of [["xm-fixed", "var(--red)", "var(--green)"], ["xm-broken", "var(--green)", "var(--red)"]]) {
      const grad = svg("linearGradient", { id, gradientUnits: "userSpaceOnUse", x1: 0, y1: 0, x2: width, y2: 0 });
      grad.append(svg("stop", { offset: "0.2", style: `stop-color:${from}` }), svg("stop", { offset: "0.8", style: `stop-color:${to}` }));
      defs.append(grad);
    }
    const lines = names.filter((n) => control.has(n) && donor.has(n)).map((n) => {
      const y = names.indexOf(n) * row + row / 2;
      const attrs = { d: `M0 ${y} L${width} ${y}`, class: change.get(n) || "" };
      if (isChanged(n)) attrs.pathLength = "1"; // lets the changed wires draw in
      return svg("path", attrs);
    });
    wires.replaceChildren(defs, ...lines);
  };
  let drawn = false;
  new ResizeObserver(() => {
    if (drawn) wires.classList.add("settled");
    draw();
    drawn = true;
  }).observe(wires);

  const count = (key, label, cls = "") => h("span", { class: `count ${cls}` }, h("b", { text: (cmp[key] || []).length }), label);
  const label = `${(cmp.newly_fixed || []).length} newly fixed, ${(cmp.regressions || []).length} newly broken, ` +
    `${(cmp.still_passing || []).length} still passing, ${(cmp.still_failing || []).length} still failing`;
  return h("figure", { class: "xm", "aria-label": `Test crossmatch: ${label}` },
    h("div", { class: "crossmatch" }, leftCol, h("div", {}, wires), rightCol),
    h("figcaption", { class: "tally" },
      count("newly_fixed", "newly fixed", "fixed"), count("regressions", "newly broken", "broken"),
      count("still_passing", "still passing"), count("still_failing", "still failing"),
      h("span", { class: "tally-source" }, tagFor("OBSERVED"),
        [...new Set([cap.control?.junit_file, cap.donor?.junit_file].filter(Boolean))].map((p) => fileLink(base, p)))));
}

function riskCard(cap, base) {
  const r = cap.risk || {};
  const findings = r.indicators?.findings || [];
  return h("div", { class: "fact-card" },
    h("h3", { class: "label section-title", text: "Why it was risky" }),
    h("p", { class: "score-line" }, h("b", { text: r.score ?? "?" }), h("span", { class: "muted", text: "risk score out of 100" })),
    h("ul", {},
      findings.slice(0, 4).map((c) => h("li", {}, claim(c, base))),
      r.ai_summary ? h("li", {}, claim(r.ai_summary, base)) : null));
}

function proposalCard(cap, base) {
  const p = cap.proposal || {};
  const steps = (cap.migration || []).slice(-1)[0]?.steps || [];
  return h("div", { class: "fact-card" },
    h("h3", { class: "label section-title", text: "What Gemma proposed" }),
    h("p", { class: "meta", text: `${ACTION_TEXT[p.action] || p.action || "Swap"} to ${p.replacement || "nothing"} · ${p.model || "Gemma 4"}` }),
    h("ul", {},
      p.reasoning ? h("li", {}, claim(p.reasoning, base)) : null,
      steps.slice(0, 3).map((s) => h("li", {}, claim(s, base))),
      (p.alternatives || []).map((a) => h("li", {}, tagFor("AI"), `Not chosen: ${a.name}${a.why_not ? `, ${a.why_not}` : ""}`))));
}

function gapsCard(cap, base) {
  const items = [
    ...(cap.gaps || []).map((g) => h("li", {}, tagFor(g.source), h("strong", { text: `${g.severity}, ${g.status}: ` }), g.description,
      (g.evidence || []).map((p) => fileLink(base, p)))),
    ...(cap.concerns || []).map((c) => h("li", {}, claim(c, base))),
    ...(cap.suggested_tests || []).map((t) => h("li", {}, tagFor(t.ran ? "OBSERVED" : "AI"),
      `Suggested test ${t.name}: ${t.purpose}${t.ran ? "" : " (not run)"}`)),
  ];
  return h("div", { class: "fact-card" },
    h("h3", { class: "label section-title", text: "Gaps and concerns" }),
    items.length ? h("ul", {}, items) : h("p", { class: "meta", text: "No open gaps or concerns." }));
}

function diffView(text) {
  const kind = (line) => /^(\+\+\+|---|diff )/.test(line) ? "file" : line.startsWith("@@") ? "hunk"
    : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : null;
  return h("pre", { class: "diff", tabindex: "0", "aria-label": "Migration patch" },
    text.replace(/\n$/, "").split("\n").map((line) => h("span", { class: kind(line), text: line || " " })));
}

// --- start ---------------------------------------------------------------------------

$("#refresh").addEventListener("click", () => { loadCapsules(); loadStatus(); });
$("#raw").hidden = true;
renderPicks(DEFAULT_PICKS);
loadStatus();
loadCapsules();
loadJobs().then((jobs) => {
  const running = jobs.find((j) => j.status === "running");
  if (running) poll(running.id);
});
