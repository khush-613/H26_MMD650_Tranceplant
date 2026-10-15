"use strict";

const $ = (sel) => document.querySelector(sel);
const form = $("#run-form");
const logEl = $("#log");
const statusEl = $("#job-status");
let currentJob = null;
let pollTimer = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
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

async function loadStatus() {
  try {
    const s = await api("/api/status");
    const gemma = $("#pill-gemma");
    gemma.textContent = s.gemma_key_present ? "Gemma key: set" : "Gemma key: missing (use fake)";
    gemma.className = "pill " + (s.gemma_key_present ? "ok" : "warn");
    const docker = $("#pill-docker");
    docker.textContent = s.docker ? "Sandbox: Docker" : "Sandbox: uv (Docker not running)";
    docker.className = "pill " + (s.docker ? "ok" : "warn");
    if (!$("#repo").value) $("#repo").value = s.default_repo;
    if (s.gemma_key_present) $("#gemma").value = $("#gemma").dataset.touched ? $("#gemma").value : "live";
  } catch (err) {
    showError(`Could not reach the server: ${err.message}`);
  }
}

function render(job) {
  currentJob = job;
  statusEl.textContent = job.status;
  statusEl.className = `status ${job.status}`;
  $("#job-time").textContent = `${job.seconds}s`;
  $("#job-command").textContent = `tranceplant ${job.command}`;
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
  logEl.textContent = job.output || (job.status === "running" ? "Starting…" : "(no output)");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  setBusy(job.status === "running");
}

async function poll(id) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/jobs/${id}`);
    render(job);
    if (job.status === "running") {
      pollTimer = setTimeout(() => poll(id), 1000);
    } else {
      loadJobs();
      loadCapsules();
    }
  } catch (err) {
    showError(err.message);
    setBusy(false);
  }
}

async function start(payload) {
  showError("");
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
  const data = Object.fromEntries(new FormData(form));
  start({ ...data, command: button.dataset.command });
});
$("#gemma").addEventListener("change", (e) => (e.target.dataset.touched = "1"));

$("#stop").addEventListener("click", async () => {
  if (currentJob) await api(`/api/jobs/${currentJob.id}/stop`, { method: "POST" }).catch(() => {});
});

async function loadJobs() {
  const jobs = await api("/api/jobs").catch(() => []);
  const list = $("#jobs");
  list.replaceChildren(
    ...jobs.slice(0, 12).map((job) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ghost small";
      const name = document.createElement("span");
      name.textContent = job.label;
      const meta = document.createElement("span");
      meta.textContent = `${job.status} · ${job.seconds}s`;
      b.append(name, meta);
      b.addEventListener("click", () => poll(job.id));
      li.append(b);
      return li;
    })
  );
}

async function loadCapsules() {
  const capsules = await api("/api/capsules").catch(() => []);
  const box = $("#capsule-list");
  if (!capsules.length) {
    box.innerHTML = '<p class="empty">No capsules yet. Run a trial to make one.</p>';
    return;
  }
  const tpl = $("#capsule-card");
  box.replaceChildren(
    ...capsules.map((c) => {
      const node = tpl.content.cloneNode(true);
      node.querySelector(".capsule-name").textContent = c.name;
      const verdict = node.querySelector(".verdict");
      verdict.textContent = c.verdict;
      verdict.classList.add(c.verdict);
      node.querySelector(".swap").textContent =
        `${c.action} → ${c.replacement || "(removed)"} · ${c.backend || "?"} · ${(c.gemma || []).join(", ") || "no Gemma"}`;
      node.querySelector(".fixed").textContent = c.newly_fixed;
      node.querySelector(".regressions").textContent = c.regressions;
      node.querySelector(".gaps").textContent = c.open_gaps;
      node.querySelector(".rounds").textContent = c.rounds;
      node.querySelector(".reason").textContent = c.reason || "";
      const links = node.querySelector(".links");
      for (const file of c.files) {
        const a = document.createElement("a");
        a.href = `/capsules/${encodeURIComponent(c.name)}/${file}`;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = file;
        links.append(a);
      }
      node.querySelector(".replay").addEventListener("click", () =>
        start({ command: "replay", dep: c.name, gemma: "offline" })
      );
      return node;
    })
  );
  setBusy(currentJob?.status === "running");
}

$("#refresh").addEventListener("click", () => {
  loadCapsules();
  loadStatus();
});

loadStatus();
loadJobs();
loadCapsules();
