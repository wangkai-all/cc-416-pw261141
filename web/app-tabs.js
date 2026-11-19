/* 各标签页的数据加载与渲染 */
"use strict";

// ============================================================ 启动
async function boot() {
  state.meta = await api("/api/meta");
  initGlobalControls();
  await renderAll();
  setInterval(refreshRunsQuietly, 4000); // 后台运行状态轮询
}

function initGlobalControls() {
  const sel = $("#global-av");
  sel.textContent = "";
  for (const av of state.meta.analysis_versions) {
    sel.appendChild(el("option", { value: av.version_id, text: av.label }));
  }
  state.avId = state.meta.analysis_versions.at(-1).version_id;
  sel.value = String(state.avId);
  sel.addEventListener("change", () => {
    state.avId = parseInt(sel.value, 10);
    renderAll();
  });
  $("#range-preset").addEventListener("change", (e) => {
    state.range = e.target.value;
    renderAll();
  });
  const lo = state.meta.units.map((u) => u.first_period).sort()[0];
  const hi = state.meta.units.map((u) => u.first_period).sort().at(-1);
  $("#data-window").textContent = `数据窗口 ${lo} 起 · 共 ${state.meta.units.length} 个规范单位`;
}

async function renderAll() {
  // 移除旧图例（lineChart 会重新插入）
  $$(".panel .legend:not(#decomp-legend)").forEach((n) => n.remove());
  await Promise.all([renderOverview(), renderUnitTab(), renderCompareTab()]);
}

async function getSeries(avId, kind, code) {
  return api(`/api/analysis/series?av=${avId}&kind=${kind}&code=${encodeURIComponent(code)}`);
}

