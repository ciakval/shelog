/* shelog public page: vanilla JS + vendored uPlot. Data comes from the read-only JSON API. */
"use strict";

const TZ = "Europe/Prague";
const PALETTE = ["#d1495b", "#e8a33d", "#3f9c6d", "#2e86ab", "#8c5ea8", "#c06c2b", "#5a8f29", "#b5487f"];
const OUTDOOR_VARS = [
  "temperature_2m", "soil_temperature_0cm", "soil_temperature_6cm", "relative_humidity_2m",
  "precipitation", "soil_moisture_0_to_1cm", "soil_moisture_3_to_9cm",
];
// Measured values: the main charts.
const INDOOR = [
  { field: "t", title: "Temperature", unit: "°C", digits: 1, outdoor: true },
  { field: "rh", title: "Relative humidity", unit: "%", digits: 1, band: true },
];
// Calculated from T and RH: only fetched and drawn while their section is open.
const DERIVED = [
  { field: "emc", title: "Wood equilibrium moisture content (EMC)", unit: "%", digits: 1 },
  { field: "dew_point", title: "Dew point", unit: "°C", digits: 1 },
  { field: "abs_humidity", title: "Absolute humidity", unit: "g/m³", digits: 2 },
];
const small = () => window.innerWidth < 600;

const state = {
  range: 604800, // seconds back from now, "all", or {from, to}
  history: [],
  devices: [],
  selected: new Set(),
  charts: [],
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids) node.append(kid);
  return node;
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_) { return null; }
  return null;
}

const fmtTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: TZ, day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});
function ago(ts) {
  if (ts == null) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const fmtHM = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const fmtDay = new Intl.DateTimeFormat("en-GB", { timeZone: TZ, day: "numeric", month: "short" });
// X axis in Prague time, 24 h: a date at local midnight (and on wide spans), else HH:MM.
function axisTimes(u, splits, axisIdx, space, incr) {
  return splits.map((ts) => {
    if (ts == null) return "";
    const d = new Date(ts * 1e3);
    const hm = fmtHM.format(d);
    return incr >= 86400 || hm === "00:00" ? fmtDay.format(d) : hm;
  });
}
const num = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/* ---------- range & params ---------- */

function window_() {
  const now = Math.floor(Date.now() / 1000);
  if (state.range === "all") return { from: "all", to: now };
  if (typeof state.range === "object") return state.range;
  return { from: now - state.range, to: now };
}

function resampleFor(span) {
  const pick = $("#resample").value;
  if (pick !== "auto") return pick;
  if (span <= 8 * 86400) return "none";
  if (span <= 60 * 86400) return "15m";
  return "1h";
}

function params(extra = {}) {
  const w = window_();
  const p = new URLSearchParams({ from: w.from, to: w.to, ...extra });
  if (state.selected.size !== state.devices.length) p.set("devices", [...state.selected].join(","));
  return p;
}

/* ---------- charts ---------- */

function stepped() {
  return uPlot.paths.stepped({ align: 1 });
}

function makeChart(container, { title, unit, digits, x, series, from, to, band, extraScales, axes2, minor, height }) {
  const box = el("div", { class: minor ? "chart minor" : "chart" }, el("h3", {}, title));
  container.append(box);
  if (!x.length || series.every((s) => s.values.every((v) => v == null))) {
    box.append(el("div", { class: "empty" }, "No data in this range."));
    return null;
  }
  const ink = css("--muted");
  const grid = css("--line");
  const opts = {
    width: Math.max(280, box.clientWidth - 8),
    height: height || (small() ? 200 : 240),
    tzDate: (ts) => uPlot.tzDate(new Date(ts * 1e3), TZ),
    cursor: { sync: { key: "shelog" }, drag: { x: true, y: false, setScale: false } },
    select: { show: true },
    scales: { x: { time: true, min: from, max: to }, ...(extraScales || {}) },
    legend: { live: true },
    series: [
      { value: (u, v) => (v == null ? "–" : fmtTime.format(new Date(v * 1e3))) },
      ...series.map((s) => ({
        label: s.label,
        stroke: s.color,
        fill: s.fill,
        width: s.width || (minor ? 1.2 : 1.6),
        dash: s.dash,
        scale: s.scale || "y",
        spanGaps: false,
        paths: s.paths || stepped(),
        points: { show: false },
        value: (u, v) => (v == null ? "–" : `${v.toFixed(s.digits ?? digits)} ${s.unit || unit}`),
      })),
    ],
    axes: [
      { stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, values: axisTimes, size: 44 },
      {
        stroke: ink, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, scale: "y",
        values: (u, vals) => vals.map((v) => `${v}${unit === "%" ? "%" : ""}`), size: 52,
      },
      ...(axes2 || []).map((a) => ({ stroke: ink, grid: { show: false }, side: 1, size: 56, ...a })),
    ],
    hooks: {
      drawAxes: band ? [(u) => drawBand(u)] : [],
      setSelect: [(u) => {
        if (u.select.width > 8) {
          const a = Math.round(u.posToVal(u.select.left, "x"));
          const b = Math.round(u.posToVal(u.select.left + u.select.width, "x"));
          zoomTo(a, b);
        }
      }],
    },
  };
  const data = [x, ...series.map((s) => s.values)];
  const chart = new uPlot(opts, data, box);
  box.addEventListener("dblclick", zoomBack);
  return chart;
}

function drawBand(u) {
  const lo = Number($("#band_lo").value);
  const hi = Number($("#band_hi").value);
  if (!(hi > lo)) return;
  const top = u.valToPos(hi, "y", true);
  const bottom = u.valToPos(lo, "y", true);
  const { ctx } = u;
  ctx.save();
  ctx.fillStyle = css("--band");
  ctx.fillRect(u.bbox.left, top, u.bbox.width, bottom - top);
  ctx.restore();
}

function deviceSeries(body, field, meta) {
  return body.devices.map((name, i) => {
    const dev = state.devices.find((d) => d.name === name);
    return {
      label: name,
      color: dev.color,
      dash: dev.group === "reference" ? [6, 4] : undefined,
      values: body.fields[field][i],
      unit: meta.unit,
    };
  }).filter((s) => s.values.some((v) => v != null));
}

// Put an hourly outdoor series onto the indoor x grid (one shared x per uPlot chart).
// Indoor values are sample-and-hold, so at inserted x they are forward-filled (a null stays
// a gap); the outdoor series is linear between its hourly points, as in the outdoor charts.
function withOutdoor(x, series, ox, ov, outSeries) {
  if (!ox || !ox.length || !ov || ov.every((v) => v == null)) return { x, series };
  const xs = [...new Set([...x, ...ox])].sort((a, b) => a - b);
  const filled = series.map((s) => {
    const out = new Array(xs.length);
    let i = -1;
    xs.forEach((xv, k) => {
      while (i + 1 < x.length && x[i + 1] <= xv) i++;
      out[k] = i < 0 ? null : s.values[i];
    });
    return { ...s, values: out };
  });
  const outVals = new Array(xs.length);
  let j = 0;
  xs.forEach((xv, k) => {
    while (j + 1 < ox.length && ox[j + 1] <= xv) j++;
    if (ox[j] === xv) outVals[k] = ov[j];
    else if (ox[j] < xv && j + 1 < ox.length && ov[j] != null && ov[j + 1] != null) {
      outVals[k] = ov[j] + ((ov[j + 1] - ov[j]) * (xv - ox[j])) / (ox[j + 1] - ox[j]);
    } else outVals[k] = null;
  });
  return { x: xs, series: [...filled, { ...outSeries, values: outVals }] };
}

function renderIndoor(body, out) {
  const box = $("#indoor");
  box.replaceChildren();
  for (const meta of INDOOR) {
    let x = body.x;
    let ser = deviceSeries(body, meta.field, meta);
    if (meta.outdoor) {
      ({ x, series: ser } = withOutdoor(x, ser, out.x, out.vars.temperature_2m, {
        label: "Outdoor air", color: css("--muted"), width: 1.2, paths: uPlot.paths.linear(), unit: "°C",
      }));
    }
    const c = makeChart(box, { ...meta, x, series: ser, from: body.from, to: body.to, height: small() ? 240 : 320 });
    if (c) state.charts.push(c);
  }
}

function renderDerived(body, stats) {
  const box = $("#derived-charts");
  box.replaceChildren();
  if (!$("#derived").open) return;
  renderEmcStats(stats);
  for (const meta of DERIVED) {
    if (!body.fields[meta.field]) continue;
    const ser = deviceSeries(body, meta.field, meta);
    const c = makeChart(box, { ...meta, x: body.x, series: ser, from: body.from, to: body.to, minor: true, height: small() ? 140 : 170 });
    if (c) state.charts.push(c);
  }
}

function renderOutdoor(out, from, to) {
  const box = $("#outdoor");
  box.replaceChildren();
  const soil = $("#soil-charts");
  soil.replaceChildren();
  const v = out.vars;
  const linear = uPlot.paths.linear();
  const charts = [
    {
      title: "Air and soil temperature", unit: "°C", digits: 1,
      series: [
        { label: "Air 2 m", values: v.temperature_2m, color: "#d1495b", paths: linear },
        { label: "Soil 0 cm", values: v.soil_temperature_0cm, color: "#c06c2b", paths: linear, dash: [4, 3] },
        { label: "Soil 6 cm", values: v.soil_temperature_6cm, color: "#8c5ea8", paths: linear, dash: [2, 3] },
      ],
    },
    {
      title: "Air humidity", unit: "%", digits: 0,
      series: [{ label: "RH", values: v.relative_humidity_2m, color: "#2e86ab", paths: linear }],
    },
    {
      fold: true,
      title: "Precipitation and soil moisture", unit: "mm", digits: 1,
      extraScales: { sm: { auto: true } },
      axes2: [{ scale: "sm", values: (u, vals) => vals.map((x) => x.toFixed(3)) }],
      series: [
        { label: "Rain", values: v.precipitation, color: "#2e86ab", fill: "rgba(46,134,171,0.45)", paths: uPlot.paths.bars({ size: [0.9, 24] }) },
        { label: "Soil 0–1 cm", values: v.soil_moisture_0_to_1cm, color: "#c06c2b", paths: linear, scale: "sm", unit: "m³/m³", digits: 3 },
        { label: "Soil 3–9 cm", values: v.soil_moisture_3_to_9cm, color: "#8c5ea8", paths: linear, scale: "sm", unit: "m³/m³", digits: 3 },
      ],
    },
  ];
  for (const spec of charts) {
    if (spec.fold && !$("#soil").open) continue;
    const c = makeChart(spec.fold ? soil : box, { ...spec, x: out.x, from, to });
    if (c) state.charts.push(c);
  }
}

/* ---------- stats & health ---------- */

const GROUPS = [["subfloor", "Subfloor"], ["reference", "Reference rooms"]];

// One table body with a header row per sensor group; `cells(r)` gives the value cells.
function groupedTable(table, heads, rows, cells) {
  const head = el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { scope: "col" }, h))));
  const body = el("tbody");
  const known = new Set(GROUPS.map(([g]) => g));
  const groups = [...GROUPS, ...[...new Set(rows.map((r) => r.group))].filter((g) => !known.has(g)).map((g) => [g, g])];
  for (const [g, label] of groups) {
    const rs = rows.filter((r) => r.group === g);
    if (!rs.length) continue;
    body.append(el("tr", { class: "grp" }, el("th", { colspan: String(heads.length), scope: "colgroup" }, label)));
    for (const r of rs) body.append(el("tr", {}, el("td", {}, r.name), ...cells(r).map((c) => el("td", {}, c))));
  }
  table.replaceChildren(head, body);
}

