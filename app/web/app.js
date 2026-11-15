/* Search reformulation analytics — vanilla JS, no build step, no dependencies.
 * All dynamic labels come from the API and are inserted with textContent /
 * createElement (untrusted-data rule), never innerHTML concatenation.
 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const tip = $("#tooltip");
const toastEl = $("#toast");
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.style.display = "block";
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => (toastEl.style.display = "none"), 2600);
}

const state = {
  filters: {},
  editingVersion: null,
  versions: [],
  pathOffset: 0,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function qs(params) {
  const merged = { ...state.filters, ...params };
  const usp = new URLSearchParams();
  Object.entries(merged).forEach(([k, v]) => {
    if (v !== "" && v != null) usp.set(k, v);
  });
  return usp.toString();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  });
  children.flat().forEach((c) => {
    if (c == null) return;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
}

const pct = (v) => (v == null ? "—" : `${v}%`);
const intFmt = (n) => (n == null ? "—" : Number(n).toLocaleString());

/* =============================================================== theme */
$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark";
  root.dataset.theme = dark ? "light" : "dark";
  $("#theme-toggle").textContent = dark ? "🌙 暗色" : "☀️ 亮色";
  renderAll();
});

/* =============================================================== tabs */
$$("nav.tabs button").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$("nav.tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".view").forEach((v) =>
      v.classList.toggle("active", v.id === `view-${btn.dataset.view}`));
    renderAll();
  })
);

/* =============================================================== filters */
async function loadFilterOptions() {
  const f = await api("/api/filters");
  state.versions = f.mapping_versions;
  fillSelect("#f-ab", f.ab_versions.map((v) => [v, `版本 ${v}`]));
  fillSelect("#f-wc", f.word_classes.map((v) => [v, v]));
  fillSelect(
    "#f-mv",
    f.mapping_versions.map((v) => [v.version_id, `${v.label}${v.is_active ? "（激活）" : ""}`]),
    true
  );
}
function fillSelect(sel, pairs, keepPlaceholder = false) {
  const node = $(sel);
  const ph = node.querySelector("option");
  const placeholder = ph ? ph.cloneNode(true) : null;
  node.textContent = "";
  if (placeholder) node.appendChild(placeholder);
  pairs.forEach(([value, label]) =>
    node.appendChild(el("option", { value, text: label })));
  // keep the explicit "all / current" placeholder selected after rebuilding
  node.value = "";
}

function readFilters() {
  state.filters = {
    date_from: $("#f-date-from").value,
    date_to: $("#f-date-to").value,
    ab_version: $("#f-ab").value,
    word_class: $("#f-wc").value,
    mapping_version_id: $("#f-mv").value,
  };
}
$("#f-apply").addEventListener("click", () => { readFilters(); renderAll(); });
$("#f-reset").addEventListener("click", () => {
  $$("#global-filters input, #global-filters select").forEach((n) => (n.value = ""));
  readFilters();
  renderAll();
});

/* =============================================================== KPI row */
function renderKpis(ov) {
  const defs = [
    { label: "查询路径数", value: intFmt(ov.paths), metric: null },
    { label: "搜索节点总数", value: intFmt(ov.search_nodes), metric: null },
    { label: "零结果节点", value: intFmt(ov.zero_result_nodes),
      hint: `零结果率 ${ov.zero_result_rate_pct}%`, metric: "zero", warn: true },
    { label: "零结果改写恢复率", value: pct(ov.recovery_rate_pct),
      hint: `分母 ${ov.observed_zero_nodes} 个完整观察节点`, metric: "recovery" },
    { label: "改写后点击率", value: pct(ov.click_rate_pct),
      hint: "固定窗口内发生点击", metric: "click" },
    { label: "加购率", value: pct(ov.cart_rate_pct), metric: "cart" },
    { label: "转化率", value: pct(ov.conversion_rate_pct),
      hint: "窗口内下单（含跨标签页）", metric: "conversion" },
    { label: "回退查询节点", value: intFmt(ov.fallback_nodes),
      hint: "零结果后放宽词且结果数达阈值", metric: "fallback" },
    { label: "未完整观察零结果", value: intFmt(ov.incomplete_zero_nodes),
      hint: "会话早于观察窗结束，不计入率", metric: "incomplete", warn: true },
  ];
  const grid = $("#kpi-row");
  grid.textContent = "";
  defs.forEach((d) => {
    const tile = el("div", { class: `tile ${d.warn ? "warn" : ""} ${d.metric ? "clickable" : ""}` },
      el("div", { class: "label", text: d.label }),
      el("div", { class: "value", text: d.value }),
      d.hint ? el("div", { class: "hint", text: d.hint }) : null);
    if (d.metric) tile.addEventListener("click", () => openDrill(d.metric, d.label));
    grid.appendChild(tile);
  });
}