// ============================================================ 1. 总览
async function renderOverview() {
  const av = currentAv();
  const meta = state.meta;
  const mvid = av.mapping_version_id;
  const mv = meta.mapping_versions.find((v) => v.version_id === mvid);

  // KPI 行：最新期间队列指标
  const cohortIds = Object.keys(mv.cohorts).sort();
  $("#overview-cohort").innerHTML = "";
  for (const cid of cohortIds) {
    $("#overview-cohort").appendChild(el("option", { value: cid, text: `${cid}（${mv.cohorts[cid].length} 个单位）` }));
  }
  $("#overview-cohort").onchange = renderOverview;

  const kpiRow = $("#kpi-row");
  kpiRow.textContent = "";
  const allCohortSeries = await Promise.all(
    cohortIds.map((cid) => getSeries(av.version_id, "cohort", cid))
  );
  const latest = allCohortSeries[0].at(-1).period;
  let totalLatest = 0;
  let totalPrev = 0;
  for (const s of allCohortSeries) {
    totalLatest += s.at(-1).as_reported;
    if (s.length >= 2) totalPrev += s.at(-2).as_reported;
  }
  const tiles = [
    { label: `全网络当期件量（${latest} 当时口径）`, value: fmt(totalLatest), delta: totalLatest - totalPrev },
    { label: "规范单位 / 队列", value: `${meta.units.length} / ${cohortIds.length}`, delta: null },
    { label: "隔离区待处理", value: String(Object.values(meta.quarantine_summary).reduce((a, b) => a + b, 0)),
      delta: null, warn: true },
    { label: "当前绑定映射", value: mv.label, delta: null, text: true },
  ];
  for (const t of tiles) {
    const tile = el("div", { class: "tile" });
    tile.appendChild(el("div", { class: "label", text: t.label }));
    tile.appendChild(el("div", { class: "value", text: t.value }));
    if (t.delta !== null) {
      const cls = t.delta > 0 ? "up" : t.delta < 0 ? "down" : "flat";
      tile.appendChild(el("div", { class: `delta ${cls}`, text: `环比 ${fmtDelta(t.delta)}` }));
    }
    if (t.warn) tile.appendChild(el("div", { class: "delta flat", text: Object.entries(meta.quarantine_summary).map(([k, v]) => `${k}:${v}`).join(" · ") }));
    kpiRow.appendChild(tile);
  }

  // 选中队列双口径折线
  const cid = $("#overview-cohort").value || cohortIds[0];
  const series = await getSeries(av.version_id, "cohort", cid);
  const periods = filterPeriods(series.map((r) => r.period));
  const idx0 = series.findIndex((r) => r.period === periods[0]);
  const slice = series.slice(idx0);
  $("#cohort-note").textContent =
    `队列 ${cid} 是沿革连通分量（成员：${mv.cohorts[cid].join("、")}）。拆/合/迁址只在单位间搬移件量，` +
    "队列层面两条线始终重合——队列总量不受单位调整影响。";
  lineChart($("#cohort-chart"), {
    labels: slice.map((r) => r.period),
    series: [
      { name: "当时口径 as-reported（直接重述历史）", color: cssVar("--as-reported"), values: slice.map((r) => r.as_reported), dash: "6 4" },
      { name: "最新口径 restated（按当前边界重述）", color: cssVar("--restated"), values: slice.map((r) => r.restated) },
    ],
    bands: await edgeBands(mvid, slice.map((r) => r.period)),
  });
  makeTable($("#cohort-table"), [
    { title: "期间", key: "period" },
    { title: "当时口径", key: "as_reported", numeric: true, render: (r) => fmt(r.as_reported) },
    { title: "最新口径（重述）", key: "restated", numeric: true, render: (r) => fmt(r.restated) },
    { title: "两口径差异", numeric: true, render: (r) => fmtDelta(r.restated - r.as_reported) },
  ], slice);

  // 队列汇总表（最新期间）
  const summaryRows = [];
  for (let i = 0; i < cohortIds.length; i++) {
    const s = allCohortSeries[i];
    const last = s.at(-1);
    const prev = s.at(-2);
    summaryRows.push({
      cohort: cohortIds[i],
      members: mv.cohorts[cohortIds[i]].join("、"),
      ar: last.as_reported,
      rs: last.restated,
      delta: last.as_reported - prev.as_reported,
    });
  }
  makeTable($("#cohort-summary"), [
    { title: "队列", key: "cohort" },
    { title: "成员单位", key: "members" },
    { title: `${latest} 当时口径`, key: "ar", numeric: true, render: (r) => fmt(r.ar) },
    { title: `${latest} 重述口径`, key: "rs", numeric: true, render: (r) => fmt(r.rs) },
    { title: "环比", key: "delta", numeric: true, render: (r) => fmtDelta(r.delta) },
  ], summaryRows);
}

async function edgeBands(mvid, periods) {
  const edges = await api(`/api/lineage/edges?vid=${mvid}`);
  return edges
    .filter((e) => e.review_status === "approved" && periods.includes(e.effective_period))
    .map((e) => ({ index: periods.indexOf(e.effective_period), label: `${e.from_unit}→${e.to_unit}` }));
}

