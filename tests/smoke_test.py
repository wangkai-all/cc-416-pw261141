"""冒烟测试：验证分析管线核心不变量（自建临时库，无副作用）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import AnalysisService
from app.db import connect
from app.seed import generate
from app.util import period_range

DB = "/tmp/smoke_obs.db"
Path(DB).unlink(missing_ok=True)
generate(DB)

conn = connect(DB)
svc = AnalysisService(conn, db_path=DB)

# 两个分析版：v1 绑定错误映射 v1，v2 绑定修订映射 v2
# 注意：v2 新增的 U06->U07 边初始为 proposed（待审阅），先演示“未审阅即分析”
av1 = svc.create_analysis_version(1, "分析版 A（映射v1）", "初版")
av2 = svc.create_analysis_version(2, "分析版 B（映射v2修订，边待审阅）", "修订版")

r1 = svc.run(av1)
r2 = svc.run(av2)
assert r1["status"] == "completed" and r2["status"] == "completed"

# 1. 权重守恒：任一期间，全单位 restated 之和 == accepted 源事件总量
periods = period_range("2023-01", "2026-10")
# 结构守卫：结果必须真正落库（防止写入代码误入死作用域）
n_units = len(svc.lineage.list_units())
n_cohorts = len(svc.lineage.cohorts(1))
res_count = conn.execute(
    "SELECT COUNT(*) c FROM analysis_result WHERE analysis_version_id=?", (av1,)
).fetchone()["c"]
assert res_count == len(periods) * (n_units + n_cohorts), f"结果行数量异常：{res_count}"
# 每条已批准沿革边都应在生效期产生对称的进/出调整记录
n_edges = conn.execute(
    "SELECT COUNT(*) c FROM lineage_edge WHERE version_id=1 AND review_status='approved'"
).fetchone()["c"]
adj_rows = conn.execute(
    "SELECT COUNT(*) c FROM adjustment WHERE analysis_version_id=?"
    , (av1,)).fetchone()["c"]
assert adj_rows == n_edges * 2, f"调整明细应覆盖每条边的进出双向，实际 {adj_rows}/{n_edges * 2}"
for p in periods:
    total_src = conn.execute(
        "SELECT COALESCE(SUM(parcels),0) s FROM source_event WHERE period=? AND status='accepted'", (p,)
    ).fetchone()["s"]
    total_restated = conn.execute(
        "SELECT COALESCE(SUM(restated),0) s FROM analysis_result "
        "WHERE analysis_version_id=? AND scope_kind='unit' AND period=?", (av1, p)
    ).fetchone()["s"]
    assert total_src == total_restated, f"v1 守恒失败 {p}: {total_src} != {total_restated}"
    # 重述后的准时+延误也守恒（服务质量指标随重述分摊）
    ql = conn.execute(
        "SELECT COALESCE(SUM(on_time),0)+COALESCE(SUM(late),0) s FROM analysis_result "
        "WHERE analysis_version_id=? AND scope_kind='unit' AND period=?", (av1, p)
    ).fetchone()["s"]
    assert ql == total_src, f"重述准时/延误不守恒 {p}: {ql} != {total_src}"
    # 队列 restated 之和同样守恒
    total_cohort = conn.execute(
        "SELECT COALESCE(SUM(restated),0) s FROM analysis_result "
        "WHERE analysis_version_id=? AND scope_kind='cohort' AND period=?", (av1, p)
    ).fetchone()["s"]
    assert total_src == total_cohort, f"队列守恒失败 {p}"

# 2. 队列 as_reported == restated（队列不受拆合影响）
for av in (av1, av2):
    rows = conn.execute(
        "SELECT * FROM analysis_result WHERE analysis_version_id=? AND scope_kind='cohort'", (av,)
    ).fetchall()
    bad = [r for r in rows if r["as_reported"] != r["restated"]]
    assert not bad, f"v{av} 队列两口径不一致: {[dict(b) for b in bad[:3]]}"

# 3. 调整恒等式：单位层面 total_change = 调整净额 + 有机变化
decomp = svc.growth_decomposition(av2, "unit", "U07")
for d in decomp:
    if d["total_change"] is not None:
        assert d["total_change"] == d["structural_adjustment"] + d["organic_change"]

# 4. v1 与 v2 的差异：v2 修订边审阅前，两版 U07 历史重述相同（proposed 边不参与计算）
u07_v1 = {r["period"]: r["restated"] for r in svc.get_series(av1, "unit", "U07")}
u07_v2_pending = {r["period"]: r["restated"] for r in svc.get_series(av2, "unit", "U07")}
assert u07_v1 == u07_v2_pending, "待审阅边不得影响分析结果"

# 审阅通过 v2 的 U06->U07 边，作废旧运行后重算，生成真正的修订分析版
edge_v2 = conn.execute(
    "SELECT id FROM lineage_edge WHERE version_id=2 AND from_unit='U06' AND to_unit='U07'"
).fetchone()
svc.lineage.review_edge(edge_v2["id"], "approved", "沿革管理员", comment="核对西关撤站文件后补登")
new_run_id = svc.rerun(av2, "U06->U07 修订边审阅通过后重算")
svc._process(new_run_id)
u07_v2 = {r["period"]: r["restated"] for r in svc.get_series(av2, "unit", "U07")}
assert u07_v2["2025-06"] > u07_v1["2025-06"] > 0, "v2 修订后 U07 历史重述应高于 v1"
# v1 缺口 = U06 当期事件量 × 合并边权重（漏接边的直接后果）
u06_2506 = conn.execute(
    "SELECT parcels FROM source_event WHERE reported_code='S06' AND period='2025-06'"
).fetchone()["parcels"]
w = conn.execute(
    "SELECT weight FROM lineage_edge WHERE version_id=2 AND from_unit='U06' AND to_unit='U07'"
).fetchone()["weight"]
assert u07_v2["2025-06"] - u07_v1["2025-06"] == round(u06_2506 * w)
# 原报口径两版完全一致（修订映射不得重写历史上报）
u07_ar_v1 = {r["period"]: r["as_reported"] for r in svc.get_series(av1, "unit", "U07")}
u07_ar_v2 = {r["period"]: r["as_reported"] for r in svc.get_series(av2, "unit", "U07")}
assert u07_ar_v1 == u07_ar_v2, "修订映射不得改变当时口径"

# 5. 中断恢复：新建 v3 分析版，处理 10 个期间后中断，再恢复，结果与一次跑完一致
av3 = svc.create_analysis_version(2, "分析版 C（中断恢复验证）", "")
run_id = svc.start_run(av3)
svc.interrupt_after_periods(run_id, 10)
row = svc._process(run_id)
assert row["status"] == "interrupted", f"预期中断，实际 {row['status']}"
assert row["last_checkpoint_period"] == "2023-10", f"检查点错误：{row['last_checkpoint_period']}"
# 恢复续算
svc.clear_interrupt(run_id)
row2 = svc._process(run_id)
assert row2["status"] == "completed"
assert row2["last_checkpoint_period"] == "2026-10"
# 与 av2（同映射 v2、一次跑完）结果逐行一致
diff = conn.execute(
    "SELECT a.period, a.scope_code, a.restated ar, b.restated br FROM analysis_result a "
    "JOIN analysis_result b ON b.period=a.period AND b.scope_kind=a.scope_kind "
    "AND b.scope_code=a.scope_code AND b.analysis_version_id=? "
    "WHERE a.analysis_version_id=? AND a.restated<>b.restated LIMIT 5", (av2, av3)
).fetchall()
assert not diff, f"中断恢复后结果不一致: {[dict(d) for d in diff]}"

# 6. 异常追溯：2025-09 U07 的贡献应包含带 BULK 备注的 S07 事件
trace = svc.trace_contributors(av2, "U07", "2025-09")
assert any(t["raw_note"] and "BULK" in t["raw_note"] for t in trace), "未追溯到大客户批量事件"
assert sum(t["contribution"] for t in trace) == u07_v2["2025-09"]

# 7. 隔离区初始状态
q = conn.execute("SELECT COUNT(*) c FROM source_event WHERE status='quarantined'").fetchone()["c"]
assert q == 4, f"预期 4 条隔离事件，实际 {q}"

print("ALL SMOKE CHECKS PASSED")
print(f"期间数={len(periods)} 单位结果行/版={len(periods)*10} 队列={svc.lineage.cohorts(2)}")
conn.close()
