/* Running dashboard — reads static JSON from ./data/, renders all panels.
   No build step, no external calls beyond the Chart.js CDN. */

(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // Workout type -> categorical palette slot (fixed assignment, never cycled)
  const TYPE_META = {
    easy:      { label: "Easy",      slot: "--s1" },
    long:      { label: "Long run",  slot: "--s5" },
    tempo:     { label: "Tempo",     slot: "--s3" },
    intervals: { label: "Intervals", slot: "--s6" },
    race_pace: { label: "Race pace", slot: "--s7" },
    cross:     { label: "Cross",     slot: "--s2" },
    race:      { label: "RACE",      slot: "--s8" },
    recovery:  { label: "Recovery",  slot: "--s1" },
    rest:      { label: "Rest",      slot: "--axis" },
  };
  const typeColor = (t) => cssVar((TYPE_META[t] || TYPE_META.rest).slot);
  const typeLabel = (t) => (TYPE_META[t] || { label: t }).label;

  const fmtHms = (s) => {
    if (s == null) return "";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
             : `${m}:${String(sec).padStart(2, "0")}`;
  };
  const fmtPace = (s) => (s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}` : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const shortDate = (iso) => {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  };
  const todayISO = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  const D = {}; // loaded data
  let charts = [];
  const unitAbbr = () => (D.plan?.units === "km" ? "km" : "mi");

  // ------------------------------------------------------------ theme
  const applyTheme = (t) => {
    if (t) document.documentElement.dataset.theme = t;
    localStorage.setItem("theme", t || "");
  };
  const savedTheme = localStorage.getItem("theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    applyTheme(cur === "dark" ? "light" : "dark");
    renderCharts();
  });

  // ------------------------------------------------------------ tabs
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("nav.tabs button").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll("section.page").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      $(`#page-${b.dataset.page}`).classList.add("active");
      renderCharts(); // canvases sized 0 while hidden — render on reveal
    }));

  // ------------------------------------------------------------ load
  async function loadJSON(name) {
    try {
      const r = await fetch(`data/${name}?v=${Date.now()}`);
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  async function init() {
    const [plan, metrics, activities, strategy, race, revlog, meta] = await Promise.all(
      ["plan.json", "metrics.json", "activities.json", "race_strategy.json",
       "race.json", "revision_log.json", "meta.json"].map(loadJSON));
    Object.assign(D, { plan, metrics, activities, strategy, race, revlog, meta });
    if (!plan && !metrics) { $("#load-error").style.display = "block"; return; }
    if (meta?.refreshed_at)
      $("#refreshed-at").textContent =
        "updated " + new Date(meta.refreshed_at).toLocaleDateString() +
        (meta.sample ? " (sample data)" : "");
    renderOverview();
    initCalendar();
    renderTables();
    renderRaceInfo();
    renderLog();
    renderCharts();
  }

  // ------------------------------------------------------------ overview
  function renderOverview() {
    const race = D.plan?.race || {};
    const t = todayISO();
    if (race.race_date) {
      const days = Math.round((new Date(race.race_date) - new Date(t)) / 864e5);
      $("#countdown").textContent = days > 0 ? days : (days === 0 ? "TODAY" : "done");
      $("#countdown-label").textContent = days > 0
        ? `days until ${race.name || race.distance_type + " race"} — ${shortDate(race.race_date)}`
        : (days === 0 ? "It's race day. Trust the training." : "Race completed");
    }

    const weekly = D.metrics?.weekly || [];
    const thisWeek = weekly[weekly.length - 1];
    const lastWeek = weekly[weekly.length - 2];
    const acwr = D.metrics?.load_ratio;
    const tiles = [
      { label: "Goal time", value: D.race?.goal_time || "—" },
      { label: "Projected finish", value: D.race?.projected_time || "—",
        delta: D.metrics?.fitness?.best_effort
          ? `from ${D.metrics.fitness.best_effort.distance} ${unitAbbr()} @ ${fmtPace(D.metrics.fitness.best_effort.avg_pace_s)}` : "" },
      { label: `This week (${unitAbbr()})`, value: thisWeek ? thisWeek.distance : "—",
        delta: lastWeek ? `${lastWeek.distance} last week` : "" },
      { label: "Load ratio", value: acwr?.current ?? "—",
        delta: acwr ? acwr.flag : "", cls: acwr?.flag === "high" ? "bad" : (acwr?.flag === "ok" ? "good" : "") },
    ];
    $("#overview-tiles").innerHTML = tiles.map((x) => `
      <div class="tile"><div class="label">${esc(x.label)}</div>
        <div class="value">${esc(x.value)}</div>
        ${x.delta ? `<div class="delta ${x.cls || ""}">${esc(x.delta)}</div>` : ""}</div>`).join("");

    const ga = D.plan?.goal_assessment;
    if (ga) {
      $("#goal-assessment").innerHTML =
        `<span class="badge">${esc(ga.verdict)}</span>` +
        (ga.notes || []).map((n) => `<div class="assess ${esc(ga.verdict)}">${esc(n)}</div>`).join("") +
        `<div class="seg-note">Tier: ${esc(D.plan.tier)} — ${esc(D.plan.tier_reason)}</div>`;
    }

    const rl = D.revlog || [];
    if (rl.length) {
      const last = rl[rl.length - 1];
      $("#latest-revision").textContent = last.note;
    }

    // this week's plan (Mon-Sun containing today)
    const dt = new Date(t + "T00:00:00");
    const monday = new Date(dt); monday.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
    const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const days = (D.plan?.days || []).filter((d) => d.date >= iso(monday) && d.date <= iso(sunday));
    $("#week-plan").innerHTML = days.map((d) => planRow(d, t)).join("") ||
      `<div class="empty-state">No plan days this week.</div>`;
  }

  const compMark = (d) =>
    d.completion === "done" ? `<span class="mark done">✓</span>` :
    d.completion === "partial" ? `<span class="mark partial">◐</span>` :
    d.completion === "missed" ? `<span class="mark missed">✗</span>` : "";

  function planRow(d, today) {
    const dist = d.distance ? `${d.distance} ${unitAbbr()}` : "";
    const pace = d.pace ? ` @ ${d.pace}` : "";
    return `<li class="${d.date === today ? "today-row" : ""}">
      <span class="d">${esc(shortDate(d.date))}</span>
      <span class="t"><span class="badge dot" style="--c:${typeColor(d.type)}">${esc(typeLabel(d.type))}</span></span>
      <span class="desc">${esc(dist)}${esc(pace)}${d.revised ? " ✎" : ""} ${compMark(d)}
        <span class="seg-note">${esc(d.description || "")}</span></span></li>`;
  }

  // ------------------------------------------------------------ calendar
  let calMonth; // Date at first of displayed month
  function initCalendar() {
    const t = new Date(todayISO() + "T00:00:00");
    calMonth = new Date(t.getFullYear(), t.getMonth(), 1);
    $("#cal-prev").onclick = () => { calMonth.setMonth(calMonth.getMonth() - 1); renderCalendar(); };
    $("#cal-next").onclick = () => { calMonth.setMonth(calMonth.getMonth() + 1); renderCalendar(); };
    $("#view-grid").onclick = () => setCalView(true);
    $("#view-list").onclick = () => setCalView(false);
    $("#cal-legend").innerHTML = Object.entries(TYPE_META)
      .filter(([k]) => k !== "recovery")
      .map(([k, v]) => `<span class="badge dot" style="--c:${cssVar(v.slot)}">${esc(v.label)}</span>`).join("");
    renderCalendar();
  }
  function setCalView(grid) {
    $("#calendar-grid").style.display = grid ? "" : "none";
    $("#calendar-list").style.display = grid ? "none" : "";
    $("#view-grid").classList.toggle("active", grid);
    $("#view-list").classList.toggle("active", !grid);
  }
  function renderCalendar() {
    const y = calMonth.getFullYear(), m = calMonth.getMonth();
    $("#cal-month").textContent = calMonth.toLocaleDateString(undefined, { month: "long", year: "numeric" });
    const byDate = {};
    (D.plan?.days || []).forEach((d) => { byDate[d.date] = d; });
    const t = todayISO();
    const first = new Date(y, m, 1);
    const startPad = (first.getDay() + 6) % 7; // Monday-first
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    let html = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
      .map((d) => `<div class="dowh">${d}</div>`).join("");
    for (let i = 0; i < startPad; i++) html += `<div class="cal-day empty"></div>`;
    for (let day = 1; day <= daysInMonth; day++) {
      const iso = `${y}-${String(m + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      const p = byDate[iso];
      html += `<div class="cal-day ${iso === t ? "today" : ""}">
        <div class="dnum">${day}</div>`;
      if (p) {
        html += compMark(p);
        const dist = p.distance ? `<span class="dist">${p.distance}</span>` : "";
        html += `<div class="wtype" style="--c:${typeColor(p.type)}" title="${esc(p.description || "")}">
          ${dist} ${esc(typeLabel(p.type))}${p.pace ? `<br>@ ${esc(p.pace)}` : ""}</div>`;
        if (p.actual?.distance)
          html += `<div class="seg-note">ran ${p.actual.distance}${p.actual.avg_pace ? ` @ ${esc(p.actual.avg_pace)}` : ""}</div>`;
      }
      html += `</div>`;
    }
    $("#calendar-grid").innerHTML = `<div class="calendar">${html}</div>`;

    const monthDays = (D.plan?.days || []).filter((d) => d.date.startsWith(`${y}-${String(m + 1).padStart(2, "0")}`));
    $("#calendar-list").innerHTML = monthDays.map((d) => planRow(d, t)).join("") ||
      `<div class="empty-state">No plan days this month.</div>`;
  }

  // ------------------------------------------------------------ charts
  function baseOpts() {
    const grid = cssVar("--grid"), muted = cssVar("--muted");
    return {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar("--surface"), titleColor: cssVar("--ink"),
          bodyColor: cssVar("--ink-2"), borderColor: cssVar("--border"), borderWidth: 1,
          padding: 10, displayColors: false,
        },
      },
      scales: {
        x: { grid: { display: false }, border: { color: cssVar("--axis") },
             ticks: { color: muted, maxRotation: 0, autoSkip: true } },
        y: { grid: { color: grid, drawTicks: false }, border: { display: false },
             ticks: { color: muted, font: { size: 11 } }, beginAtZero: true },
      },
    };
  }

  function renderCharts() {
    if (!window.Chart || !D.metrics) return;
    charts.forEach((c) => c.destroy());
    charts = [];
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';

    const s1 = cssVar("--s1"), s5 = cssVar("--s5");
    const weekly = D.metrics.weekly || [];
    const wLabel = (w) => {
      const d = new Date(w.week_start + "T00:00:00");
      return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
    };
    $("#weekly-units").textContent = `(${unitAbbr()}/week, last 12 weeks)`;

    charts.push(new Chart($("#chart-weekly"), {
      type: "bar",
      data: { labels: weekly.map(wLabel),
        datasets: [{ data: weekly.map((w) => w.distance), backgroundColor: s1,
          borderRadius: { topLeft: 4, topRight: 4 }, maxBarThickness: 24,
          categoryPercentage: 0.72 }] },
      options: (() => { const o = baseOpts();
        o.plugins.tooltip.callbacks = { label: (c) => {
          const w = weekly[c.dataIndex];
          return [`${w.distance} ${unitAbbr()} in ${w.runs} run${w.runs === 1 ? "" : "s"}`,
                  w.time_s ? `time: ${fmtHms(w.time_s)}` : ""].filter(Boolean); } };
        return o; })(),
    }));

    const longs = D.metrics.long_runs || [];
    charts.push(new Chart($("#chart-longrun"), {
      type: "bar",
      data: { labels: longs.map(wLabel),
        datasets: [{ data: longs.map((l) => l.distance), backgroundColor: s5,
          borderRadius: { topLeft: 4, topRight: 4 }, maxBarThickness: 24,
          categoryPercentage: 0.72 }] },
      options: (() => { const o = baseOpts();
        o.plugins.tooltip.callbacks = { label: (c) => {
          const l = longs[c.dataIndex];
          return l.distance ? [`${l.distance} ${unitAbbr()} on ${l.date || "?"}`,
            l.avg_pace_s ? `pace ${fmtPace(l.avg_pace_s)}/${unitAbbr()}` : ""].filter(Boolean)
            : "no long run"; } };
        return o; })(),
    }));

    const eff = D.metrics.aerobic_efficiency || { points: [] };
    $("#eff-note").textContent = eff.note || "";
    charts.push(new Chart($("#chart-eff"), {
      type: "line",
      data: { labels: eff.points.map((p) => p.date.slice(5)),
        datasets: [{ data: eff.points.map((p) => p.efficiency), borderColor: s1,
          borderWidth: 2, pointRadius: 4, pointBackgroundColor: s1,
          pointBorderColor: cssVar("--surface"), pointBorderWidth: 2,
          tension: 0.3, fill: false }] },
      options: (() => { const o = baseOpts(); o.scales.y.beginAtZero = false;
        o.plugins.tooltip.callbacks = { label: (c) => {
          const p = eff.points[c.dataIndex];
          return [`efficiency ${p.efficiency}`, `pace ${fmtPace(p.avg_pace_s)}/${unitAbbr()} @ ${p.avg_hr} bpm`]; } };
        return o; })(),
    }));

    const acwr = D.metrics.load_ratio || { series: [] };
    $("#acwr-note").textContent = acwr.note || "";
    const pts = acwr.series.filter((p) => p.acwr != null);
    charts.push(new Chart($("#chart-acwr"), {
      type: "line",
      data: { labels: pts.map((p) => p.date.slice(5)),
        datasets: [
          { data: pts.map((p) => p.acwr), borderColor: s1, borderWidth: 2,
            pointRadius: 0, tension: 0.3, order: 0 },
          // safe band 0.8–1.3 as a neutral wash (not a series)
          { data: pts.map(() => 1.3), borderWidth: 0, pointRadius: 0, fill: "+1",
            backgroundColor: cssVar("--grid") + "80", order: 1 },
          { data: pts.map(() => 0.8), borderWidth: 0, pointRadius: 0, fill: false, order: 1 },
        ] },
      options: (() => { const o = baseOpts(); o.scales.y.beginAtZero = false;
        o.scales.y.suggestedMin = 0.5; o.scales.y.suggestedMax = 1.8;
        o.plugins.tooltip.filter = (c) => c.datasetIndex === 0;
        o.plugins.tooltip.callbacks = { label: (c) => `ACWR ${c.parsed.y} (safe band 0.8–1.3)` };
        return o; })(),
    }));

    renderElevChart();
  }

  function renderElevChart() {
    const s = D.strategy;
    const card = $("#strategy-card");
    if (!s || !s.configured) {
      card.querySelector(".chart-wrap").innerHTML =
        `<div class="empty-state">${esc(s?.note || "Race strategy not configured yet.")}</div>`;
      $("#segments-card").style.display = "none";
      $("#fueling-card").style.display = "none";
      return;
    }
    $("#segments-card").style.display = "";
    $("#fueling-card").style.display = "";
    $("#strategy-sub").textContent =
      `goal ${s.goal_time} · segments from ${s.source === "vision" ? "elevation image" : "course_segments.yaml"}`;

    let wrap = card.querySelector(".chart-wrap");
    wrap.innerHTML = '<canvas id="chart-elev"></canvas>';
    const profile = s.elevation_profile || [];
    const minElev = Math.min(...profile.map((p) => p.elevation));
    const elevAt = (x) => {
      for (let i = 1; i < profile.length; i++) {
        if (x <= profile[i].distance) {
          const a = profile[i - 1], b = profile[i];
          const f = (x - a.distance) / (b.distance - a.distance || 1);
          return a.elevation + f * (b.elevation - a.elevation);
        }
      }
      return profile[profile.length - 1]?.elevation ?? 0;
    };
    const hyd = (s.hydration || []).map((h) => ({ x: h.at, y: elevAt(h.at), meta: h }));
    const s1 = cssVar("--s1"), s2 = cssVar("--s2");

    // draws segment boundaries + per-segment pace labels above the profile
    const segPlugin = {
      id: "segments",
      afterDraw(chart) {
        const { ctx, chartArea, scales } = chart;
        ctx.save();
        ctx.strokeStyle = cssVar("--grid");
        ctx.fillStyle = cssVar("--ink-2");
        ctx.textAlign = "center";
        ctx.font = "600 11px system-ui, sans-serif";
        (s.segments || []).forEach((seg, i) => {
          const x0 = scales.x.getPixelForValue(seg.start);
          const x1 = scales.x.getPixelForValue(seg.end);
          if (i > 0) {
            ctx.beginPath(); ctx.moveTo(x0, chartArea.top); ctx.lineTo(x0, chartArea.bottom); ctx.stroke();
          }
          const cx = (x0 + x1) / 2;
          if (x1 - x0 > 34) ctx.fillText(seg.target_pace, cx, chartArea.top + 12);
        });
        ctx.restore();
      },
    };

    charts.push(new Chart($("#chart-elev"), {
      type: "line",
      data: { datasets: [
        { label: "Elevation", data: profile.map((p) => ({ x: p.distance, y: p.elevation })),
          borderColor: s1, borderWidth: 2, pointRadius: 0, fill: "origin",
          backgroundColor: s1 + "1a", tension: 0, order: 1 },
        { label: "Hydration", data: hyd, type: "scatter", pointRadius: 6,
          pointHoverRadius: 8, pointBackgroundColor: s2,
          pointBorderColor: cssVar("--surface"), pointBorderWidth: 2, order: 0 },
      ] },
      options: (() => { const o = baseOpts();
        o.interaction = { mode: "nearest", intersect: false };
        o.scales.x = { type: "linear", min: 0, max: s.segments[s.segments.length - 1].end,
          grid: { display: false }, border: { color: cssVar("--axis") },
          ticks: { color: cssVar("--muted"),
            callback: (v) => `${v} ${unitAbbr()}` } };
        o.scales.y.beginAtZero = false;
        o.scales.y.suggestedMin = minElev - 30;
        o.scales.y.ticks.display = false;
        o.scales.y.grid.display = false;
        o.plugins.tooltip.callbacks = {
          title: () => "",
          label: (c) => {
            if (c.datasetIndex === 1) {
              const h = c.raw.meta;
              return `${h.type} station @ ${h.at} ${unitAbbr()}${h.eta ? ` (~${h.eta})` : ""}`;
            }
            const seg = s.segments.find((g) => c.parsed.x >= g.start && c.parsed.x <= g.end);
            return seg ? [`${seg.name}`, `target ${seg.target_pace}/${unitAbbr()} — ${seg.effort_note}`]
                       : `${c.parsed.x} ${unitAbbr()}`;
          } };
        return o; })(),
      plugins: [segPlugin],
    }));

    $("#elev-legend").innerHTML =
      `<span class="badge dot" style="--c:${s1}">Elevation (pace labels above)</span>` +
      `<span class="badge dot" style="--c:${s2}">Hydration station</span>`;
  }

  // ------------------------------------------------------------ tables
  const tbl = (el, head, rows) => {
    $(el).innerHTML =
      `<tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr>` +
      rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${esc(c ?? "—")}</td>`).join("")}</tr>`).join("");
  };

  function renderTables() {
    const u = unitAbbr();
    const weekly = D.metrics?.weekly || [];
    tbl("#table-weekly", ["Week of", u, "Runs", "Time"],
      weekly.map((w) => [w.week_start, w.distance, w.runs, fmtHms(w.time_s)]));
    tbl("#table-longrun", ["Week of", u, "Pace"],
      (D.metrics?.long_runs || []).map((l) => [l.week_start, l.distance || "—", fmtPace(l.avg_pace_s)]));
    tbl("#table-eff", ["Date", "Efficiency", "Pace", "HR"],
      (D.metrics?.aerobic_efficiency?.points || []).map((p) =>
        [p.date, p.efficiency, fmtPace(p.avg_pace_s), p.avg_hr]));
    tbl("#table-acwr", ["Date", "ACWR"],
      (D.metrics?.load_ratio?.series || []).slice(-14).map((p) => [p.date, p.acwr ?? "—"]));
    tbl("#table-runs", ["Date", u, "Time", "Pace", "HR", "Cadence", "Ascent", "TE"],
      (D.activities?.runs || []).slice().reverse().map((r) =>
        [r.date, r.distance, fmtHms(r.duration_s), fmtPace(r.avg_pace_s), r.avg_hr,
         r.cadence_spm, r.ascent, r.training_effect]));

    const s = D.strategy;
    if (s?.configured) {
      tbl("#table-segments", ["Segment", `Start (${u})`, `End (${u})`, "Grade %", "Pace", "Cum. time"],
        s.segments.map((g) => [g.name, g.start, g.end, g.avg_grade_pct, g.target_pace, g.cumulative_time]));
      $("#fuel-list").innerHTML = (s.fueling_plan || []).map((f) =>
        `<li><span class="at">${esc(f.at)} ${u}</span><span class="eta">${esc(f.time || "")}</span>
         <span>${esc(f.action)}</span></li>`).join("") ||
        `<div class="empty-state">Add hydration_points in config/race_info.yaml.</div>`;
    }
  }

  // ------------------------------------------------------------ race info
  function renderRaceInfo() {
    const info = D.race?.race_info || {};
    const race = D.race?.race || D.plan?.race || {};
    const val = (v) => (v == null || v === "" || (Array.isArray(v) && !v.length))
      ? `<span class="unset">Not yet configured</span>` : esc(v);
    $("#race-kv").innerHTML = [
      ["Race", val(race.name)],
      ["Distance", val(race.distance_type ? (race.distance_type === "full" ? "Marathon" : "Half marathon") : "")],
      ["Date", val(race.race_date)],
      ["Goal time", val(race.target_time)],
      ["Location", val(info.location)],
      ["Expected temp", val(info.expected_temperature)],
      ["Humidity", val(info.humidity)],
      ["Hydration points", info.hydration_points?.length
        ? esc(info.hydration_points.map((h) => `${h.mile_or_km} (${h.type})`).join(", "))
        : `<span class="unset">Not yet configured</span>`],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    $("#course-notes").innerHTML = info.course_notes
      ? esc(info.course_notes) : `<span class="unset">Not yet configured</span>`;
  }

  // ------------------------------------------------------------ revision log
  function renderLog() {
    const rl = (D.revlog || []).slice().reverse();
    $("#revision-timeline").innerHTML = rl.map((r) => `
      <li class="src-${esc(r.source)}">
        <div class="when">${esc(new Date(r.date).toLocaleString())} · ${esc(r.source)}</div>
        <div class="what">${esc(r.note)}</div>
      </li>`).join("") || `<div class="empty-state">No revisions yet — run the pipeline.</div>`;
  }

  init();
})();
