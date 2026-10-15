"use strict";

// Same server routes as before: /api/status, /api/jobs, /api/capsules and the
// capsule files under /capsules/. Everything here is presentation: the CLI's own
// output is read back into rows and stages, and capsule.json into the report.

const $ = (sel, root = document) => root.querySelector(sel);
const form = $("#run-form");
const logEl = $("#log");
const statusEl = $("#job-status");

const STAGES = ["Scan", "Usage X-Ray", "Propose", "Twin sandbox trial", "Compare", "Exit capsule"];
const STAGE_LABELS = ["Scan", "Usages", "Propose", "Sandboxes", "Compare", "Capsule"];
const STATUS_TEXT = { running: "Running", ok: "Finished", failed: "Failed", idle: "Idle" };
const KIND_TEXT = { scan: "scan", xray: "usage check", trial: "trial", replay: "replay", "gemma-check": "Gemma check" };
const WORKING = {
  scan: ["Scanning dependencies", "Checking each package on PyPI, OSV.dev and deps.dev."],
  xray: ["Reading the code", "Finding imports, aliases, private calls and monkeypatches."],
  replay: ["Replaying the saved trial", "Running both sides again and comparing with the saved results."],
  "gemma-check": ["Checking the Gemma connection", "One small request to the Gemma 4 API."],
};
const DEFAULT_PICKS = [{ name: "jinja2" }, { name: "toml" }, { name: "appdirs" }];
const ACTION_TEXT = { upgrade: "Upgrade", replace: "Replace", remove: "Remove", add: "Add", stdlib: "Move to the standard library" };
const ACTION_LABEL = { upgrade: "Upgrade", replace: "Replace", remove: "Remove", add: "Add", stdlib: "To stdlib" };
const ACTION_VERB = { upgrade: "Upgrading", replace: "Replacing", remove: "Removing", add: "Adding", stdlib: "Moving" };
const VERDICT_TONE = { Accepted: "green", Partial: "amber", Rejected: "red" };
const VERDICT_ICON = { Accepted: "M3.5 8.5 6.5 11.5 12.5 4.5", Partial: "M3.5 8h9", Rejected: "M4.5 4.5l7 7M11.5 4.5l-7 7" };
const SEVERITY_TONE = { critical: "red", high: "red", medium: "amber", low: "blue" };
const KIND_TONE = { private_api: "red", monkeypatch: "red", alias: "amber", config: "amber", transitive: "blue" };

let currentJob = null;
let pollTimer = null;
let rendered = "";
let rawState = "";
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
  for (const kid of kids.flat(3)) if (kid != null && kid !== false) node.append(kid);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

// replaceChildren() would print a null part as the text "null"; drop empty parts first
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(3).filter((k) => k != null && k !== false));
const tagFor = (source) => (source ? h("span", { class: `tag ${source.toLowerCase()}`, text: source }) : null);
const canon = (name) => String(name || "").trim().toLowerCase().replace(/[-_.]+/g, "-");
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const short = (test) => String(test).split("::").pop();
const fileLink = (base, path, text) =>
  h("a", { class: "ev", href: `${base}/${path}`, target: "_blank", rel: "noopener", text: text || path.split("/").pop() });
const evidence = (base, paths) => {
  const unique = [...new Set((paths || []).filter(Boolean))];
  return unique.length ? h("p", { class: "evidence" }, unique.map((p) => fileLink(base, p))) : null;
};
// A short title for a long sentence: the clause before ": " when there is one.
const headline = (text) => {
  const value = String(text);
  const cut = value.indexOf(": ");
  return cut >= 12 ? value.slice(0, cut) : value;
};

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

// --- the loader: a wireframe geodesic icosahedron (42 vertices, 120 edges) ---

