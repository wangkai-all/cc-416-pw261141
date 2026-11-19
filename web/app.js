/* 观测单位沿革分析台前端 —— 纯原生 JS + 内联 SVG，无外部依赖。
   所有来自接口的字符串一律走 textContent/createTextNode，不拼 innerHTML。 */
"use strict";

// ============================================================ 基础工具
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

function fmt(n) {
  if (n === null || n === undefined) return "—";
  return Math.round(n).toLocaleString("zh-CN");
}
function fmtDelta(n) {
  if (n === null || n === undefined) return "—";
  return (n >= 0 ? "+" : "−") + Math.round(Math.abs(n)).toLocaleString("zh-CN");
}
function cssVar(name) {
  return getComputedStyle($("#app")).getPropertyValue(name).trim();
}
function badge(text, cls) {
  const span = document.createElement("span");
  span.className = `badge ${cls}`;
  span.textContent = text;
  return span;
}
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

// ============================================================ 全局状态
const state = {
  meta: null,
  avId: null,
  range: "all",
  tooltip: $("#tooltip"),
};

function currentAv() {
  return state.meta.analysis_versions.find((v) => v.version_id === state.avId);
}
function mappingVidOf(avId) {
  const av = state.meta.analysis_versions.find((v) => v.version_id === avId);
  return av ? av.mapping_version_id : 1;
}
function filterPeriods(periods) {
  if (state.range === "all") return periods;
  const n = parseInt(state.range, 10);
  return periods.slice(-n);
}

// ============================================================ 轻量 SVG 图表库
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c) node.appendChild(c);
  }
  return node;
}

function niceTicks(max, min = 0) {
  if (max <= min) max = min + 1;
  const span = max - min;
  const step0 = span / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 0.001; v += step) {
    ticks.push(Math.round(v * 1000) / 1000);
  }
  return ticks;
}

function showTooltip(evt, rows, title) {
  const tt = state.tooltip;
  tt.textContent = "";
  if (title) tt.appendChild(el("div", { class: "tt-title", text: title }));
  for (const r of rows) {
    const row = el("div", { class: "tt-row" });
    const left = el("span");
    if (r.color) left.appendChild(el("span", { class: "tt-key", style: `background:${r.color}` }));
    left.appendChild(document.createTextNode(r.label));
    row.appendChild(left);
    row.appendChild(el("span", { class: "v", text: r.value }));
    tt.appendChild(row);
  }
  tt.classList.remove("hidden");
  moveTooltip(evt);
}
function moveTooltip(evt) {
  const tt = state.tooltip;
  const pad = 14;
  const rect = tt.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
  tt.style.left = `${x}px`;
  tt.style.top = `${y}px`;
}
function hideTooltip() {
  state.tooltip.classList.add("hidden");
}

/**
 * 多序列折线图（带十字线、单 tooltip 全序列、事件竖带、端点直标）
 * config: { labels:[], series:[{name,color,values:[],dash?}], bands:[{label,index,color?}],
 *           height, yFormat }
 */