// ============================================================ 2. 单位分析
async function renderUnitTab() {
  const av = currentAv();
  const unitSel = $("#unit-select");
  if (!unitSel.dataset.inited) {
    for (const u of state.meta.units) {
      unitSel.appendChild(el("option", { value: u.code, text: `${u.code} ${u.name}` }));
    }
    unitSel.dataset.inited = "1";
    unitSel.addEventListener("change", renderUnitTab);
  }
  const code = unitSel.value || state.meta.units[0].code;
  const unit = state.meta.units.find((u) => u.code === code);
  const [series, decomp, adjustments] = await Promise.all([
    getSeries(av.version_id, "unit", code),
    api(`/api/analysis/decomp?av=${av.version_id}&kind=unit&code=${code}`),
    api(`/api/analysis/adjustments?av=${av.version_id}&unit=${code}`),
  ]);

  const periods = filterPeriods(series.map((r) => r.period));
  const idx0 = series.findIndex((r) => r.period === periods[0]);
  const slice = series.slice(idx0);
  const life = unit.last_period
    ? `存续 ${unit.first_period} ~ ${unit.last_period}（已退役）`
    : `存续 ${unit.first_period} 至今（在役）`;
  $("#unit-note").textContent =
    `${life}。灰虚线是当时口径：单位成立前/移交后没有数据是历史事实；蓝线是按当前单位边界重述的口径，` +
    "黄色竖带标记影响本单位的拆/合/迁址生效期。";

  lineChart($("#unit-chart"), {
    labels: slice.map((r) => r.period),
    series: [
      { name: "当时口径 as-reported", color: cssVar("--as-reported"), values: slice.map((r) => r.as_reported), dash: "6 4" },
      { name: "最新口径 restated", color: cssVar("--restated"), values: slice.map((r) => r.restated) },
    ],
    bands: (await edgeBands(av.mapping_version_id, slice.map((r) => r.period)))
      .filter((b) => b.label.includes(code)),
  });
  makeTable($("#unit-table"), [
    { title: "期间", key: "period" },
    { title: "当时口径", numeric: true, render: (r) => fmt(r.as_reported) },
    { title: "重述口径", numeric: true, render: (r) => fmt(r.restated) },
    { title: "重述准时件", key: "on_time", numeric: true, render: (r) => fmt(r.on_time) },
    { title: "重述延误件", key: "late", numeric: true, render: (r) => fmt(r.late) },
  ], slice);

  // 分解图（只画筛选窗口、跳过首期无环比）
  const dslice = decomp.filter((d) => periods.includes(d.period) && d.total_change !== null);
  divergingBarChart($("#decomp-chart"), {
    labels: dslice.map((d) => d.period),
    groups: [
      { name: "结构调整（单位边界变化，不算业务增长）", color: cssVar("--adjustment"), values: dslice.map((d) => d.structural_adjustment) },
      { name: "有机变化（真实业务增减）", color: cssVar("--series-1"), values: dslice.map((d) => d.organic_change) },
    ],
  });
  makeTable($("#decomp-table"), [
    { title: "期间", key: "period" },
    { title: "当时口径", numeric: true, render: (r) => fmt(r.as_reported) },
    { title: "环比总变化", numeric: true, render: (r) => fmtDelta(r.total_change) },
    { title: "结构调整净额", numeric: true, render: (r) => fmtDelta(r.structural_adjustment) },
    { title: "有机变化", numeric: true, render: (r) => fmtDelta(r.organic_change) },
  ], dslice);

  // 追溯期间下拉
  const tp = $("#trace-period");
  if (!tp.dataset.inited) {
    tp.dataset.inited = "1";
    $("#trace-run").addEventListener("click", () => runTrace(av.version_id, code, tp.value));
  }
  tp.innerHTML = "";
  for (const r of series) tp.appendChild(el("option", { value: r.period, text: r.period }));
  tp.value = series.some((r) => r.period === "2025-09") ? "2025-09" : series.at(-1).period;
  runTrace(av.version_id, code, tp.value);
}

async function runTrace(avId, unit, period) {
  const rows = await api(`/api/analysis/trace?av=${avId}&unit=${unit}&period=${period}`);
  if (!rows.length) {
    $("#trace-table").innerHTML = "";
    $("#trace-table").appendChild(el("p", { class: "muted", text: "该期间没有可追溯到本单位的源事件。" }));
    return;
  }
  makeTable($("#trace-table"), [
    { title: "源事件 ID", key: "source_event_id" },
    { title: "批次", key: "batch_id" },
    { title: "上报代码", key: "reported_code" },
    { title: "来源单位", key: "source_unit" },
    { title: "沿革路径", render: (r) => r.path.join(" → ") },
    { title: "路径权重", key: "weight", numeric: true },
    { title: "事件件量", key: "parcels", numeric: true, render: (r) => fmt(r.parcels) },
    { title: "贡献件量", key: "contribution", numeric: true, render: (r) => fmt(r.contribution) },
    { title: "备注/异常单", key: "raw_note", render: (r) => r.raw_note || "" },
  ], rows);
}