const geometry = (() => {
  const canvas = $("#geometry");
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  const phi = (1 + Math.sqrt(5)) / 2;
  const unit = (v) => { const l = Math.hypot(...v); return v.map((x) => x / l); };
  const verts = [[-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0], [0, -1, phi], [0, 1, phi],
    [0, -1, -phi], [0, 1, -phi], [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1]].map(unit);
  const faces = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2],
    [10, 7, 6], [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5], [2, 4, 11],
    [6, 2, 10], [8, 6, 7], [9, 8, 1]];
  // Split every face into four, pushing the new vertices out to the sphere.
  const mids = new Map();
  const mid = (a, b) => {
    const key = a < b ? `${a},${b}` : `${b},${a}`;
    if (!mids.has(key)) {
      verts.push(unit(verts[a].map((x, i) => x + verts[b][i])));
      mids.set(key, verts.length - 1);
    }
    return mids.get(key);
  };
  const edgeKeys = new Set();
  for (const [a, b, c] of faces) {
    const ab = mid(a, b), bc = mid(b, c), ca = mid(c, a);
    for (const [x, y] of [[a, ab], [ab, b], [b, bc], [bc, c], [c, ca], [ca, a], [ab, bc], [bc, ca], [ca, ab]]) {
      edgeKeys.add(x < y ? `${x},${y}` : `${y},${x}`);
    }
  }
  const edges = [...edgeKeys].map((k) => k.split(",").map(Number));
  let raf = 0;
  let running = false;
  let t0 = 0;

  function frame(now) {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const size = Math.round((canvas.clientWidth || 112) * dpr);
    if (canvas.width !== size) canvas.width = canvas.height = size;
    const t = (now - t0) / 1000;
    const ay = t * 0.32;
    const ax = 0.5 + Math.sin(t * 0.17) * 0.22;
    const [cy, sy, cx, sx] = [Math.cos(ay), Math.sin(ay), Math.cos(ax), Math.sin(ax)];
    const half = size / 2;
    const dist = 3.4;
    const scale = size * 0.36;
    const pts = verts.map(([x, y, z], i) => {
      const r = 1 + 0.022 * Math.sin(t * 1.3 + i * 1.7); // slow breathing of each vertex
      const x1 = (x * cy + z * sy) * r;
      const z1 = (-x * sy + z * cy) * r;
      const y2 = y * r * cx - z1 * sx;
      const z2 = y * r * sx + z1 * cx;
      const p = dist / (dist + z2);
      return [half + x1 * scale * p, half + y2 * scale * p, z2];
    });
    ctx.clearRect(0, 0, size, size);
    ctx.lineWidth = dpr;
    for (const [a, b] of edges) {
      const near = (1 - (pts[a][2] + pts[b][2]) / 2) / 2; // 1 = nearest, 0 = farthest
      ctx.strokeStyle = `rgba(70, 194, 216, ${(0.07 + 0.58 * near ** 1.6).toFixed(3)})`;
      ctx.beginPath();
      ctx.moveTo(pts[a][0], pts[a][1]);
      ctx.lineTo(pts[b][0], pts[b][1]);
      ctx.stroke();
    }
    pts.forEach(([x, y, z], i) => {
      const near = (1 - z) / 2;
      const corner = i < 12; // the icosahedron's own twelve vertices
      ctx.fillStyle = corner ? `rgba(230, 235, 238, ${(0.2 + 0.8 * near).toFixed(3)})`
        : `rgba(70, 194, 216, ${(0.12 + 0.6 * near).toFixed(3)})`;
      ctx.beginPath();
      ctx.arc(x, y, (corner ? 2 : 1.2) * dpr * (0.7 + 0.5 * near), 0, Math.PI * 2);
      ctx.fill();
    });
    if (running && !still.matches) raf = requestAnimationFrame(frame);
  }

  return {
    start() {
      if (running) return;
      running = true;
      t0 = performance.now();
      raf = requestAnimationFrame(frame);
    },
    stop() {
      running = false;
      cancelAnimationFrame(raf);
    },
  };
})();

// --- status and picks ---------------------------------------------------------

function setEnv(sel, state, text) {
  const item = $(sel);
  item.className = `env-item ${state}`;
  item.replaceChildren(h("i"), text);
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    setEnv("#pill-docker", s.docker ? "ok" : "warn", s.docker ? "Docker ready" : "Docker off · using uv");
    setEnv("#pill-gemma", s.gemma_key_present ? "ok" : "warn", s.gemma_key_present ? "Gemma API key set" : "Gemma: saved replies");
    if (!$("#repo").value) $("#repo").value = s.default_repo;
    if (s.gemma_key_present && !$("#gemma").dataset.touched) $("#gemma").value = "live";
  } catch (err) {
    showError(`Can't reach the Tranceplant server (${err.message}). Start it with "tranceplant ui", then reload this page.`);
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

const column = (table, prefix) => table.headers.findIndex((x) => x.toLowerCase().startsWith(prefix));

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
  fill($("#readout"),
    h("ul", { class: "deps", "aria-label": "Risky dependencies" }, risky.map(depRow)),
    rest.length ? h("details", { class: "more-details" },
      h("summary", { text: plural(rest.length, "lower-risk package") }),
      h("ul", { class: "deps" }, rest.map(depRow))) : null,
    h("p", { class: "readout-note", text:
      `${plural(risky.length, "package")} scored 40 or more. Pick one to fill in the dependency, then run a trial. Scores add fixed points per finding from PyPI, OSV.dev and deps.dev.` }),
  );
  renderPicks(risky.slice(0, 4));
  return true;
}

