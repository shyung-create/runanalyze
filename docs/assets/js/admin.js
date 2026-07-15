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
    // No login/401 handling — Tailscale's tailnet-only reachability is the
    // access control, not an app-level session. A 403 here means a CSRF
    // mismatch (stale token after a cookie reset), not "not logged in".
    return fetch(path, Object.assign({}, opts, { headers }));
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

  // ------------------------------------------------------------ shared day constants
  const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  const DAY_LABEL = { monday: "Mon", tuesday: "Tue", wednesday: "Wed", thursday: "Thu",
    friday: "Fri", saturday: "Sat", sunday: "Sun" };
  const DAY_LABEL_FULL = { monday: "Monday", tuesday: "Tuesday", wednesday: "Wednesday",
    thursday: "Thursday", friday: "Friday", saturday: "Saturday", sunday: "Sunday" };

  // ------------------------------------------------------------ race details
  let planCatalog = { half: [], full: [] };

  function populatePlanIdOptions(distanceType, selectedId) {
    const sel = $("#race-plan-id");
    const ids = planCatalog[distanceType] || [];
    const stillValid = ids.includes(selectedId);
    sel.innerHTML = `<option value="">Auto-select</option>` +
      ids.map((id) => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
    sel.value = stillValid ? selectedId : "";
  }

  async function loadRaceDetails() {
    const resp = await api("/api/race-config");
    const data = await resp.json();
    planCatalog = data.plan_catalog || { half: [], full: [] };

    $("#race-name").value = data.name || "";
    $("#race-distance-type").value = data.distance_type || "full";
    $("#race-date").value = data.race_date || "";
    $("#race-target-time").value = data.target_time || "";

    $("#race-long-run-day").innerHTML = DAYS.map((d) =>
      `<option value="${d}">${DAY_LABEL_FULL[d]}</option>`
    ).join("");
    $("#race-long-run-day").value = data.long_run_day || "sunday";

    populatePlanIdOptions(data.distance_type, data.plan_id || "");

    $("#race-activities-weeks-back").value = data.activities_weeks_back || 0;
    $("#race-llm-provider").value = data.llm_provider || "deepseek";
  }

  function initRaceDetailsForm() {
    $("#race-distance-type").addEventListener("change", () => {
      populatePlanIdOptions($("#race-distance-type").value, $("#race-plan-id").value);
    });

    $("#race-details-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {
        name: $("#race-name").value,
        distance_type: $("#race-distance-type").value,
        race_date: $("#race-date").value,
        target_time: $("#race-target-time").value,
        long_run_day: $("#race-long-run-day").value,
        plan_id: $("#race-plan-id").value,
        activities_weeks_back: parseInt($("#race-activities-weeks-back").value, 10) || 0,
        llm_provider: $("#race-llm-provider").value,
      };
      const msg = $("#race-details-msg");
      msg.textContent = "Saving...";
      try {
        const resp = await api("/api/race-config/details", {
          method: "POST", body: JSON.stringify(body),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          msg.textContent = err.detail || "Could not save — check the server log.";
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

  // ------------------------------------------------------------ rest days
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

  // ------------------------------------------------------------ blocked dates (specific rest dates)
  let blockedDates = [];

  function renderBlockedDates() {
    const el = $("#blocked-dates-list");
    if (blockedDates.length === 0) {
      el.innerHTML = `<span class="admin-note empty">No specific rest dates set.</span>`;
      return;
    }
    el.innerHTML = blockedDates.map((d) =>
      `<span class="chip" data-date="${esc(d)}">${esc(d)} <button type="button" data-remove="${esc(d)}" aria-label="Remove ${esc(d)}">&times;</button></span>`
    ).join("");
  }

  async function loadBlockedDates() {
    const resp = await api("/api/race-config");
    const data = await resp.json();
    blockedDates = data.blocked_dates || [];
    renderBlockedDates();
  }

  async function saveBlockedDates(nextDates) {
    const msg = $("#blocked-dates-msg");
    msg.textContent = "Saving...";
    try {
      const resp = await api("/api/race-config/blocked-dates", {
        method: "POST", body: JSON.stringify({ dates: nextDates }),
      });
      if (!resp.ok) {
        msg.textContent = "Could not save — check the server log.";
        msg.className = "admin-msg bad";
        return;
      }
      const data = await resp.json();
      blockedDates = data.blocked_dates || [];
      renderBlockedDates();
      msg.textContent = "Saved. Trigger a refresh to apply it to the plan.";
      msg.className = "admin-msg good";
    } catch {
      msg.textContent = "Request failed.";
      msg.className = "admin-msg bad";
    }
  }

  function initBlockedDatesForm() {
    $("#blocked-date-add-btn").addEventListener("click", () => {
      const input = $("#blocked-date-input");
      const value = input.value;
      if (!value) return;
      if (blockedDates.includes(value)) {
        input.value = "";
        return;
      }
      input.value = "";
      saveBlockedDates([...blockedDates, value]);
    });

    $("#blocked-dates-list").addEventListener("click", (e) => {
      const date = e.target.dataset.remove;
      if (!date) return;
      saveBlockedDates(blockedDates.filter((d) => d !== date));
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
    $("#refresh-btn").addEventListener("click", startRefresh);
    await loadRestDays();
    initRestDaysForm();
    await loadBlockedDates();
    initBlockedDatesForm();
    await loadRaceDetails();
    initRaceDetailsForm();
    await refreshGarminStatus();
    initGarminForm();
  }

  init();
})();