/* =============================================================== SVG charts */
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function svgEl(tag, attrs = {}) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  return n;
}
function showTip(evt, rows, title) {
  tip.textContent = "";
  if (title) tip.appendChild(el("div", { class: "tl", text: title }));
  rows.forEach(([color, label, value]) => {
    tip.appendChild(el("div", { class: "row" },
      el("span", { class: "swatch", style: `display:inline-block;width:9px;height:9px;border-radius:2px;background:${color}` }),
      el("span", { class: "tl", text: label }),
      el("span", { class: "tv", text: value })));
  });
  tip.style.display = "block";
  moveTip(evt);
}
function moveTip(evt) {
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY + 14;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
function hideTip() { tip.style.display = "none"; }

function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / mag;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return step * mag;
}

/* Bar + line combo, two separate y-scales drawn as two charts stacked visually
 * is avoided: we keep ONE axis for bars and show the rate line against its own
 * labeled scale in the legend instead of a dual axis (dataviz rule). */
function renderVolumeChart(containerId, data) {
  const host = $(`#${containerId}`);
  host.textContent = "";
  const W = 620, H = 260, pad = { l: 46, r: 14, t: 16, b: 42 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const maxV = niceMax(Math.max(1, ...data.map((d) => d.searches)));
  const bw = (W - pad.l - pad.r) / Math.max(1, data.length);
  const y = (v) => H - pad.b - (v / maxV) * (H - pad.t - pad.b);
  for (let i = 0; i <= 4; i++) {
    const val = (maxV / 4) * i, yy = y(val);
    svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, class: "grid" }));
    const t = svgEl("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" });
    t.textContent = intFmt(Math.round(val));
    svg.appendChild(t);
  }
  const blue = cssVar("--series-1"), red = cssVar("--zero-fill");
  data.forEach((d, i) => {
    const x0 = pad.l + i * bw;
    const barW = Math.min(24, bw * 0.62);
    const bx = x0 + (bw - barW) / 2;
    const totalH = H - pad.b - y(d.searches);
    const zeroH = d.searches ? (d.zero_nodes / d.searches) * totalH : 0;
    const hit = svgEl("rect", { x: x0, y: pad.t, width: bw, height: H - pad.t - pad.b,
      fill: "transparent" });
    const okH = totalH - zeroH;
    svg.appendChild(svgEl("rect", { x: bx, y: y(d.searches), width: barW, height: okH,
      rx: 0, fill: blue }));
    if (zeroH > 0)
      svg.appendChild(svgEl("rect", { x: bx, y: H - pad.b - zeroH, width: barW, height: zeroH,
        rx: 4, fill: red }));
    const label = svgEl("text", { x: x0 + bw / 2, y: H - pad.b + 16, "text-anchor": "middle" });
    label.textContent = d.day.slice(5);
    svg.appendChild(label);
    hit.addEventListener("mousemove", (e) => {
      showTip(e, [
        [blue, "搜索次数", intFmt(d.searches)],
        [red, "零结果", intFmt(d.zero_nodes)],
        [cssVar("--text-muted"), "零结果率", `${d.zero_rate_pct}%`],
      ], d.day);
    });
    hit.addEventListener("mouseleave", hideTip);
    svg.appendChild(hit);
  });
  svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b, class: "axis" }));
  host.appendChild(svg);
  $("#legend-volume").textContent = "";
  [[blue, "有结果搜索"], [red, "零结果搜索"]].forEach(([c, l]) =>
    $("#legend-volume").appendChild(el("span", { class: "key" },
      el("span", { class: "swatch", style: `background:${c}` }), l)));
}

