/* 沿革审阅、来源隔离、重算审计三个管理标签页 */
"use strict";

// ============================================================ 4. 沿革审阅
async function loadLineageTab() {
  const vidSel = $("#lineage-vid");
  if (!vidSel.dataset.inited) {
    for (const mv of state.meta.mapping_versions) {
      vidSel.appendChild(el("option", { value: mv.version_id, text: mv.label }));
    }
    vidSel.dataset.inited = "1";
    vidSel.addEventListener("change", renderLineageTab);
    $("#revise-btn").addEventListener("click", reviseVersion);
  }
  vidSel.value = String(mappingVidOf(state.avId));
  await renderLineageTab();
}

async function renderLineageTab() {
  const vid = parseInt($("#lineage-vid").value, 10);
  const [edges, entries] = await Promise.all([
    api(`/api/lineage/edges?vid=${vid}`),
    api(`/api/lineage/entries?vid=${vid}`),
  ]);

  makeTable($("#edges-table"), [
    { title: "ID", key: "id" },
    { title: "类型", key: "edge_type", render: (r) => ({ split: "拆分", merge: "合并", relocation: "迁址更名" })[r.edge_type] },
    { title: "移出单位", render: (r) => `${r.from_unit} ${r.from_name}` },
    { title: "接收单位", render: (r) => `${r.to_unit} ${r.to_name}` },
    { title: "生效期", key: "effective_period" },
    { title: "权重", key: "weight", numeric: true },
    { title: "审阅状态", render: (r) => badge(
      { proposed: "待审阅", approved: "已通过", rejected: "已驳回" }[r.review_status], r.review_status) },
    { title: "说明", key: "note" },
    { title: "操作", render: (r) => {
      const box = el("span", { style: "display:flex;gap:6px" });
      if (r.review_status !== "approved") {
        box.appendChild(el("button", { class: "ghost small", text: "通过", onclick: () => reviewEdge(r, "approved") }));
      }
      box.appendChild(el("button", { class: "ghost small", text: "改权重", onclick: () => changeWeight(r, vid) }));
      if (r.review_status === "proposed") {
        box.appendChild(el("button", { class: "ghost small danger", text: "驳回", onclick: () => reviewEdge(r, "rejected") }));
      }
      return box;
    } },
  ], edges);

  renderLineageGraph(edges.filter((e) => e.review_status === "approved"));

  makeTable($("#entries-table"), [
    { title: "上报代码", key: "reported_code" },
    { title: "规范单位", key: "canonical_unit" },
    { title: "有效期起", key: "valid_from" },
    { title: "有效期止", render: (r) => r.valid_to || "至今" },
  ], entries);
}