// ============================================================ 3. 版本对比
async function renderCompareTab() {
  const avs = state.meta.analysis_versions;
  for (const [selId, idx] of [["#compare-a", 0], ["#compare-b", avs.length - 1]]) {
    const sel = $(selId);
    if (!sel.dataset.inited) {
      for (const av of avs) sel.appendChild(el("option", { value: av.version_id, text: av.label }));
      sel.dataset.inited = "1";
      sel.addEventListener("change", renderCompareTab);
    }
    sel.value = String(avs[Math.min(idx, avs.length - 1)].version_id);
  }
  const unitSel = $("#compare-unit");
  if (!unitSel.dataset.inited) {
    for (const u of state.meta.units) unitSel.appendChild(el("option", { value: u.code, text: `${u.code} ${u.name}` }));
    unitSel.value = "U07";
    unitSel.dataset.inited = "1";
    unitSel.addEventListener("change", renderCompareTab);
  }
  const aId = parseInt($("#compare-a").value, 10);
  const bId = parseInt($("#compare-b").value, 10);
  const code = unitSel.value;
  const [sa, sb] = await Promise.all([getSeries(aId, "unit", code), getSeries(bId, "unit", code)]);
  const mapB = new Map(sb.map((r) => [r.period, r]));
  const periods = filterPeriods(sa.map((r) => r.period));
  const rows = sa.filter((r) => periods.includes(r.period)).map((r) => ({
    period: r.period,
    a_rep: r.as_reported,
    a_rest: r.restated,
    b_rep: mapB.get(r.period)?.as_reported ?? null,
    b_rest: mapB.get(r.period)?.restated ?? null,
  }));
  const note = code === "U07"
    ? "U07 老城枢纽：映射 v1 漏接 U06 西关站的合并边，修订版中其历史重述线整体上移；两版当时口径完全相同——修订映射不重写历史。"
    : "对比两个分析版：灰线（当时口径）在版本间恒等，蓝/紫线（重述）随映射修订变化。";
  $("#compare-note").textContent = note;
  lineChart($("#compare-chart"), {
    labels: rows.map((r) => r.period),
    series: [
      { name: `A 重述（${$("#compare-a").selectedOptions[0].textContent}）`, color: cssVar("--series-7"), values: rows.map((r) => r.a_rest) },
      { name: `B 重述（${$("#compare-b").selectedOptions[0].textContent}）`, color: cssVar("--restated"), values: rows.map((r) => r.b_rest) },
      { name: "当时口径（两版恒等）", color: cssVar("--as-reported"), values: rows.map((r) => r.a_rep), dash: "6 4" },
    ],
    bands: [],
  });
  makeTable($("#compare-table"), [
    { title: "期间", key: "period" },
    { title: "A 重述", numeric: true, render: (r) => fmt(r.a_rest) },
    { title: "B 重述", numeric: true, render: (r) => fmt(r.b_rest) },
    { title: "当时口径", numeric: true, render: (r) => fmt(r.a_rep) },
  ], rows);
  const diffRows = rows.filter((r) => r.b_rest !== r.a_rest);
  makeTable($("#diff-table"), [
    { title: "期间", key: "period" },
    { title: "A 重述", numeric: true, render: (r) => fmt(r.a_rest) },
    { title: "B 重述", numeric: true, render: (r) => fmt(r.b_rest) },
    { title: "重述差异 B−A", numeric: true, render: (r) => {
      const d = r.b_rest - r.a_rest;
      return el("span", { class: d > 0 ? "up" : d < 0 ? "down" : "", text: fmtDelta(d) });
    } },
    { title: "当时口径差异（恒为 0）", numeric: true, render: (r) => fmtDelta(r.b_rep - r.a_rep) },
  ], diffRows.length ? diffRows : [{ period: "无差异", a_rest: null, b_rest: null, b_rep: null, a_rep: null }]);
}