function renderStats(stats) {
  groupedTable($("#stats"),
    ["Sensor", "T min", "T mean", "T max", "RH min", "RH mean", "RH max",
      `RH outside ${stats.band[0]}–${stats.band[1]} %`, "Coverage"],
    stats.devices,
    (r) => {
      const rh = r.rh || {};
      return [num(r.t.min), num(r.t.mean), num(r.t.max), num(rh.min), num(rh.mean), num(rh.max),
        r.rh_outside_band_pct == null ? "–" : `${num(r.rh_outside_band_pct)} %`,
        `${num(r.coverage_pct, 0)} %`];
    });
}

function renderEmcStats(stats) {
  groupedTable($("#emc-stats"), ["Sensor", "EMC mean", "EMC range"], stats.devices, (r) => {
    const emc = r.emc || {};
    return [emc.mean == null ? "–" : `${num(emc.mean)} %`,
      emc.min == null ? "–" : `${num(emc.min)}–${num(emc.max)} %`];
  });
}

// Latest measured values of every sensor. A short window is enough: the API forward-fills
// the report held at its start and returns null once a sensor is silent past its threshold.
async function refreshNow(health) {
  const now = Math.floor(Date.now() / 1000);
  let body;
  try {
    body = await getJSON(`api/readings?${new URLSearchParams({ from: now - 60, to: now, resample: "none", fields: "t,rh,battery" })}`);
  } catch (_) {
    return;
  }
  const last = (col) => (col && col.length ? col[col.length - 1] : null);
  const hdev = new Map((health?.devices || []).map((d) => [d.name, d]));
  const box = $("#now");
  box.replaceChildren();
  for (const [g, label] of GROUPS) {
    const devs = state.devices.filter((d) => d.group === g);
    if (!devs.length) continue;
    const row = el("div", { class: "now-group" }, el("h3", {}, label));
    for (const d of devs) {
      const i = body.devices.indexOf(d.name);
      const t = i < 0 ? null : last(body.fields.t[i]);
      const rh = i < 0 ? null : last(body.fields.rh[i]);
      const bat = i < 0 ? null : last(body.fields.battery[i]);
      const h = hdev.get(d.name);
      const stale = h ? h.silent : t == null;
      const vals = el("div", { class: "vals" },
        el("span", { class: "v" }, num(t), el("small", {}, "°C")));
      if (d.has_rh) vals.append(el("span", { class: "v" }, num(rh, 0), el("small", {}, "%")));
      const meta = [h ? (stale ? `silent, last report ${ago(h.last_report)}` : ago(h.last_report)) : ""];
      if (bat != null) meta.push(`battery ${num(bat, 0)} %`);
      const card = el("div", { class: `reading${g === "reference" ? " ref" : ""}${stale ? " stale" : ""}` },
        el("div", { class: "name" }, d.name), vals, el("div", { class: "meta" }, meta.filter(Boolean).join(", ")));
      card.style.setProperty("--c", d.color);
      row.append(card);
    }
    box.append(row);
  }
}