function lineChart(container, config) {
  container.textContent = "";
  const labels = config.labels;
  const width = Math.max(640, labels.length * 26 + 80);
  const height = config.height || 320;
  const m = { top: 24, right: 24, bottom: 48, left: 64 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const allVals = config.series.flatMap((s) => s.values).filter((v) => v !== null);
  const yMax = Math.max(...allVals, 1);
  const ticks = niceTicks(yMax);
  const yMaxUse = ticks[ticks.length - 1];
  const x = (i) => m.left + (labels.length <= 1 ? plotW / 2 : (plotW * i) / (labels.length - 1));
  const y = (v) => m.top + plotH - (v / yMaxUse) * plotH;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", width: "100%" });

  // 网格 + y 轴文字（实线发丝网格）
  for (const t of ticks) {
    root.appendChild(svg("line", { class: "grid-line", x1: m.left, x2: width - m.right, y1: y(t), y2: y(t) }));
    root.appendChild(svg("text", { class: "axis-text", x: m.left - 8, y: y(t) + 4, "text-anchor": "end", text: fmt(t) }));
  }
  // x 轴标签：按月/季稀疏显示
  const step = Math.ceil(labels.length / 16);
  labels.forEach((lb, i) => {
    if (i % step === 0 || i === labels.length - 1) {
      root.appendChild(svg("text", {
        class: "axis-text", x: x(i), y: height - m.bottom + 18, "text-anchor": "middle", text: lb,
      }));
    }
  });
  root.appendChild(svg("line", { class: "zero-line", x1: m.left, x2: width - m.right, y1: m.top + plotH, y2: m.top + plotH }));

  // 事件竖带（单位调整生效期）
  for (const b of config.bands || []) {
    const bx = x(b.index) - 10;
    root.appendChild(svg("rect", { class: "event-band", x: bx, y: m.top, width: 20, height: plotH, fill: b.color || cssVar("--series-4") }));
    root.appendChild(svg("line", { class: "edge-marker", x1: x(b.index), x2: x(b.index), y1: m.top, y2: m.top + plotH }));
    const t = svg("text", { class: "axis-text", x: x(b.index), y: m.top - 8, "text-anchor": "middle" });
    t.textContent = b.label;
    root.appendChild(t);
  }

  // 序列
  config.series.forEach((s) => {
    const pts = s.values.map((v, i) => (v === null ? null : `${x(i)},${y(v)}`)).filter(Boolean);
    root.appendChild(svg("polyline", {
      class: "series-line", points: pts.join(" "),
      stroke: s.color, "stroke-dasharray": s.dash || null,
    }));
    // 端点直标（选择性标注，只标最后一个有效值）
    const lastIdx = s.values.map((v, i) => (v === null ? -1 : i)).filter((i) => i >= 0).pop();
    if (lastIdx !== undefined) {
      root.appendChild(svg("circle", { class: "marker", cx: x(lastIdx), cy: y(s.values[lastIdx]), r: 4, fill: s.color }));
      const lbl = svg("text", {
        class: "axis-text", x: x(lastIdx) + 7, y: y(s.values[lastIdx]) + 4, "font-weight": 600,
      });
      lbl.textContent = fmt(s.values[lastIdx]);
      root.appendChild(lbl);
    }
  });

  // 十字线 + 全序列 tooltip（命中带为整列，不要求压在线上）
  const cross = svg("line", { stroke: cssVar("--muted"), "stroke-width": 1, visibility: "hidden", y1: m.top, y2: m.top + plotH });
  root.appendChild(cross);
  const hit = svg("rect", { x: m.left, y: m.top, width: plotW, height: plotH, fill: "transparent" });
  root.appendChild(hit);
  hit.addEventListener("pointermove", (evt) => {
    const [mx] = clientToSvg(evt, root);
    const i = Math.round(((mx - m.left) / plotW) * (labels.length - 1));
    const idx = Math.max(0, Math.min(labels.length - 1, i));
    cross.setAttribute("x1", x(idx));
    cross.setAttribute("x2", x(idx));
    cross.setAttribute("visibility", "visible");
    showTooltip(evt, config.series.map((s) => ({
      label: s.name, color: s.color, value: s.values[idx] === null ? "无数据（单位当时不存在）" : fmt(s.values[idx]),
    })), labels[idx]);
  });
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    hideTooltip();
  });

  // 图例（>=2 序列恒有）；重绘前清掉同面板散落的旧图例，避免堆积
  if (config.series.length >= 2) {
    const panel = container.closest(".panel");
    if (panel) panel.querySelectorAll(".legend").forEach((n) => n.remove());
    const lg = el("div", { class: "legend" });
    for (const s of config.series) {
      const key = el("span", { class: "key" });
      key.appendChild(el("span", { class: "swatch", style: `background:${s.color}` }));
      key.appendChild(document.createTextNode(s.name));
      lg.appendChild(key);
    }
    container.parentElement.insertBefore(lg, container);
  }
  container.appendChild(root);
}