function renderLineageGraph(edges) {
  const box = $("#lineage-graph");
  box.textContent = "";
  const units = state.meta.units;
  const byRegion = {};
  for (const u of units) (byRegion[u.region] ||= []).push(u);
  const laneH = 150;
  const laneW = Math.max(900, units.length * 130);
  const root = svg("svg", { viewBox: `0 0 ${laneW} ${Object.keys(byRegion).length * laneH + 20}`, width: "100%" });

  // 节点位置：按区域分泳道，单位按 code 排开
  const pos = {};
  Object.entries(byRegion).forEach(([region, us], li) => {
    const y = li * laneH + 70;
    root.appendChild(svg("text", { x: 16, y: y - 44, fill: cssVar("--muted"), "font-size": 12, text: region }));
    root.appendChild(svg("rect", { class: "cohort-lane", x: 8, y: y - 34, width: laneW - 16, height: laneH - 40, rx: 10 }));
    us.forEach((u, i) => {
      const x = 90 + i * 135;
      pos[u.code] = { x, y };
    });
  });

  const typeColor = { split: cssVar("--series-1"), merge: cssVar("--series-2"), relocation: cssVar("--series-3") };
  // 边先画（压在节点下层）
  for (const e of edges) {
    const a = pos[e.from_unit];
    const b = pos[e.to_unit];
    if (!a || !b) continue;
    const sameLane = Math.abs(a.y - b.y) < 10;
    const d = sameLane
      ? `M ${a.x + 52} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x - 52} ${b.y}`
      : `M ${a.x + 40} ${a.y + 18} C ${a.x + 40} ${(a.y + b.y) / 2}, ${b.x - 40} ${(a.y + b.y) / 2}, ${b.x - 40} ${b.y - 18}`;
    root.appendChild(svg("path", { class: "edge-path", d, stroke: typeColor[e.edge_type] }));
    const mid = sameLane ? { x: (a.x + b.x) / 2, y: a.y - 8 } : { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const t = svg("text", { class: "edge-label", x: mid.x, y: mid.y, "text-anchor": "middle" });
    t.textContent = `${({ split: "拆", merge: "合", relocation: "迁" })[e.edge_type]} ${e.weight * 100}% · ${e.effective_period}`;
    root.appendChild(t);
  }
  // 节点
  for (const u of units) {
    const { x, y } = pos[u.code];
    const g = svg("g");
    g.appendChild(svg("rect", {
      class: `node-rect ${u.status === "retired" ? "node-retired" : ""}`,
      x: x - 52, y: y - 24, width: 104, height: 48, rx: 8,
    }));
    const t1 = svg("text", { x, y: y - 4, "text-anchor": "middle", "font-weight": 650 });
    t1.textContent = u.code;
    const t2 = svg("text", { x, y: y + 13, "text-anchor": "middle", "font-size": 11, fill: cssVar("--text-secondary") });
    t2.textContent = u.name.length > 7 ? u.name.slice(0, 7) + "…" : u.name;
    g.appendChild(t1);
    g.appendChild(t2);
    if (u.status === "retired") {
      const t3 = svg("text", { x, y: y + 30, "text-anchor": "middle", "font-size": 10.5, fill: cssVar("--muted") });
      t3.textContent = `退役于 ${u.last_period}`;
      g.appendChild(t3);
    }
    const title = svg("title");
    title.textContent = `${u.code} ${u.name}｜${u.region}｜${u.first_period}${u.last_period ? " ~ " + u.last_period : " 至今"}`;
    g.appendChild(title);
    root.appendChild(g);
  }
  box.appendChild(root);
}

async function reviewEdge(edge, action) {
  const comment = prompt(`确认${action === "approved" ? "通过" : "驳回"}沿革边 ${edge.from_unit}→${edge.to_unit}（可填审阅意见）`, "");
  if (comment === null) return;
  try {
    await api("/api/lineage/review", {
      method: "POST",
      body: { edge_id: edge.id, action, comment, reviewer: "值班分析员" },
    });
    toast("审阅已记录；修订映射后请对相应分析版执行重算", "success");
    await refreshMeta();
    await renderLineageTab();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function changeWeight(edge, vid) {
  const w = prompt(`修改 ${edge.from_unit}→${edge.to_unit} 权重（0,1]，当前 ${edge.weight}）`, String(edge.weight));
  if (w === null) return;
  try {
    const weight = parseFloat(w);
    await api("/api/lineage/edge-weight", { method: "POST", body: { version_id: vid, edge_id: edge.id, weight } });
    toast("权重已修订（新版本请走“修订出新版本”后重算分析版）", "success");
    await refreshMeta();
    await renderLineageTab();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function reviseVersion() {
  const label = prompt("新映射版本名称", `映射 v${state.meta.mapping_versions.length + 1}（分析员修订）`);
  if (!label) return;
  const description = prompt("修订说明", "页面发起的映射修订");
  if (description === null) return;
  try {
    const r = await api("/api/lineage/revise", { method: "POST", body: { label, description } });
    toast(`已生成映射版本 v${r.version_id}，可在其中调整边并创建新分析版`, "success");
    await refreshMeta();
    const sel = $("#lineage-vid");
    sel.appendChild(el("option", { value: r.version_id, text: label }));
    sel.value = String(r.version_id);
    await renderLineageTab();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ============================================================ 5. 来源与隔离
async function loadSourceTab() {
  await renderSourceTab();
  if (!$("#ingest-demo").dataset.bound) {
    $("#ingest-demo").dataset.bound = "1";
    $("#ingest-demo").onclick = ingestDemo;
    $("#event-status").onchange = renderSourceTab;
  }
}

async function renderSourceTab() {
  const [quarantine, events] = await Promise.all([
    api("/api/quarantine"),
    api(`/api/events?status=${$("#event-status").value}`),
  ]);
  const accepted = state.meta.event_counts.accepted || 0;
  const qCount = quarantine.length;
  $("#source-kpi").innerHTML = "";
  for (const t of [
    { label: "已接受源事件", value: accepted },
    { label: "隔离中", value: qCount },
    { label: "已拒绝", value: state.meta.event_counts.rejected || 0 },
  ]) {
    const tile = el("div", { class: "tile" });
    tile.appendChild(el("div", { class: "label", text: t.label }));
    tile.appendChild(el("div", { class: "value", text: fmt(t.value) }));
    $("#source-kpi").appendChild(tile);
  }

  const reasonText = {
    duplicate: "重复批次", retired_target: "落入退役单位", unknown_code: "未知上报代码", outlier: "疑似异常值",
  };
  makeTable($("#quarantine-table"), [
    { title: "事件 ID", key: "id" },
    { title: "批次", key: "batch_id" },
    { title: "上报代码", key: "reported_code" },
    { title: "期间", key: "period" },
    { title: "件量", key: "parcels", numeric: true, render: (r) => fmt(r.parcels) },
    { title: "隔离原因", render: (r) => badge(reasonText[r.quarantine_reason] || r.quarantine_reason, r.quarantine_reason === "outlier" ? "interrupted" : "critical") },
    { title: "说明", key: "raw_note" },
    { title: "操作", render: (r) => {
      const box = el("span", { style: "display:flex;gap:6px" });
      const hard = r.quarantine_reason === "retired_target" || r.quarantine_reason === "unknown_code";
      if (!hard) box.appendChild(el("button", { class: "ghost small", text: "解除隔离", onclick: () => quarantineAction(r, "release") }));
      else {
        const b = el("button", { class: "ghost small", text: "解除隔离（拦截）", onclick: () => quarantineAction(r, "release") });
        b.title = "退役/未知代码必须先处理映射";
        box.appendChild(b);
      }
      box.appendChild(el("button", { class: "ghost small danger", text: "拒绝", onclick: () => quarantineAction(r, "reject") }));
      return box;
    } },
  ], quarantine);

  makeTable($("#events-table"), [
    { title: "ID", key: "id" },
    { title: "批次", key: "batch_id" },
    { title: "上报代码", key: "reported_code" },
    { title: "期间", key: "period" },
    { title: "件量", key: "parcels", numeric: true, render: (r) => fmt(r.parcels) },
    { title: "状态", render: (r) => badge(
      { accepted: "已接受", quarantined: "隔离中", rejected: "已拒绝" }[r.status], r.status) },
    { title: "规范单位", key: "canonical_unit", render: (r) => r.canonical_unit || "（无法映射）" },
    { title: "备注", key: "raw_note", render: (r) => r.raw_note || "" },
  ], events.slice(0, 200));
}

async function quarantineAction(row, action) {
  try {
    await api(`/api/quarantine/${action}`, { method: "POST", body: { event_id: row.id } });
    toast(action === "release" ? "事件已解除隔离并接受，请重算受影响分析版" : "事件已拒绝", "success");
    await refreshMeta();
    await renderSourceTab();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function ingestDemo() {
  const batchId = `B-DEMO-${Date.now().toString(36)}`;
  const body = {
    batch_id: batchId,
    source_name: "页面演示投递",
    note: "包含正常补报、退役单位误投与重复数据各一条",
    items: [
      { reported_code: "S08", period: "2026-09", parcels: 17200, raw_note: "正常月度补报" },
      { reported_code: "S03", period: "2026-09", parcels: 4100, raw_note: "误投到已退役的 S03（应改投 S04）" },
      { reported_code: "S08", period: "2026-09", parcels: 17200, raw_note: "同批次重复行" },
    ],
  };
  try {
    const r = await api("/api/ingest", { method: "POST", body });
    toast(`批次 ${r.batch_id}：接受 ${r.accepted} 条，隔离 ${r.quarantined} 条`, r.quarantined ? "success" : "success");
    await refreshMeta();
    await renderSourceTab();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ============================================================ 6. 重算与审计
async function loadRunsTab() {
  await renderRunsTab();
  if (!$("#demo-interrupt").dataset.bound) {
    $("#demo-interrupt").dataset.bound = "1";
    $("#demo-interrupt").onclick = demoInterruptResume;
  }
}

async function renderRunsTab() {
  const [runs, audit] = await Promise.all([
    api("/api/analysis/runs"),
    api("/api/audit"),
  ]);
  const statusText = {
    running: "运行中", interrupted: "已中断", completed: "已完成", failed: "失败", superseded: "被取代",
  };
  makeTable($("#runs-table"), [
    { title: "运行 ID", key: "id" },
    { title: "分析版", key: "analysis_label" },
    { title: "状态", render: (r) => badge(statusText[r.status], r.status) },
    { title: "检查点", render: (r) => r.last_checkpoint_period || "—" },
    { title: "总期间数", key: "total_periods", numeric: true },
    { title: "进度", render: (r) => {
      const done = r.last_checkpoint_period ? periodOrdinal(r.last_checkpoint_period, r.total_periods) : 0;
      const pct = Math.round((done / Math.max(1, r.total_periods)) * 100);
      const wrap = el("span", { style: "display:flex;gap:8px;align-items:center" });
      wrap.appendChild(el("span", { class: "progress" }, el("span", { style: `width:${r.status === "completed" ? 100 : pct}%` })));
      wrap.appendChild(document.createTextNode(`${r.status === "completed" ? 100 : pct}%`));
      return wrap;
    } },
    { title: "开始时间", key: "started_at" },
    { title: "操作", render: (r) => {
      const box = el("span", { style: "display:flex;gap:6px" });
      if (r.status === "running") {
        box.appendChild(el("button", { class: "ghost small", text: "请求中断", onclick: () => interruptRun(r.id) }));
      }
      if (r.status === "interrupted" || r.status === "failed") {
        box.appendChild(el("button", { class: "ghost small primary", text: "从检查点恢复", onclick: () => resumeRun(r.id) }));
      }
      return box;
    } },
  ], runs);

  makeTable($("#audit-table"), [
    { title: "时间", key: "created_at" },
    { title: "动作", key: "action" },
    { title: "对象", key: "entity" },
    { title: "详情", key: "detail" },
  ], audit);
}

function periodOrdinal(period, total) {
  // 以数据窗口首期间 2023-01 为第 1 期的月序
  const [y, m] = period.split("-").map(Number);
  return (y - 2023) * 12 + m; // 2023-01 -> 1，与 total_periods 同口径
}

async function interruptRun(runId) {
  await api("/api/analysis/interrupt", { method: "POST", body: { run_id: runId } });
  toast("已请求中断，运行将在下一个检查点停下", "success");
  setTimeout(renderRunsTab, 1200);
}
async function resumeRun(runId) {
  await api("/api/analysis/resume", { method: "POST", body: { run_id: runId } });
  toast("已从最后检查点恢复续算", "success");
  setTimeout(renderRunsTab, 1500);
}

async function demoInterruptResume() {
  // 用当前分析版创建一个新分析版做演练，避免影响基线
  const av = currentAv();
  const r = await api("/api/analysis/version", { method: "POST", body: {
    mapping_version_id: av.mapping_version_id,
    label: `演练版 ${new Date().toLocaleTimeString("zh-CN")}`,
    note: "中断-恢复演练，结果应与基线一致",
  } });
  const start = await api("/api/analysis/start", { method: "POST", body: { analysis_version_id: r.version_id } });
  toast(`演练运行 #${start.run_id} 已启动，2 秒后请求中断…`, "success");
  setTimeout(async () => {
    await api("/api/analysis/interrupt", { method: "POST", body: { run_id: start.run_id } });
    setTimeout(async () => {
      const run = (await api("/api/analysis/runs")).find((x) => x.id === start.run_id);
      if (run && run.status === "interrupted") {
        toast(`运行在检查点 ${run.last_checkpoint_period} 中断，正在恢复…`, "success");
        await api("/api/analysis/resume", { method: "POST", body: { run_id: start.run_id } });
        setTimeout(() => { renderRunsTab(); refreshMeta(); }, 2500);
      } else {
        renderRunsTab();
      }
    }, 1500);
  }, 2000);
}

// ============================================================ 公共
async function refreshMeta() {
  state.meta = await api("/api/meta");
  // 同步分析版下拉
  const sel = $("#global-av");
  const known = new Set(Array.from(sel.options).map((o) => o.value));
  for (const av of state.meta.analysis_versions) {
    if (!known.has(String(av.version_id))) {
      sel.appendChild(el("option", { value: av.version_id, text: av.label }));
    }
  }
}

async function refreshRunsQuietly() {
  if (!$("#tab-runs").classList.contains("active")) return;
  await renderRunsTab();
}

boot().catch((e) => toast(`初始化失败：${e.message}`, "error"));
