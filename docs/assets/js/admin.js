/* Admin tab: Garmin credential form, refresh trigger, live job status.
   Talks to the FastAPI backend (webapp/main.py). No CDN, no build step,
   matches app.js's conventions. */

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
    const resp = await fetch(path, Object.assign({}, opts, { headers }));
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("not authenticated");
    }
    return resp;
  }

  async function loadCsrf() {
    const resp = await api("/api/csrf-token");
    const data = await resp.json();
    csrfToken = data.csrf_token;
  }

  // ------------------------------------------------------------ garmin status/form
  async function refreshGarminStatus() {
    const resp = await api("/api/garmin/status");
    const st = await resp.json();
    const el = $("#garmin-status");
    if (st.configured) {
      el.innerHTML =
        `<span class="badge dot" style="--c:${st.token_valid ? "var(--good)" : "var(--warn)"}">
           ${st.token_valid ? "Connected" : "Configured, not yet verified"}</span>
         <div class="seg-note">Account: ${esc(st.username)}</div>
         <div class="seg-note">Last successful auth: ${esc(st.last_successful_auth || "never")}</div>` +
        (st.last_failed_auth ? `<div class="seg-note" style="color:var(--critical)">Last failed auth: ${esc(st.last_failed_auth)}</div>` : "");
    } else {
      el.innerHTML = `<span class="badge dot" style="--c:var(--muted)">Not configured</span>
        <div class="seg-note">Enter your Garmin Connect credentials below.</div>`;
    }
  }

  function initGarminForm() {
    $("#garmin-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const username = $("#garmin-username").value;
      const password = $("#garmin-password").value;
      const msg = $("#garmin-form-msg");
      msg.textContent = "Saving...";
      try {
        const resp = await api("/api/garmin/credentials", {
          method: "POST", body: JSON.stringify({ username, password }),
        });
        // Password field is write-only: clear it immediately either way,
        // it is never re-populated from the server (the server never
        // returns it in the first place).
        $("#garmin-password").value = "";
        if (!resp.ok) {
          msg.textContent = "Could not save credentials — check the server log.";
          msg.className = "admin-msg bad";
          return;
        }
        msg.textContent = "Saved.";
        msg.className = "admin-msg good";
        await refreshGarminStatus();
      } catch {
        $("#garmin-password").value = "";
        msg.textContent = "Request failed.";
        msg.className = "admin-msg bad";
      }
    });
  }

  // ------------------------------------------------------------ rest days
  const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const DAY_LABEL = { monday: "Mon", tuesday: "Tue", wednesday: "Wed", thursday: "Thu",
    friday: "Fri", saturday: "Sat", sunday: "Sun" };

  async function loadRestDays() {
    const resp = await api("/api/race-config");
    const data = await resp.json();
    const current = new Set(data.rest_days || []);
    $("#rest-days-checks").innerHTML = DAYS.map((d) =>
      `<label><input type="checkbox" data-day="${d}" ${current.has(d) ? "checked" : ""}> ${DAY_LABEL[d]}</label>`
    ).join("");
  }

  function initRestDaysForm() {
    $("#rest-days-save-btn").addEventListener("click", async () => {
      const days = Array.from(document.querySelectorAll("#rest-days-checks input:checked"))
        .map((el) => el.dataset.day);
      const msg = $("#rest-days-msg");
      msg.textContent = "Saving...";
      try {
        const resp = await api("/api/race-config/rest-days", {
          method: "POST", body: JSON.stringify({ days }),
        });
        if (!resp.ok) {
          msg.textContent = "Could not save — check the server log.";
          msg.className = "admin-msg bad";
          return;
        }
        msg.textContent = "Saved. Trigger a refresh to apply it to the plan.";
        msg.className = "admin-msg good";
      } catch {
        msg.textContent = "Request failed.";
        msg.className = "admin-msg bad";
      }
    });
  }

  // ------------------------------------------------------------ refresh trigger + polling
  function setRefreshUI(running) {
    $("#refresh-btn").disabled = running;
    $("#refresh-btn").textContent = running ? "Refreshing..." : "Refresh now";
  }

  async function startRefresh() {
    const body = {
      no_llm: $("#opt-no-llm").checked,
      replan: $("#opt-replan").checked,
      no_sync: $("#opt-no-sync").checked,
      note: $("#opt-note").value || null,
    };
    setRefreshUI(true);
    $("#refresh-status").textContent = "Starting...";
    const resp = await api("/api/refresh", { method: "POST", body: JSON.stringify(body) });
    const data = await resp.json();
    if (!data.job_id) {
      $("#refresh-status").textContent = "Could not start refresh.";
      setRefreshUI(false);
      return;
    }
    if (!data.started) {
      $("#refresh-status").textContent = "A refresh is already running — watching it instead.";
    }
    pollJob(data.job_id);
  }

  function pollJob(jobId) {
    if (pollHandle) clearInterval(pollHandle);
    $("#refresh-log").style.display = "";
    const tick = async () => {
      const resp = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!resp.ok) { clearInterval(pollHandle); setRefreshUI(false); return; }
      const job = await resp.json();
      $("#refresh-status").textContent = `Job ${jobId}: ${job.status}`;
      $("#refresh-log").textContent = (job.log_tail || []).join("\n");
      $("#refresh-log").scrollTop = $("#refresh-log").scrollHeight;
      if (job.status === "running") return;

      clearInterval(pollHandle);
      pollHandle = null;
      setRefreshUI(false);
      $("#last-refresh-at").textContent = new Date().toLocaleString();
      if (job.status === "succeeded") {
        $("#last-refresh-error").textContent = "none";
        if (window.RunAnalyzeApp) window.RunAnalyzeApp.reload(); // re-fetch data/*.json, re-render, no page reload
      } else {
        $("#last-refresh-error").textContent = `exit code ${job.exit_code}`;
      }
      await refreshGarminStatus(); // last_successful_auth / last_failed_auth may have changed
    };
    tick();
    pollHandle = setInterval(tick, 2500);
  }

  async function init() {
    await loadCsrf();
    await refreshGarminStatus();
    initGarminForm();
    await loadRestDays();
    initRestDaysForm();
    $("#refresh-btn").addEventListener("click", startRefresh);
  }

  init();
})();