function depRow(d) {
  const [tone, level] = d.score >= 60 ? ["red", "High"] : d.score >= 40 ? ["amber", "Medium"] : ["blue", "Low"];
  const first = d.why[0];
  const where = [d.pinned && d.pinned !== "-" ? d.pinned : "unpinned", d.declared].filter(Boolean).join(" · ");
  return h("li", {}, h("button", { type: "button", class: "dep-row", "data-dep": d.name, onclick: () => chooseDep(d.name) },
    h("span", { class: `dep-score tone-${tone}`, text: String(d.score) }),
    h("span", { class: "dep-name" }, d.name, h("small", { text: where })),
    h("span", { class: `dep-level sev ${tone}`, text: level }),
    h("span", { class: "dep-why", title: d.why.map((w) => w.text).join("\n") },
      first ? [tagFor(first.tag), " ", first.text, d.why.length > 1 ? ` (+${d.why.length - 1} more)` : ""] : "No findings")));
}

function renderUsages(job) {
  const table = parseTable(job.output || "");
  if (!table) return false;
  const [where, kind, code, why] = ["where", "kind", "code", "why"].map((p) => column(table, p));
  const summary = ((job.output || "").match(/OBSERVED\s+(\d+) usage\(s\)/) || [])[1];
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
  fill($("#readout"),
    groups.length
      ? h("ul", { class: "usages" }, groups.map((g) => h("li", { class: "usage" },
        h("code", { class: "where", text: g.where }),
        h("span", { class: "kinds" }, [...new Set(g.kinds)].map((k) => {
          const n = g.kinds.filter((x) => x === k).length;
          return h("span", { class: `sev ${KIND_TONE[k] || "gray"}`, text: `${k.replace("_", " ")}${n > 1 ? ` ×${n}` : ""}` });
        })),
        h("code", { class: "code", text: g.code }),
        g.note ? h("span", { class: "why" }, tagFor(g.note.tag), " ", g.note.text) : null)))
      : h("p", { class: "empty", text: "The repo never imports or runs this dependency." }),
    summary ? h("p", { class: "readout-note" }, tagFor("OBSERVED"), ` ${plural(Number(summary), "usage")} found by reading the code's syntax tree.`) : null,
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

function renderStages(job, progress) {
  const { current } = progress;
  $("#stages").replaceChildren(...STAGES.map((name, i) => {
    const n = i + 1;
    let state = "waiting";
    if (job.status === "ok" || n < current) state = "done";
    else if (n === Math.max(current, 1) && job.status === "failed") state = "halted";
    else if (n === current) state = "active";
    return h("li", { class: `stage ${state}`, "aria-current": state === "active" ? "step" : null, title: name, text: STAGE_LABELS[i] });
  }));
}

function failureLine(output) {
  const lines = String(output || "").split("\n").map((l) => l.trim()).filter(Boolean);
  return [...lines].reverse().find((l) => /^error\b|error:|Traceback|Exception/i.test(l)) || lines[lines.length - 1] || "No output.";
}

// --- jobs -----------------------------------------------------------------------

function render(job) {
  currentJob = job;
  const kind = job.label.split(" ")[0];
  const running = job.status === "running";
  const failed = job.status === "failed";
  statusEl.textContent = STATUS_TEXT[job.status] || job.status;
  statusEl.className = `status ${job.status}`;
  $("#job-time").textContent = `${job.seconds}s`;
  $("#job-command").textContent = `tranceplant ${job.command}`;
  $("#job-command").hidden = false;
  $("#idle").hidden = true;
  $("#raw").hidden = false;
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
  logEl.textContent = job.output || (running ? "Starting…" : "(no output)");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  const progress = parseStages(job.output || "");
  $("#working").hidden = !running;
  if (running) {
    const [title, detail] = kind === "trial"
      ? [progress.current ? `Running trial · ${STAGES[progress.current - 1]}` : "Starting the trial",
        (progress.details[progress.current] || []).slice(-1)[0] || "The original and the swapped repo run the same tests."]
      : WORKING[kind] || ["Working", ""];
    $("#working-title").textContent = title;
    $("#working-detail").textContent = detail;
    geometry.start();
  } else {
    geometry.stop();
  }

  $("#stages").hidden = kind !== "trial";
  const key = `${job.id}:${job.status}:${(job.output || "").length}`;
  if (key !== rendered) {
    rendered = key;
    if (kind === "trial") renderStages(job, progress);
    const structured = !running && (kind === "scan" ? renderScan(job) : kind === "xray" ? renderUsages(job) : false);
    $("#readout").hidden = !structured;
    $("#job-error").hidden = !failed;
    if (failed) {
      $("#job-error").replaceChildren(
        h("strong", { text: `The ${KIND_TEXT[kind] || kind} stopped with an error.` }),
        h("code", { text: failureLine(job.output) }));
    }
  }
  if (rawState !== `${job.id}:${job.status}`) {
    rawState = `${job.id}:${job.status}`;
    $("#raw").open = failed || (!running && (kind === "replay" || kind === "gemma-check"));
  }
  setBusy(running);
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
    geometry.stop();
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
form.addEventListener("submit", (event) => event.preventDefault());
$("#dep").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("#trial-button").disabled) $("#trial-button").click();
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

// --- reports --------------------------------------------------------------------

async function loadCapsules() {
  capsuleIndex = await api("/api/capsules").catch(() => []);
  const list = $("#capsule-list");
  if (!capsuleIndex.length) {
    list.replaceChildren(h("li", { class: "empty", text: "No reports yet. Each trial adds one here." }));
    return;
  }
  list.replaceChildren(...capsuleIndex.map((c) =>
    h("li", {}, h("button", { type: "button", class: "report-row", "aria-current": String(c.name === openName), onclick: () => openCapsule(c.name) },
      h("span", { class: "name", text: c.name }),
      h("span", { class: `sev ${VERDICT_TONE[c.verdict] || "gray"}`, text: c.verdict || "Unknown" }),
      h("span", { class: "swap", text: `${c.action || "swap"} → ${c.replacement || "nothing"} · ${c.newly_fixed} fixed · ${c.regressions} broken` })))));
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
    showError(`Couldn't open the ${name} report (${err.message}). Press Refresh and try again.`);
    return;
  }
  const patch = await fetch(`${base}/migration.patch`, { cache: "no-store" }).then((r) => (r.ok ? r.text() : "")).catch(() => "");
  openName = name;
  document.querySelectorAll(".report-row").forEach((row) =>
    row.setAttribute("aria-current", String(row.querySelector(".name").textContent === name)));
  const viewer = $("#viewer");
  renderReport(cap, name, base, patch);
  viewer.hidden = false;
  viewer.classList.remove("enter");
  void viewer.offsetWidth;
  viewer.classList.add("enter");
  viewer.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

const controlNames = (cap) => (cap.control?.results || []).map((r) => r.name);
const donorNames = (cap) => (cap.donor?.results || []).map((r) => r.name);
const testsRan = (cap) => controlNames(cap).length + donorNames(cap).length > 0;

function outcomeSentence(cap) {
  const cmp = cap.comparison || {};
  const p = cap.proposal || {};
  const dep = cap.dependency || {};
  if (!testsRan(cap)) return "The tests never ran, so this result says nothing about the swap.";
  const from = `${dep.name || ""} ${dep.pinned || ""}`.trim();
  const to = p.target_spec || p.replacement || "nothing";
  const broken = (cmp.regressions || []).length;
  const fixed = (cmp.newly_fixed || []).length;
  const verb = ACTION_VERB[p.action] || "Swapping";
  const object = p.action === "remove" ? from : `${from} to ${to}`;
  const effect = broken ? `broke ${plural(broken, "test")}` : "broke no tests";
  return `${verb} ${object} ${effect}${fixed ? ` and fixed ${fixed}` : ""}.`;
}

function nextStep(cap, base) {
  const cmp = cap.comparison || {};
  const broken = (cmp.regressions || []).length;
  const open = (cap.gaps || []).filter((g) => g.status === "open").length;
  const rounds = Math.max(0, (cap.migration || []).length - 1);
  const total = new Set([...controlNames(cap), ...donorNames(cap)]).size;
  if (!testsRan(cap)) {
    return h("p", { class: "attention" }, h("b", { class: "tone-red", text: "Check the sandbox logs" }), " to see why: ",
      [cap.control?.log_file, cap.donor?.log_file].filter(Boolean).map((p, i) => [i ? ", " : "", fileLink(base, p, p)]));
  }
  if (cap.donor && cap.donor.setup_ok === false) {
    return h("p", { class: "attention" }, h("b", { class: "tone-red", text: "The swapped repo could not be set up." }),
      " The donor log explains why: ", cap.donor.log_file ? fileLink(base, cap.donor.log_file, cap.donor.log_file) : "no log");
  }
  if (broken) {
    return h("p", { class: "attention" }, h("b", { class: "tone-red", text: `${plural(broken, "test")} still ${broken === 1 ? "breaks" : "break"}` }),
      ` after ${plural(rounds, "repair round")}. They are listed first below.`);
  }
  if (open) {
    return h("p", { class: "attention" }, h("b", { class: "tone-amber", text: `${plural(open, "open gap")} to review` }),
      " before you apply the patch.");
  }
  return h("p", { class: "attention" }, h("b", { class: "tone-green", text: "Nothing flagged" }),
    ` within what the repo's ${plural(total, "test")} cover. The patch is ready: `, fileLink(base, "migration.patch", "migration.patch"));
}

function finding({ tone, label, title, source, body }) {
  return h("li", {}, h("details", { class: "finding" },
    h("summary", {},
      h("span", { class: `sev ${tone}`, text: label }),
      h("span", { class: "finding-title", text: title }),
      tagFor(source) || h("span")),
    h("div", { class: "finding-body" }, body)));
}

function resultFindings(cap, base) {
  const cmp = cap.comparison || {};
  const before = new Map((cap.control?.results || []).map((r) => [r.name, r]));
  const after = new Map((cap.donor?.results || []).map((r) => [r.name, r]));
  const junits = [cap.control?.junit_file, cap.donor?.junit_file];
  const from = `${cap.dependency?.name || "the original"} ${cap.dependency?.pinned || ""}`.trim();
  const to = cap.proposal?.target_spec || cap.proposal?.replacement || "the replacement";
  const message = (r) => (r && r.message ? h("pre", { text: r.message }) : null);
  const rows = [];

  for (const t of cmp.regressions || []) {
    rows.push(finding({ tone: "red", label: "Newly broken", title: `${short(t)} broke after the swap`, source: "OBSERVED",
      body: [h("p", {}, "Passes with ", h("code", { text: from }), ", fails with ", h("code", { text: to }), "."),
        h("p", {}, h("code", { text: t })), message(after.get(t)), evidence(base, junits)] }));
  }
  // OBSERVED regression gaps repeat the rows above, so only the rest are listed
  const gaps = (cap.gaps || []).filter((g) => !(g.source === "OBSERVED" && (g.related_tests || []).length
    && g.related_tests.every((t) => (cmp.regressions || []).includes(t))));
  const order = { critical: 0, high: 1, medium: 2, low: 3 };
  gaps.sort((a, b) => (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || (order[a.severity] ?? 4) - (order[b.severity] ?? 4));
  for (const g of gaps) {
    rows.push(finding({ tone: g.status === "open" ? SEVERITY_TONE[g.severity] || "gray" : "gray",
      label: `${g.severity} gap${g.status === "open" ? "" : " · resolved"}`, title: headline(g.description), source: g.source,
      body: [h("p", { text: g.description }),
        (g.related_tests || []).length ? h("p", {}, "Tests: ", h("code", { text: g.related_tests.join(", ") })) : null,
        evidence(base, g.evidence)] }));
  }
  for (const t of cmp.still_failing || []) {
    rows.push(finding({ tone: "amber", label: "Still failing", title: `${short(t)} fails on both versions`, source: "OBSERVED",
      body: [h("p", {}, h("code", { text: t })), message(after.get(t)), evidence(base, junits)] }));
  }
  for (const t of cmp.newly_fixed || []) {
    rows.push(finding({ tone: "green", label: "Newly fixed", title: `${short(t)} now passes`, source: "OBSERVED",
      body: [h("p", {}, "Fails with ", h("code", { text: from }), ", passes with ", h("code", { text: to }), "."),
        h("p", {}, h("code", { text: t })),
        before.get(t)?.message ? [h("p", { text: "Failure before the swap:" }), message(before.get(t))] : null,
        evidence(base, junits)] }));
  }
  const passing = cmp.still_passing || [];
  if (passing.length) {
    rows.push(finding({ tone: "green", label: "Passing", title: `${plural(passing.length, "test")} pass on both versions`, source: "OBSERVED",
      body: [h("p", {}, h("code", { text: passing.map(short).join(", ") })), evidence(base, junits)] }));
  }
  for (const t of [...(cmp.only_in_control || []), ...(cmp.only_in_donor || [])]) {
    rows.push(finding({ tone: "gray", label: "One side only", title: `${short(t)} ran on only one side`, source: "OBSERVED",
      body: [h("p", {}, h("code", { text: t })), evidence(base, junits)] }));
  }
  for (const c of cap.concerns || []) {
    rows.push(finding({ tone: "blue", label: "Note", title: headline(c.claim), source: c.source,
      body: [h("p", { text: c.claim }), evidence(base, c.evidence)] }));
  }
  for (const t of cap.suggested_tests || []) {
    rows.push(finding({ tone: "blue", label: t.ran ? "Test ran" : "Suggested test", title: `${t.name}${t.ran ? "" : " (not run)"}`,
      source: t.ran ? "OBSERVED" : "AI", body: [h("p", { text: t.purpose }), t.code ? h("pre", { text: t.code }) : null] }));
  }
  return rows;
}

function riskFindings(cap, base) {
  const r = cap.risk || {};
  const points = new Map((r.raw_sources?.score || []).map((s) => [s.finding, s.points]));
  const rows = (r.indicators?.findings || []).map((c) => {
    const p = points.get(c.claim);
    const tone = p == null ? "gray" : p >= 30 ? "red" : p >= 15 ? "amber" : "blue";
    return finding({ tone, label: p == null ? "Finding" : `+${p} risk`, title: headline(c.claim), source: c.source,
      body: [h("p", { text: c.claim }), evidence(base, c.evidence)] });
  });
  if (r.ai_summary) {
    rows.push(finding({ tone: SEVERITY_TONE[r.ai_severity] || "blue", label: r.ai_severity ? `${r.ai_severity} (AI)` : "Summary",
      title: headline(r.ai_summary.claim), source: r.ai_summary.source,
      body: [h("p", { text: r.ai_summary.claim }), evidence(base, r.ai_summary.evidence)] }));
  }
  return rows;
}

function swapFindings(cap, base) {
  const p = cap.proposal || {};
  const dep = cap.dependency || {};
  const attempts = cap.migration || [];
  const last = attempts[attempts.length - 1];
  const rows = [];
  if (p.reasoning) {
    rows.push(finding({ tone: "blue", label: ACTION_LABEL[p.action] || "Proposal",
      title: `${dep.name} ${dep.pinned || ""} → ${p.target_spec || p.replacement || "nothing"}`.replace(/\s+/g, " "), source: p.reasoning.source,
      body: [h("p", { text: p.reasoning.claim }), evidence(base, p.reasoning.evidence),
        (p.alternatives || []).length ? h("p", {}, h("strong", { text: "Not chosen: " }),
          p.alternatives.map((a, i) => [i ? "; " : "", `${a.name}${a.why_not ? ` (${a.why_not})` : ""}`])) : null] }));
  }
  if (last && last.steps?.length) {
    rows.push(finding({ tone: "blue", label: "Steps",
      title: headline(last.steps[0].claim) + (last.steps.length > 1 ? ` (+${last.steps.length - 1} more)` : ""), source: last.steps[0].source,
      body: [h("ol", {}, last.steps.map((s) => h("li", { text: s.claim }))), evidence(base, last.steps.flatMap((s) => s.evidence || []))] }));
  }
  for (const a of attempts) {
    if (a.diagnosis) {
      rows.push(finding({ tone: "blue", label: `Repair round ${a.round}`, title: headline(a.diagnosis.claim), source: a.diagnosis.source,
        body: [h("p", { text: a.diagnosis.claim }), evidence(base, a.diagnosis.evidence)] }));
    }
    if (a.patch_applies) {
      const clean = /applies cleanly/i.test(a.patch_applies.claim);
      rows.push(finding({ tone: clean ? "green" : "red", label: clean ? "Patch applies" : "Patch failed", title: a.patch_applies.claim,
        source: a.patch_applies.source, body: [h("p", { text: a.patch_applies.claim }), evidence(base, [a.patch_file, ...(a.patch_applies.evidence || [])])] }));
    }
  }
  return rows;
}

function verdictBadge(verdict) {
  const icon = svg("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
  icon.append(svg("path", { d: VERDICT_ICON[verdict] || VERDICT_ICON.Partial }));
  return h("span", { class: `verdict ${verdict}` }, icon, verdict || "Unknown");
}

function renderReport(cap, name, base, patch) {
  const cmp = cap.comparison || {};
  const p = cap.proposal || {};
  const dep = cap.dependency || {};
  const entry = capsuleIndex.find((c) => c.name === name);
  const files = entry?.files || ["capsule.html", "capsule.json", "migration.patch", "replay.sh"];
  const open = (cap.gaps || []).filter((g) => g.status === "open").length;
  const count = (n, label, cls) => h("li", {}, h("b", { class: n ? cls : null, text: String(n) }), label);
  const segments = [["newly_fixed", "fixed"], ["regressions", "broken"], ["still_failing", "failing"], ["still_passing", "passing"],
    ["only_in_control", "other"], ["only_in_donor", "other"]].flatMap(([field, cls]) =>
    (cmp[field] || []).map((t) => h("span", { class: cls, title: `${short(t)}: ${cls}` })));
  const results = resultFindings(cap, base);
  const risk = riskFindings(cap, base);
  const swap = swapFindings(cap, base);
  const runs = [cap.control, ...(cap.donor_rounds?.length ? cap.donor_rounds : [cap.donor])].filter(Boolean);
  const totalTests = new Set([...controlNames(cap), ...donorNames(cap)]).size;

  fill($("#viewer"),
    h("header", { class: "report-head" },
      h("div", {},
        h("p", { class: "step" }, h("b", { text: "03" }), " Report"),
        h("h2", { id: "viewer-title", text: name }),
        h("p", { class: "swap", text: `${dep.name || name} ${dep.pinned || ""} → ${p.target_spec || p.replacement || "nothing"} · ${(ACTION_TEXT[p.action] || "swap").toLowerCase()}`.replace(/\s+/g, " ") })),
      h("div", { class: "report-actions" },
        files.includes("capsule.html") ? h("a", { href: `${base}/capsule.html`, target: "_blank", rel: "noopener", text: "Open full report ↗" }) : null,
        h("button", { type: "button", class: "link replay", onclick: () => start({ command: "replay", dep: name, gemma: "offline" }), text: "Replay trial" }))),

    h("section", { class: "glance", "aria-label": "Outcome" },
      h("div", { class: "outcome" }, verdictBadge(cap.verdict), h("p", { class: "headline", text: outcomeSentence(cap) })),
      nextStep(cap, base),
      testsRan(cap) ? h("ul", { class: "metrics" },
        count((cmp.newly_fixed || []).length, "newly fixed", "tone-green"),
        count((cmp.regressions || []).length, "newly broken", "tone-red"),
        count((cmp.still_failing || []).length, "still failing", "tone-amber"),
        count((cmp.still_passing || []).length, "still passing", ""),
        count(open, open === 1 ? "open gap" : "open gaps", "tone-amber")) : null,
      segments.length ? h("div", { class: "testbar", role: "img", "aria-label": `${plural(segments.length, "test")} by outcome` }, segments) : null,
      cap.verdict_reason ? h("p", { class: "reason" }, tagFor(cap.verdict_reason.source), " ", cap.verdict_reason.claim,
        [...new Set(cap.verdict_reason.evidence || [])].map((e) => fileLink(base, e))) : null),

    h("section", { class: "report-section" },
      h("h3", {}, "Results", h("small", { text: plural(results.length, "item") })),
      results.length ? h("ul", { class: "findings" }, results) : h("p", { class: "empty", text: "No test results or gaps to show." })),

    h("section", { class: "report-section" },
      h("h3", {}, `Why ${dep.name || name} was flagged`, h("small", { text: `risk ${cap.risk?.score ?? "?"}/100 from the scan` })),
      risk.length ? h("ul", { class: "findings" }, risk) : h("p", { class: "empty", text: "The scan recorded no findings." })),

    h("section", { class: "report-section" },
      h("h3", {}, "The swap", h("small", { text: p.model || "" })),
      swap.length ? h("ul", { class: "findings" }, swap) : h("p", { class: "empty", text: "No proposal recorded." })),

    testsRan(cap) ? h("details", { class: "more-details" },
      h("summary", { text: `Compare every test (${totalTests})` }), crossmatch(cap, base)) : null,
    runs.length ? h("details", { class: "more-details" },
      h("summary", { text: `Sandbox runs (${runs.length})` }),
      h("table", { class: "runs" },
        h("thead", {}, h("tr", {}, ["Run", "Backend", "Python", "Exit", "Setup", "Passed", "Evidence"].map((x) => h("th", { scope: "col", text: x })))),
        h("tbody", {}, runs.map((s) => h("tr", {},
          h("td", { text: s.label }), h("td", { text: s.backend || "-" }), h("td", { text: s.python_version || "-" }),
          h("td", { text: s.exit_code ?? "-" }), h("td", { class: s.setup_ok ? null : "tone-red", text: s.setup_ok ? "ok" : "failed" }),
          h("td", { text: `${(s.results || []).filter((r) => r.outcome === "passed").length}/${(s.results || []).length}` }),
          h("td", {}, [s.junit_file, s.log_file].filter(Boolean).map((f, i) => [i ? " " : "", fileLink(base, f)]))))))) : null,
    patch.trim() ? h("details", { class: "more-details" }, h("summary", { text: "Migration patch" }), diffView(patch)) : null,

    h("footer", { class: "report-foot" },
      h("span", { class: "label", text: "Files" }),
      files.map((f) => h("a", { href: `${base}/${f}`, target: "_blank", rel: "noopener", text: f }))),
  );
  setBusy(currentJob?.status === "running");
}

// Every test on one row: the original result on the left, the swapped result on
// the right. A test that changed gets a wire that shifts colour from old to new.
function crossmatch(cap, base) {
  const before = new Map((cap.control?.results || []).map((r) => [r.name, r.outcome]));
  const after = new Map((cap.donor?.results || []).map((r) => [r.name, r.outcome]));
  if (!before.size && !after.size) {
    return h("div", { class: "no-results" }, h("p", { text: "Neither sandbox produced test results." }),
      h("p", { class: "evidence" }, [cap.control?.log_file, cap.donor?.log_file].filter(Boolean).map((l) => fileLink(base, l, l))));
  }
  const cmp = cap.comparison || {};
  const change = new Map();
  for (const [field, key] of [["newly_fixed", "fixed"], ["regressions", "broken"], ["still_failing", "failing"],
    ["still_passing", "passing"], ["only_in_control", "gone"], ["only_in_donor", "added"]]) {
    for (const n of cmp[field] || []) change.set(n, key);
  }
  const rank = { broken: 0, fixed: 1, failing: 2, gone: 3, added: 4, passing: 5 };
  const names = [...new Set([...before.keys(), ...after.keys()])]
    .sort((a, b) => (rank[change.get(a)] ?? 6) - (rank[change.get(b)] ?? 6) || a.localeCompare(b));
  const row = 28;
  const height = names.length * row;
  const changed = (n) => ["fixed", "broken"].includes(change.get(n));
  const dot = (o) => h("span", { class: `dot ${o === "passed" ? "" : o === "skipped" ? "other" : "fail"}`, title: o });
  const cell = (n, map, side) => {
    if (!map.has(n)) return h("div", { class: "xm-row missing" }, h("span", { class: "t", text: "not run" }));
    const c = change.get(n);
    const flag = side === "right" && changed(n)
      ? h("span", { class: `sev ${c === "fixed" ? "green" : "red"}` }, h("span", { class: "flag-text", text: c === "fixed" ? "fixed" : "broken" }))
      : null;
    const parts = side === "left" ? [h("span", { class: "t", text: short(n) }), dot(map.get(n))]
      : [dot(map.get(n)), flag, h("span", { class: "t", text: short(n) })];
    return h("div", { class: `xm-row ${changed(n) ? "changed" : ""}`, title: `${n}: ${map.get(n)}` }, parts);
  };
  const leftCol = h("div", { class: "xm-col xm-left" },
    h("div", { class: "xm-head" }, h("strong", { text: "Original" }), h("span", { text: `${cap.dependency?.name || ""} ${cap.dependency?.pinned || ""}` })),
    names.map((n) => cell(n, before, "left")));
  const rightCol = h("div", { class: "xm-col xm-right" },
    h("div", { class: "xm-head" }, h("strong", { text: "After swap" }), h("span", { text: cap.proposal?.target_spec || cap.proposal?.replacement || "" })),
    names.map((n) => cell(n, after, "right")));

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
    const lines = names.filter((n) => before.has(n) && after.has(n)).map((n) => {
      const y = names.indexOf(n) * row + row / 2;
      const attrs = { d: `M0 ${y} L${width} ${y}`, class: change.get(n) || "" };
      if (changed(n)) attrs.pathLength = "1"; // lets the changed wires draw in
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
  return h("figure", { class: "xm" }, h("div", { class: "crossmatch" }, leftCol, h("div", {}, wires), rightCol));
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
