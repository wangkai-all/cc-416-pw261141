"""API 端到端测试：通过 HTTP 验证完整业务流程。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode())


def call_expect_error(path, body):
    try:
        call(path, body)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    raise AssertionError("预期报错但成功了")


# 1. 静态资源
with urllib.request.urlopen(BASE + "/") as r:
    assert r.status == 200 and "观测单位沿革分析台" in r.read().decode()
with urllib.request.urlopen(BASE + "/app.js") as r:
    assert r.status == 200

# 2. 元数据与基线分析版
_, meta = call("/api/meta")
assert len(meta["units"]) == 10
assert len(meta["mapping_versions"]) == 2
avs = meta["analysis_versions"]
assert len(avs) == 2, f"预期两个基线分析版，实际 {len(avs)}"
assert meta["quarantine_summary"], "预置隔离区不应为空"

# 3. 双口径序列 + 队列恒等
_, series = call(f"/api/analysis/series?av={avs[1]['version_id']}&kind=cohort&code=C2")
assert all(r["as_reported"] == r["restated"] for r in series), "队列两口径必须恒等"

# 4. 分解恒等式
_, decomp = call(f"/api/analysis/decomp?av={avs[1]['version_id']}&kind=unit&code=U07")
for d in decomp:
    if d["total_change"] is not None:
        assert d["total_change"] == d["structural_adjustment"] + d["organic_change"]

# 5. 异常追溯
_, trace = call(f"/api/analysis/trace?av={avs[1]['version_id']}&unit=U07&period=2025-09")
assert any("BULK" in (t["raw_note"] or "") for t in trace), "应追溯到大客户批量事件"

# 6. 隔离区：退役单位拦截放行
_, q = call("/api/quarantine")
retired = next(e for e in q if e["quarantine_reason"] == "retired_target")
code, payload = call_expect_error("/api/quarantine/release", {"event_id": retired["id"]})
assert code == 400 and "退役" in payload["error"]
# 重复批次可以人工放行（先拒绝演示，再验证拒绝生效）
dup = next(e for e in q if e["quarantine_reason"] == "duplicate")
_, rel = call("/api/quarantine/reject", {"event_id": dup["id"]})
assert rel["status"] == "rejected"

# 7. 新批次投递：退役单位/重复自动隔离
_, ing = call("/api/ingest", {
    "batch_id": "B-API-TEST-1", "source_name": "API测试",
    "items": [
        {"reported_code": "S04", "period": "2026-09", "parcels": 9000},
        {"reported_code": "S03", "period": "2026-09", "parcels": 1000},
    ],
})
assert ing["accepted"] == 1 and ing["quarantined"] == 1, ing

# 8. 沿革审阅 + 修订新版本 + 新分析版 + 重算
_, edges = call("/api/lineage/edges?vid=2")
# 基线已全部 approved；创建修订版并改权重
_, rev = call("/api/lineage/revise", {"label": "映射 v3（API测试）", "description": "测试修订链路"})
vid3 = rev["version_id"]
_, e3 = call(f"/api/lineage/edges?vid={vid3}")
target = next(e for e in e3 if e["from_unit"] == "U01" and e["to_unit"] == "U02")
call("/api/lineage/edge-weight", {"version_id": vid3, "edge_id": target["id"], "weight": 0.6})
# 互补边必须同步调整才能守恒，这里只验证链路；创建分析版
_, av3 = call("/api/analysis/version", {
    "mapping_version_id": vid3, "label": "分析版 C（API测试）", "note": "修订链路验证"})
_, started = call("/api/analysis/start", {"analysis_version_id": av3["version_id"]})
run_id = started["run_id"]

# 9. 中断 + 恢复
import time
time.sleep(0.5)
call("/api/analysis/interrupt", {"run_id": run_id})
time.sleep(1.2)
_, runs = call(f"/api/analysis/runs?av={av3['version_id']}")
run = next(r for r in runs if r["id"] == run_id)
assert run["status"] in ("interrupted", "completed"), run
if run["status"] == "interrupted":
    call("/api/analysis/resume", {"run_id": run_id})
    for _ in range(20):
        time.sleep(0.5)
        _, runs = call(f"/api/analysis/runs?av={av3['version_id']}")
        run = next(r for r in runs if r["id"] == run_id)
        if run["status"] == "completed":
            break
    assert run["status"] == "completed", f"恢复后未完成：{run['status']}"
assert run["last_checkpoint_period"] == "2026-10"

# 10. 审计日志
_, audit = call("/api/audit")
actions = {a["action"] for a in audit}
assert {"ingest_batch", "review_edge", "create_mapping_version", "complete_run"} <= actions

print("ALL API E2E CHECKS PASSED")