/** 发散条形图：结构调整（灰）+ 有机变化（蓝），零轴居中 */
function divergingBarChart(container, config) {
  container.textContent = "";
  const labels = config.labels;
  const width = Math.max(640, labels.length * 26 + 80);
  const height = config.height || 300;
  const m = { top: 20, right: 24, bottom: 48, left: 64 };
  const plotW = width - m.left - m.right;
  const plotH = height - m.top - m.bottom;
  const maxAbs = Math.max(1, ...config.groups.flatMap((g) => g.values).map((v) => Math.abs(v || 0)));
  const ticks = niceTicks(maxAbs);
  const lim = ticks[ticks.length - 1];
  const zeroX = m.left + plotW / 2;
  const x = (v) => zeroX + (v / lim) * (plotW / 2);
  const bandW = plotW / labels.length;
  const barW = Math.min(10, bandW / 3);

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", width: "100%" });
  for (const t of ticks) {
    for (const sign of [-1, 1]) {
      const gx = x(t * sign);
      root.appendChild(svg("line", { class: "grid-line", x1: gx, x2: gx, y1: m.top, y2: m.top + plotH }));
      root.appendChild(svg("text", { class: "axis-text", x: gx, y: m.top + plotH + 16, "text-anchor": "middle", text: sign === 1 ? fmt(t) : t === 0 ? "0" : `−${fmt(t)}` }));
    }
  }
  const step = Math.ceil(labels.length / 16);
  labels.forEach((lb, i) => {
    if (i % step === 0 || i === labels.length - 1) {
      root.appendChild(svg("text", { class: "axis-text", x: m.left + bandW * (i + 0.5), y: height - 8, "text-anchor": "middle", text: lb }));
    }
  });
  root.appendChild(svg("line", { class: "zero-line", x1: zeroX, x2: zeroX, y1: m.top, y2: m.top + plotH }));

  config.groups.forEach((g, gi) => {
    g.values.forEach((v, i) => {
      if (v === null || v === undefined || v === 0) return;
      const cx = m.left + bandW * (i + 0.5) + (gi - (config.groups.length - 1) / 2) * (barW + 2);
      const x0 = zeroX;
      const x1 = x(v);
      const rect = svg("rect", {
        x: Math.min(x0, x1), y: m.top + 8, width: Math.max(1.5, Math.abs(x1 - x0)),
        height: barW, rx: 2, fill: g.color,
      });
      rect.addEventListener("pointermove", (evt) => showTooltip(evt, [
        { label: g.name, color: g.color, value: fmtDelta(v) },
        ...config.groups.filter((x2) => x2 !== g).map((og) => ({ label: og.name, color: og.color, value: fmtDelta(og.values[i]) })),
      ], labels[i]));
      rect.addEventListener("pointerleave", hideTooltip);
      root.appendChild(rect);
    });
  });
  container.appendChild(root);

  const panel = container.closest(".panel");
  if (panel) panel.querySelectorAll(".legend").forEach((n) => n.remove());
  const lg = el("div", { class: "legend" });
  for (const g of config.groups) {
    const key = el("span", { class: "key" });
    key.appendChild(el("span", { class: "swatch rect", style: `background:${g.color}` }));
    key.appendChild(document.createTextNode(g.name));
    lg.appendChild(key);
  }
  container.parentElement.insertBefore(lg, container);
}

function clientToSvg(evt, svgRoot) {
  const pt = svgRoot.createSVGPoint();
  pt.x = evt.clientX;
  pt.y = evt.clientY;
  const ctm = svgRoot.getScreenCTM();
  const p = pt.matrixTransform(ctm.inverse());
  return [p.x, p.y];
}

// ============================================================ 通用表格
function makeTable(container, columns, rows, opts = {}) {
  container.textContent = "";
  const table = el("table");
  const thead = el("thead");
  const trh = el("tr");
  for (const c of columns) {
    trh.appendChild(el("th", { class: c.numeric ? "num" : "", text: c.title }));
  }
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const c of columns) {
      const td = el("td", { class: c.numeric ? "num" : "" });
      const v = c.render ? c.render(row) : row[c.key];
      if (v instanceof Node) td.appendChild(v);
      else td.textContent = v === null || v === undefined ? "—" : String(v);
      tr.appendChild(td);
    }
    if (opts.rowClick) tr.style.cursor = "pointer", tr.addEventListener("click", () => opts.rowClick(row));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  container.appendChild(table);
}

// ============================================================ 数据表切换
document.addEventListener("click", (evt) => {
  const btn = evt.target.closest("[data-table]");
  if (!btn) return;
  const box = document.getElementById(btn.dataset.table);
  box.classList.toggle("hidden");
  btn.textContent = box.classList.contains("hidden") ? "查看数据表" : "收起数据表";
});

// ============================================================ 标签页与主题
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#tab-${t.dataset.tab}`).classList.add("active");
  if (t.dataset.tab === "runs") loadRunsTab();
  if (t.dataset.tab === "lineage") loadLineageTab();
  if (t.dataset.tab === "source") loadSourceTab();
}));

$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme === "dark";
  root.dataset.theme = dark ? "light" : "dark";
  $("#theme-toggle").textContent = dark ? "切换暗色" : "切换亮色";
  renderAll();
});