function renderRateLineChart(containerId, legendId, data, series) {
  const host = $(`#${containerId}`);
  host.textContent = "";
  const W = 620, H = 260, pad = { l: 46, r: 14, t: 16, b: 42 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const y = (v) => H - pad.b - (v / 100) * (H - pad.t - pad.b);
  for (let i = 0; i <= 5; i++) {
    const val = i * 20, yy = y(val);
    svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, class: "grid" }));
    const t = svgEl("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" });
    t.textContent = `${val}%`;
    svg.appendChild(t);
  }
  const colors = [cssVar("--series-1"), cssVar("--series-3"), cssVar("--series-2")];
  const xStep = (W - pad.l - pad.r) / Math.max(1, data.length - 1);
  series.forEach((s, si) => {
    const pts = data.map((d, i) => ({
      x: pad.l + (data.length === 1 ? (W - pad.l - pad.r) / 2 : i * xStep),
      y: y(d[s.key] ?? 0), d, i,
    })).filter((p) => p.d[s.key] != null);
    if (pts.length < 2) return;
    const path = pts.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ");
    svg.appendChild(svgEl("path", { d: path, fill: "none", stroke: colors[si],
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    pts.forEach((p) => {
      svg.appendChild(svgEl("circle", { cx: p.x, cy: p.y, r: 4, fill: colors[si],
        stroke: cssVar("--surface"), "stroke-width": 2 }));
    });
  });
  data.forEach((d, i) => {
    const x = pad.l + (data.length === 1 ? (W - pad.l - pad.r) / 2 : i * xStep);
    const label = svgEl("text", { x, y: H - pad.b + 16, "text-anchor": "middle" });
    label.textContent = d.day.slice(5);
    svg.appendChild(label);
    const hit = svgEl("rect", { x: x - xStep / 2, y: pad.t, width: xStep,
      height: H - pad.t - pad.b, fill: "transparent" });
    hit.addEventListener("mousemove", (e) => {
      showTip(e, series.map((s, si) => [colors[si], s.label, pct(d[s.key])]), d.day);
    });
    hit.addEventListener("mouseleave", hideTip);
    svg.appendChild(hit);
  });
  svg.appendChild(svgEl("line", { x1: pad.l, x2: W - pad.r, y1: H - pad.b, y2: H - pad.b, class: "axis" }));
  host.appendChild(svg);
  const legend = $(`#${legendId}`);
  legend.textContent = "";
  series.forEach((s, si) =>
    legend.appendChild(el("span", { class: "key" },
      el("span", { class: "linekey", style: `border-color:${colors[si]}` }), s.label)));
}

/* Horizontal grouped comparison bars (one rate per group, one axis 0-100%) */
function renderCompareBars(containerId, rows, rateKey) {
  const host = $(`#${containerId}`);
  host.textContent = "";
  if (!rows.length) { host.appendChild(el("div", { class: "empty", text: "暂无数据" })); return; }
  const rowH = 34, W = 620, H = rows.length * rowH + 20, padL = 130, padR = 60;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const color = cssVar("--series-1");
  rows.forEach((r, i) => {
    const yy = 10 + i * rowH;
    const name = svgEl("text", { x: padL - 10, y: yy + 15, "text-anchor": "end" });
    name.textContent = r.bucket;
    svg.appendChild(name);
    const val = r[rateKey] ?? 0;
    const bw = ((W - padL - padR) * val) / 100;
    svg.appendChild(svgEl("rect", { x: padL, y: yy + 4, width: Math.max(2, bw), height: 18,
      rx: 4, fill: color }));
    const vt = svgEl("text", { x: padL + Math.max(2, bw) + 8, y: yy + 17 });
    vt.textContent = r[rateKey] == null ? "无完整观察样本" : `${val}%`;
    svg.appendChild(vt);
    const hit = svgEl("rect", { x: 0, y: yy, width: W, height: rowH, fill: "transparent" });
    hit.addEventListener("mousemove", (e) => showTip(e, [
      [color, rateKey, pct(r[rateKey])],
      [cssVar("--text-muted"), "零结果节点", intFmt(r.zero_nodes)],
      [cssVar("--text-muted"), "完整观察", intFmt(r.observed_zero_nodes)],
    ], String(r.bucket)));
    hit.addEventListener("mouseleave", hideTip);
    svg.appendChild(hit);
  });
  host.appendChild(svg);
}

/* =============================================================== tables */
function makeTable(containerSel, columns, rows, onRowClick) {
  const host = $(containerSel);
  host.textContent = "";
  if (!rows.length) { host.appendChild(el("div", { class: "empty", text: "暂无数据" })); return; }
  const thead = el("thead", el("tr", ...columns.map((c) =>
    el("th", { class: c.numeric ? "num" : "", text: c.label }))));
  const tbody = el("tbody");
  rows.forEach((row) => {
    const tr = el("tr", onRowClick ? { class: "clickable", onClick: () => onRowClick(row) } : {});
    columns.forEach((c) => {
      const td = el("td", { class: c.numeric ? "num" : "" });
      const v = c.render ? c.render(row) : row[c.key];
      if (v instanceof Node) td.appendChild(v);
      else td.textContent = v ?? "—";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  host.append(thead, tbody);
}

function renderTopZero(rows) {
  makeTable("#table-top-zero", [
    { label: "规范查询词", render: (r) => el("div", {},
        el("div", { text: r.query, style: "font-weight:600" }),
        el("div", { class: "muted", style: "font-size:11.5px", text: `原词示例: ${r.query_raw}` })) },
    { label: "词类", key: "word_class" },
    { label: "版本", render: (r) => el("span", { class: `badge ${r.ab_version.toLowerCase()}` , text: r.ab_version }) },
    { label: "零结果次数", key: "zero_nodes", numeric: true },
    { label: "完整观察", key: "observed", numeric: true },
    { label: "恢复", key: "recovered", numeric: true },
    { label: "点击", key: "clicks", numeric: true },
    { label: "转化", key: "orders", numeric: true },
  ], rows, (r) => openDrill("zero", `零结果: ${r.query}`));
}

const breakdownCols = [
  { label: "分组", key: "bucket" },
  { label: "搜索总数", key: "total_nodes", numeric: true },
  { label: "零结果", key: "zero_nodes", numeric: true },
  { label: "零结果率", render: (r) => `${r.zero_rate_pct}%`, numeric: true },
  { label: "完整观察零结果", key: "observed_zero_nodes", numeric: true },
  { label: "未完整观察", key: "incomplete_zero_nodes", numeric: true },
  { label: "恢复率", render: (r) => pct(r.recovery_rate_pct), numeric: true },
  { label: "点击率", render: (r) => pct(r.click_rate_pct), numeric: true },
  { label: "加购率", render: (r) => pct(r.cart_rate_pct), numeric: true },
  { label: "转化率", render: (r) => pct(r.conversion_rate_pct), numeric: true },
  { label: "回退数", key: "fallback_nodes", numeric: true },
];

/* =============================================================== paths */
function nodeFlags(n) {
  const flags = [];
  if (n.is_fallback) flags.push(el("span", { class: "flag fallback", text: "回退" }));
  if (!n.window_observed)
    flags.push(el("span", { class: "flag incomplete",
      text: `未完整观察${n.incomplete_reason === "truncated_window" ? "·窗口截断" : "·会话早退"}` }));
  if (n.has_click) flags.push(el("span", { class: "flag click", text: "点击" }));
  if (n.has_add_to_cart) flags.push(el("span", { class: "flag click", text: "加购" }));
  if (n.has_conversion) flags.push(el("span", { class: "flag order", text: "转化" }));
  return el("div", { class: "flags" }, ...flags);
}

function renderPaths(payload) {
  const host = $("#path-list");
  host.textContent = "";
  host.appendChild(el("div", { class: "muted", style: "margin-bottom:10px",
    text: `共 ${payload.total} 条路径，展示前 ${payload.paths.length} 条` }));
  if (!payload.paths.length) { host.appendChild(el("div", { class: "empty", text: "暂无路径" })); return; }
  payload.paths.forEach(({ path, nodes }) => {
    const flow = el("div", { class: "path-flow" });
    nodes.forEach((n, i) => {
      const node = el("div", { class: `node ${n.is_zero_result ? "zero" : ""}` },
        el("div", { class: "q", text: n.canonical_query }),
        n.query_raw !== n.canonical_query
          ? el("div", { class: "qraw", text: `原词: ${n.query_raw}` }) : null,
        el("div", { class: "meta" },
          el("span", { class: `badge ${n.is_zero_result ? "zero" : "ok"}`,
            text: n.is_zero_result ? `零结果` : `${n.result_count} 条结果` }),
          el("span", { class: `badge ${n.ab_version.toLowerCase()}`, text: n.ab_version }),
          el("span", { class: "badge muted", text: n.word_class })),
        nodeFlags(n));
      flow.appendChild(node);
      if (i < nodes.length - 1) flow.appendChild(el("div", { class: "arrow", text: "→" }));
    });
    const card = el("div", { class: "card", style: "margin-top:10px" },
      el("div", { class: "form-row", style: "margin:0 0 6px" },
        el("strong", { class: "mono", text: path.session_id }),
        el("span", { class: "badge muted", text: `标签页 ${path.tab_id}` }),
        el("span", { class: `badge ${path.ab_version.toLowerCase()}`, text: `版本 ${path.ab_version}` }),
        el("span", { class: "badge muted", text: `${path.node_count} 个节点` }),
        path.session_observed ? el("span", { class: "badge ok", text: "会话完整观察" })
                              : el("span", { class: "badge zero", text: "会话未完整观察" }),
        el("span", { class: "spacer" }),
        el("button", { class: "btn small", text: "查看会话",
          onClick: () => openSession(path.session_id) })),
      flow);
    host.appendChild(card);
  });
}

/* =============================================================== modal */
function openModal(title, sub) {
  $("#modal-title").textContent = title;
  $("#modal-sub").textContent = sub || "";
  $("#modal-backdrop").classList.add("open");
}
$("#modal-close").addEventListener("click", () =>
  $("#modal-backdrop").classList.remove("open"));
$("#modal-backdrop").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.remove("open");
});

async function openSession(sessionId) {
  openModal(`会话 ${sessionId}`, "事件时间排序 · 原始词保留 · 含跨标签页事件");
  const body = $("#modal-body");
  body.textContent = "";
  const detail = await api(`/api/session?session_id=${encodeURIComponent(sessionId)}`);
  detail.paths.forEach(({ path, nodes }) => {
    const flow = el("div", { class: "path-flow", style: "margin:10px 0" });
    nodes.forEach((n, i) => {
      flow.appendChild(el("div", { class: `node ${n.is_zero_result ? "zero" : ""}` },
        el("div", { class: "q", text: n.canonical_query }),
        el("div", { class: "qraw", text: n.query_raw }),
        el("div", { class: "meta" },
          el("span", { text: n.is_zero_result ? "零结果" : `${n.result_count} 结果` })),
        nodeFlags(n)));
      if (i < nodes.length - 1) flow.appendChild(el("div", { class: "arrow", text: "→" }));
    });
    body.appendChild(el("div", { class: "card" },
      el("div", { class: "muted mono", text:
        `路径 #${path.path_id} · 标签页 ${path.tab_id} · ${path.start_time} → ${path.end_time}` }),
      flow));
  });
  const tl = el("div", { class: "timeline" });
  detail.raw_events.forEach((e) => {
    const isZero = e.event_type === "search" && e.result_count === 0;
    const summary = e.event_type === "search"
      ? `搜索 “${e.query_raw}” → ${e.result_count} 结果`
      : `${e.event_type} ${e.product_id || ""} ${e.amount ? "¥" + e.amount : ""}`;
    tl.appendChild(el("div", { class: `te ${isZero ? "z" : ""}` },
      el("span", { class: "t", text: e.event_time.slice(11, 19) }),
      el("span", { class: "badge muted", text: e.tab_id }), " ",
      document.createTextNode(summary),
      e.source !== "web" ? el("span", { class: "badge muted", text: e.source }) : null));
  });
  body.append(el("h4", { text: "完整事件时间线（原始埋点）" }), tl);
}

async function openDrill(metric, label) {
  openModal(`指标下钻: ${label}`, "每个指标可查到对应的会话明细");
  const body = $("#modal-body");
  body.textContent = "";
  const rows = await api(`/api/drill?${qs({ metric })}`);
  if (!rows.length) { body.appendChild(el("div", { class: "empty", text: "无匹配会话" })); return; }
  const table = el("table", {},
    el("thead", el("tr",
      el("th", { text: "会话 ID" }), el("th", { class: "num", text: "匹配节点数" }),
      el("th", { text: "首次出现" }), el("th", { text: "最近出现" }),
      el("th", { text: "" }))),
    el("tbody", ...rows.map((r) => el("tr", { class: "clickable",
        onClick: () => openSession(r.session_id) },
      el("td", { class: "mono", text: r.session_id }),
      el("td", { class: "num", text: r.matching_nodes }),
      el("td", { text: r.first_seen }), el("td", { text: r.last_seen }),
      el("td", { text: "打开 →" })))));
  body.appendChild(el("div", { class: "table-wrap" }, table));
}

/* =============================================================== mapping */
async function renderMapping() {
  const versions = await api("/api/mapping/versions");
  state.versions = versions;
  makeTable("#table-mv", [
    { label: "版本", key: "version_id" },
    { label: "名称", key: "label" },
    { label: "说明", key: "note" },
    { label: "条目数", key: "entry_count", numeric: true },
    { label: "状态", render: (r) => r.is_active
        ? el("span", { class: "badge ok", text: "激活中" })
        : el("span", { class: "badge muted", text: "未激活" }) },
    { label: "操作", render: (r) => el("div", { style: "display:flex;gap:6px" },
        el("button", { class: "btn small", text: "编辑", onClick: () => selectVersion(r.version_id) }),
        r.is_active ? null : el("button", { class: "btn small primary", text: "激活并重建",
          onClick: () => activateVersion(r.version_id) })) },
  ], versions);
  if (state.editingVersion == null && versions.length)
    state.editingVersion = versions.find((v) => !v.is_active)?.version_id ?? versions[0].version_id;
  await renderEntries();
  await renderWordClassRules();
}

async function selectVersion(vid) {
  state.editingVersion = vid;
  await renderEntries();
}

async function renderEntries() {
  const vid = state.editingVersion;
  if (vid == null) return;
  const v = state.versions.find((x) => x.version_id === vid);
  $("#mv-editing").textContent = v ? v.label : `#${vid}`;
  const rows = await api(`/api/mapping/entries?version_id=${vid}`);
  makeTable("#table-me", [
    { label: "ID", key: "mapping_id", numeric: true },
    { label: "原词（规范化）", key: "source_term" },
    { label: "规范词", key: "target_term" },
    { label: "操作", render: (r) => el("button", { class: "btn small danger",
      text: "删除", onClick: () => deleteEntry(r.mapping_id) }) },
  ], rows);
}

$("#mv-clone").addEventListener("click", async () => {
  const label = $("#mv-label").value.trim();
  if (!label) return showToast("请填写新版本名称");
  const r = await api("/api/mapping/clone", { method: "POST",
    body: { label, note: $("#mv-note").value.trim() } });
  state.editingVersion = r.version_id;
  $("#mv-label").value = ""; $("#mv-note").value = "";
  showToast(`已克隆为新版本 #${r.version_id}，编辑后激活即可生效`);
  await renderMapping();
});

$("#me-save").addEventListener("click", async () => {
  const source = $("#me-source").value.trim(), target = $("#me-target").value.trim();
  if (!source || !target) return showToast("原词与规范词均不能为空");
  const v = state.versions.find((x) => x.version_id === state.editingVersion);
  if (v?.is_active) return showToast("激活版本不可直接修改，请先克隆出新版本");
  await api("/api/mapping/entry", { method: "POST",
    body: { version_id: state.editingVersion, source_term: source, target_term: target } });
  $("#me-source").value = ""; $("#me-target").value = "";
  showToast("条目已保存");
  await renderMapping();
});

async function deleteEntry(mappingId) {
  const v = state.versions.find((x) => x.version_id === state.editingVersion);
  if (v?.is_active) return showToast("激活版本不可直接修改");
  await api("/api/mapping/entry/delete", { method: "POST",
    body: { version_id: state.editingVersion, mapping_id: mappingId } });
  showToast("条目已删除");
  await renderMapping();
}

async function activateVersion(vid) {
  const r = await api("/api/mapping/activate", { method: "POST", body: { version_id: vid } });
  showToast(`版本 #${vid} 已激活，派生数据已重建`);
  renderDiff(r.before, r.after);
  await loadFilterOptions();
  await renderMapping();
  await renderOverview();
}

function renderDiff(before, after) {
  $("#mv-diff-card").style.display = "";
  const keys = [
    ["zero_result_rate_pct", "零结果率 %"], ["observed_zero_nodes", "完整观察零结果"],
    ["recovery_rate_pct", "恢复率 %"], ["click_rate_pct", "点击率 %"],
    ["cart_rate_pct", "加购率 %"], ["conversion_rate_pct", "转化率 %"],
    ["fallback_nodes", "回退节点"],
  ];
  const rows = keys.map(([k, label]) => {
    const b = before[k], a = after[k];
    const delta = typeof b === "number" && typeof a === "number" ? Math.round((a - b) * 100) / 100 : null;
    return { label, before: b, after: a, delta };
  });
  makeTable("#table-mv-diff", [
    { label: "指标", key: "label" },
    { label: "激活前", key: "before", numeric: true },
    { label: "激活后", key: "after", numeric: true },
    { label: "变化", render: (r) => r.delta == null ? "—" :
        el("span", { class: r.delta > 0 ? "delta-up" : r.delta < 0 ? "delta-down" : "",
          text: `${r.delta > 0 ? "+" : ""}${r.delta}` }), numeric: true },
  ], rows);
}

async function renderWordClassRules() {
  const rows = await api("/api/word_class_rules");
  makeTable("#table-wc-rules", [
    { label: "ID", key: "rule_id", numeric: true },
    { label: "关键词", key: "keyword" },
    { label: "词类", key: "word_class" },
    { label: "顺序", key: "rule_order", numeric: true },
    { label: "操作", render: (r) => el("button", { class: "btn small danger",
      text: "删除", onClick: () => deleteRule(r.rule_id) }) },
  ], rows);
}
$("#wc-save").addEventListener("click", async () => {
  const kw = $("#wc-keyword").value.trim(), wc = $("#wc-class").value.trim();
  if (!kw || !wc) return showToast("关键词与词类均不能为空");
  await api("/api/word_class_rule", { method: "POST", body: {
    keyword: kw, word_class: wc, rule_order: Number($("#wc-order").value || 100) } });
  $("#wc-keyword").value = ""; $("#wc-class").value = "";
  showToast("词类规则已保存，派生数据已重建");
  await renderWordClassRules();
  await renderAll();
});
async function deleteRule(id) {
  await api("/api/word_class_rule/delete", { method: "POST", body: { rule_id: id } });
  showToast("规则已删除，派生数据已重建");
  await renderWordClassRules();
  await renderAll();
}

/* =============================================================== quality */
async function renderQuality() {
  const q = await api("/api/quality");
  const total = q.accepted_events + q.excluded_events;
  const grid = $("#quality-kpi");
  grid.textContent = "";
  [
    ["处理事件总数", total],
    ["进入分析的事件", q.accepted_events],
    ["被排除事件", q.excluded_events, true],
    ["排除率", total ? `${Math.round(100 * q.excluded_events / total)}%` : "0%", true],
  ].forEach(([label, value, warn]) =>
    grid.appendChild(el("div", { class: `tile ${warn ? "warn" : ""}` },
      el("div", { class: "label", text: label }),
      el("div", { class: "value", text: intFmt(value) }))));
  makeTable("#table-excluded", [
    { label: "识别规则", key: "rule" },
    { label: "事件数", key: "n", numeric: true },
  ], q.excluded);
  makeTable("#table-examples", [
    { label: "事件 ID", key: "event_id" },
    { label: "规则", key: "rule" },
    { label: "判定明细", key: "detail" },
  ], q.examples);
}

/* =============================================================== render hub */
async function renderOverview() {
  const ov = await api(`/api/overview?${qs()}`);
  renderKpis(ov);
  const daily = await api(`/api/timeseries?${qs()}`);
  renderVolumeChart("chart-daily-volume", daily);
  renderRateLineChart("chart-daily-rates", "legend-rates", daily, [
    { key: "recovery_rate_pct", label: "恢复率" },
    { key: "conversion_rate_pct", label: "转化率" },
  ]);
  renderTopZero(await api(`/api/top_zero_queries?${qs()}`));
}

async function renderCompare() {
  const wc = await api(`/api/breakdown/word_class?${qs()}`);
  makeTable("#table-wc", breakdownCols, wc);
  renderCompareBars("chart-wc", wc.filter((r) => r.observed_zero_nodes), "recovery_rate_pct");
  const ab = await api(`/api/breakdown/ab_version?${qs()}`);
  makeTable("#table-ab", breakdownCols, ab);
  renderCompareBars("chart-ab", ab.filter((r) => r.observed_zero_nodes), "conversion_rate_pct");
}

async function renderPathsView() {
  const extra = {
    has_zero: $("#p-zero").checked ? 1 : "",
    incomplete_only: $("#p-incomplete").checked ? 1 : "",
    limit: 60,
  };
  renderPaths(await api(`/api/paths?${qs(extra)}`));
}
$("#p-refresh").addEventListener("click", renderPathsView);

async function renderAll() {
  const active = $(".view.active").id;
  try {
    if (active === "view-overview") await renderOverview();
    else if (active === "view-paths") await renderPathsView();
    else if (active === "view-compare") await renderCompare();
    else if (active === "view-mapping") await renderMapping();
    else if (active === "view-quality") await renderQuality();
  } catch (e) {
    showToast(`加载失败: ${e.message}`);
  }
}

(async function init() {
  readFilters();
  await loadFilterOptions();
  await renderAll();
})();
