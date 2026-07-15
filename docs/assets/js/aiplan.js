/* AI Plan tab: de novo / blended plan generation, own trigger + result view.
   Separate comparison view only — never touches plan.json or the main
   Overview/Calendar tabs. Talks to the FastAPI backend (webapp/main.py).
   No CDN, no build step, matches admin.js's conventions (own IIFE, CSRF). */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let csrfToken = null;
  let pollHandle = null;

  async function api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (opts.method && opts.method !== "GET") headers["X-CSRF-Token"] = csrfToken;
    return fetch(path, Object.assign({}, opts, { headers }));
  }

  async function loadCsrf() {
    const resp = await api("/api/csrf-token");
    const data = await resp.json();
    csrfToken = data.csrf_token;
  }

  const tbl = (el, head, rows) => {
    $(el).innerHTML =
      `<tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr>` +
      rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${esc(c ?? "—")}</td>`).join("")}</tr>`).join("");
  };

  function renderPlan(plan) {
    if (!plan || !plan.days) {
      $("#aiplan-result-card").style.display = "none";
      return;
    }
    $("#aiplan-result-card").style.display = "";
    const modeLabel = plan.mode === "denovo" ? "De novo" : "Blended";
    $("#aiplan-meta").textContent =
      `${modeLabel} · ${plan.provider} · generated ${new Date(plan.generated_at).toLocaleString()}`;
    $("#aiplan-note").textContent = plan.generation_note || "";
    const u = plan.units === "km" ? "km" : "mi";
    tbl("#table-aiplan", ["Date", "Type", u, "Pace", "Description"],
      plan.days.map((d) => [d.date, d.type, d.distance, d.pace || "—", d.description]));
  }

  async function loadExistingPlan() {
    try {
      const resp = await fetch("/data/llm_plan.json", { cache: "no-store" });
      if (!resp.ok) { renderPlan(null); return; }
      renderPlan(await resp.json());
    } catch {
      renderPlan(null);
    }
  }

  function setButtonsEnabled(enabled) {
    $("#aiplan-denovo-btn").disabled = !enabled;
    $("#aiplan-blended-btn").disabled = !enabled;
  }

  async function startGeneration(mode) {
    setButtonsEnabled(false);
    $("#aiplan-status").textContent = "Starting...";
    let resp, data;
    try {
      resp = await api("/api/ai-plan/generate", {
        method: "POST", body: JSON.stringify({ mode }),
      });
      data = await resp.json();
    } catch {
      $("#aiplan-status").textContent = "Request failed.";
      setButtonsEnabled(true);
      return;
    }
    if (!data.job_id) {
      $("#aiplan-status").textContent = data.detail || "Could not start generation.";
      setButtonsEnabled(true);
      return;
    }
    if (!data.started) {
      $("#aiplan-status").textContent = "A refresh or generation is already running — watching it instead.";
    }
    pollJob(data.job_id);
  }

  function pollJob(jobId) {
    if (pollHandle) clearInterval(pollHandle);
    $("#aiplan-log").style.display = "";
    const tick = async () => {
      const resp = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!resp.ok) { clearInterval(pollHandle); setButtonsEnabled(true); return; }
      const job = await resp.json();
      $("#aiplan-status").textContent = `Job ${jobId}: ${job.status}`;
      $("#aiplan-log").textContent = (job.log_tail || []).join("\n");
      $("#aiplan-log").scrollTop = $("#aiplan-log").scrollHeight;
      if (job.status === "running") return;

      clearInterval(pollHandle);
      pollHandle = null;
      setButtonsEnabled(true);
      if (job.status === "succeeded") {
        await loadExistingPlan();
      } else {
        $("#aiplan-status").textContent = `Job ${jobId}: failed (exit code ${job.exit_code}) — see log above.`;
      }
    };
    tick();
    pollHandle = setInterval(tick, 2500);
  }

  async function init() {
    await loadCsrf();
    await loadExistingPlan();
    $("#aiplan-denovo-btn").addEventListener("click", () => startGeneration("denovo"));
    $("#aiplan-blended-btn").addEventListener("click", () => startGeneration("blended"));
  }

  init();
})();