async function refreshHealth() {
  const box = $("#health");
  let h;
  try {
    h = await getJSON("api/health");
  } catch (_) {
    box.textContent = "Status unavailable";
    refreshNow(null);
    return;
  }
  refreshNow(h);
  const colors = { ok: "--ok", degraded: "--warn", down: "--bad", starting: "--muted" };
  const label = { ok: "Logging", degraded: "Logging, with warnings", down: "Logging stopped", starting: "Starting" };
  const dot = el("span", { class: "dot" });
  dot.style.background = css(colors[h.status] || "--muted");
  const kids = [el("div", {}, dot, el("strong", {}, label[h.status] || h.status)),
    el("div", {}, `Last poll ${ago(h.last_ok_poll)}`)];
  const silent = h.devices.filter((d) => d.silent).map((d) => d.name);
  const low = h.devices.filter((d) => d.battery_low).map((d) => d.name);
  if (silent.length) kids.push(el("div", { class: "silent" }, `Silent: ${silent.join(", ")}`));
  if (low.length) kids.push(el("div", { class: "silent" }, `Battery low: ${low.join(", ")}`));
  box.replaceChildren(...kids);
  const banner = $("#banner");
  if (h.auth === "rejected") {
    banner.replaceChildren(el("div", {}, "Logging is paused: Shelly Cloud rejected the access key. The owner has been notified."));
    banner.hidden = false;
  } else if (h.status === "down" && h.last_ok_poll) {
    banner.replaceChildren(el("div", {}, `No successful poll since ${fmtTime.format(new Date(h.last_ok_poll * 1e3))}.`));
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

/* ---------- controls ---------- */

function renderDevices() {
  const box = $("#devices");
  box.replaceChildren(el("span", { class: "lbl" }, "Sensors"));
  for (const group of ["subfloor", "reference"]) {
    const devs = state.devices.filter((d) => d.group === group);
    if (!devs.length) continue;
    const toggle = el("button", {
      onclick: () => {
        const all = devs.every((d) => state.selected.has(d.name));
        devs.forEach((d) => (all ? state.selected.delete(d.name) : state.selected.add(d.name)));
        if (!state.selected.size) devs.forEach((d) => state.selected.add(d.name));
        saveSelection(); renderDevices(); load();
      },
    }, group === "subfloor" ? "Subfloor" : "Reference rooms");
    const g = el("div", { class: "group" }, toggle);
    for (const d of devs) {
      const cb = el("input", { type: "checkbox" });
      cb.checked = state.selected.has(d.name);
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(d.name);
        else if (state.selected.size > 1) state.selected.delete(d.name);
        else cb.checked = true;
        saveSelection(); load();
      });
      const sw = el("span", { class: `swatch${d.group === "reference" ? " dashed" : ""}` });
      sw.style.background = d.color;
      sw.style.borderColor = d.color;
      g.append(el("label", { class: "dev" }, cb, sw, d.name));
    }
    box.append(g);
  }
}

function saveSelection() {
  store("shelog.selected", [...state.selected]);
}

function markRange() {
  document.querySelectorAll("[data-range]").forEach((b) => {
    const r = b.dataset.range === "all" ? "all" : Number(b.dataset.range);
    b.classList.toggle("on", r === state.range);
  });
}

function setRange(r, remember = true) {
  if (remember) state.history.push(state.range);
  state.range = r;
  if (typeof r !== "object") store("shelog.range", r);
  markRange();
  load();
}

function zoomTo(a, b) {
  if (b - a < 600) b = a + 600;
  setRange({ from: a, to: Math.min(b, Math.floor(Date.now() / 1000)) });
}

function zoomBack() {
  if (state.history.length) setRange(state.history.pop(), false);
}

function toLocalInput(ts) {
  const d = new Date(ts * 1e3);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ---------- load ---------- */

let loadSeq = 0;
async function load() {
  const seq = ++loadSeq;
  const metas = $("#derived").open ? [...INDOOR, ...DERIVED] : INDOOR;
  const fields = metas.map((m) => m.field).join(",");
  try {
    // Stats resolve the actual range (also for "all"), which picks the resampling.
    const stats = await getJSON(`api/stats?${params({ band_lo: $("#band_lo").value, band_hi: $("#band_hi").value })}`);
    const resample = resampleFor(stats.to - stats.from);
    const [body, out] = await Promise.all([
      getJSON(`api/readings?${params({ fields, resample })}`),
      getJSON(`api/outdoor?${params({ vars: OUTDOOR_VARS.join(",") })}`).catch(() => ({ x: [], vars: {} })),
    ]);
    if (seq !== loadSeq) return;
    state.charts.forEach((c) => c.destroy());
    state.charts = [];
    renderIndoor(body, out);
    renderStats(stats);
    renderDerived(body, stats);
    renderOutdoor(out, body.from, body.to);
    $("#range-label").textContent =
      `${fmtTime.format(new Date(body.from * 1e3))} – ${fmtTime.format(new Date(body.to * 1e3))}`;
    $("#from").value = toLocalInput(body.from);
    $("#to").value = toLocalInput(body.to);
    $("#csv").href = `api/export.csv?${params({ fields, resample })}`;
  } catch (err) {
    if (seq !== loadSeq) return;
    $("#indoor").replaceChildren(el("div", { class: "chart" }, el("div", { class: "empty" }, `Could not load data (${err.message}).`)));
  }
}

async function init() {
  document.querySelectorAll("[data-range]").forEach((b) =>
    b.addEventListener("click", () => setRange(b.dataset.range === "all" ? "all" : Number(b.dataset.range))));
  $("#apply").addEventListener("click", () => {
    const a = Math.floor(new Date($("#from").value).getTime() / 1000);
    const b = Math.floor(new Date($("#to").value).getTime() / 1000);
    if (Number.isFinite(a) && Number.isFinite(b) && b > a) setRange({ from: a, to: b });
  });
  for (const id of ["#resample", "#band_lo", "#band_hi"]) $(id).addEventListener("change", load);
  // Folded sections remember their state; opening one loads and draws its contents.
  let ready = false;
  for (const id of ["derived", "soil"]) {
    const d = document.getElementById(id);
    d.open = store(`shelog.open.${id}`) === true;
    d.addEventListener("toggle", () => {
      store(`shelog.open.${id}`, d.open);
      if (ready) load();
    });
  }
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.charts.forEach((c) => {
      const box = c.root.parentElement;
      c.setSize({ width: Math.max(280, box.clientWidth - 8), height: c.height });
    }), 150);
  });

  const saved = store("shelog.range");
  if (saved === "all" || Number.isFinite(saved)) state.range = saved;
  markRange();
  const { devices } = await getJSON("api/devices");
  state.devices = devices.map((d, i) => ({ ...d, color: PALETTE[i % PALETTE.length] }));
  const sel = store("shelog.selected");
  const names = new Set(state.devices.map((d) => d.name));
  const keep = Array.isArray(sel) ? sel.filter((n) => names.has(n)) : [];
  state.selected = new Set(keep.length ? keep : names);
  renderDevices();
  refreshHealth();
  setInterval(refreshHealth, 60000);
  setInterval(() => { if (typeof state.range !== "object") load(); }, 5 * 60000);
  ready = true;
  load();
}

init();
