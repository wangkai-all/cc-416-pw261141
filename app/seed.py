"""虚构观测数据生成：站点沿革、源事件批次、隔离项与异常标记。

场景（月度数据，2023-01 ~ 2026-10）
-----------------------------------
北片区 C1：
  U01 城北旧站 --2024-07 拆分 55%/45%--> U02 城北东片站 / U03 城北西片站
  U03 --2025-03 迁址更名 100%--> U04 滨河新区站（上报代码 S03 -> S04）
老城/南城 C2：
  U05 东关站 + U06 西关站 --2025-07 合并--> U07 老城枢纽中心站
  U09 南城临时站 --2025-01 拆分 100%--> U10 南城中心站 --2025-07 并入--> U07
临港 C3：
  U08 临港综合站（全程稳定，对照组）

映射版本：v1 漏接 U06->U07 边（历史映射错误），v2 修订补上。
"""
from __future__ import annotations

import random
from pathlib import Path

from .db import connect, init_db
from .util import now_iso, period_range

DATA_START = "2023-01"
DATA_END = "2026-10"

# code, 名称, 区域, 首期间, 末期间(None=在役)
UNITS = [
    ("U01", "城北旧站", "北片区", "2023-01", "2024-06"),
    ("U02", "城北东片站", "北片区", "2024-07", None),
    ("U03", "城北西片站", "北片区", "2023-01", "2025-02"),
    ("U04", "滨河新区站", "北片区", "2025-03", None),
    ("U05", "老城东关站", "老城区", "2023-01", "2025-06"),
    ("U06", "老城西关站", "老城区", "2023-01", "2025-06"),
    ("U07", "老城枢纽中心站", "老城区", "2025-07", None),
    ("U08", "临港综合站", "临港片区", "2023-01", None),
    ("U09", "南城临时站", "老城区", "2023-01", "2024-12"),
    ("U10", "南城中心站", "老城区", "2025-01", "2025-06"),
]

# 上报代码规格：代码, 规范单位, 有效期起, 有效期止(None=至今), 基准件量, 月增速
REPORTED_SPECS = [
    ("S01", "U01", "2023-01", "2024-06", 12000, 0.0035),
    ("S02", "U02", "2024-07", None, 7000, 0.0040),
    ("S03", "U03", "2023-01", "2025-02", 5000, 0.0030),   # 2024-07 起承接 U01 的 45%
    ("S04", "U04", "2025-03", None, 8400, 0.0035),
    ("S05", "U05", "2023-01", "2025-06", 9000, 0.0030),
    ("S06", "U06", "2023-01", "2025-06", 7000, 0.0025),
    ("S07", "U07", "2025-07", None, 23500, 0.0035),       # 含 U05+U06+U10 合并口径
    ("S08", "U08", "2023-01", None, 15000, 0.0045),
    ("S09", "U09", "2023-01", "2024-12", 6000, 0.0020),
    ("S10", "U10", "2025-01", "2025-06", 6300, 0.0025),
]

# 沿革边：(from, to, 生效期, 类型, 权重, v1是否存在, 说明)
EDGES = [
    ("U01", "U02", "2024-07", "split", 0.55, True, "城北旧站拆分：东片 55%"),
    ("U01", "U03", "2024-07", "split", 0.45, True, "城北旧站拆分：西片 45%"),
    ("U03", "U04", "2025-03", "relocation", 1.00, True, "城北西片站迁址滨河新区，整体更名"),
    ("U05", "U07", "2025-07", "merge", 0.50, True, "东关、西关合并组建枢纽中心（东关份额）"),
    ("U06", "U07", "2025-07", "merge", 0.50, False, "【v1 漏接】西关并入枢纽的映射边，v2 修订补上"),
    ("U09", "U10", "2025-01", "split", 1.00, True, "南城临时站撤销，业务整体转入南城中心站"),
    ("U10", "U07", "2025-07", "merge", 1.00, True, "南城中心站并入老城枢纽中心站"),
]

# 结构性吸收：某期间起基准件量阶跃（模拟承接来的业务，本身是单位调整而非增长）
BASE_STEPS = {
    ("S03", "2024-07"): 8000,   # 承接 U01 西片 45%
    ("S07", "2025-07"): 23500,  # U05+U06+U10 合并
}

# 真实业务异常（可追溯的源事件）：(上报代码, 期间, 额外件量, 说明)
KNOWN_EVENTS = [
    ("S07", "2025-09", 4200, "大客户双节促销批量件，合同编号 BULK-2025-091"),
    ("S08", "2024-03", -1800, "临港堆场改造，部分件量临时分流（有调度单 OPS-2024-03）"),
]

# 隔离区演示数据：(上报代码, 期间, 件量, 原因, 批次, 备注)
QUARANTINE_CASES = [
    ("S08", "2025-06", None, "duplicate", "B-DUP-01",
     "重复批次：与 B-2025-06 中 S08 同期间同件量，疑似重传"),
    ("S03", "2026-02", 8500, "retired_target", "B-FEED-NEW",
     "上报代码 S03 对应单位 U03 已于 2025-02 退役（迁址为 U04/S04），新数据不得落入退役单位"),
    ("S99", "2026-02", 3000, "unknown_code", "B-FEED-NEW",
     "主数据中不存在的上报代码 S99"),
    ("S02", "2026-01", 99999, "outlier", "B-FEED-NEW",
     "件量超出该单位历史区间 5 倍以上，疑似小数点/单位错误"),
]


def _seasonal(period: str) -> float:
    month = int(period.split("-")[1])
    return {2: 0.82, 11: 1.15, 12: 1.08, 6: 0.95, 1: 0.92}.get(month, 1.0)


def generate(db_path: str | Path) -> None:
    init_db(db_path)
    conn = connect(db_path)
    rng = random.Random(20261120)
    ts = now_iso()
    try:
        # ---- 规范单位
        conn.executemany(
            "INSERT INTO canonical_unit(code,name,region,first_period,last_period,status) "
            "VALUES (?,?,?,?,?,?)",
            [(c, n, r, fp, lp, "retired" if lp else "active") for c, n, r, fp, lp in UNITS],
        )

        # ---- 映射版本 v1 / v2
        conn.execute(
            "INSERT INTO mapping_version(version_id,label,description,created_at,supersedes) "
            "VALUES (1,'映射 v1（初版）','初版沿革映射，漏接 U06西关站->U07枢纽 的合并边',?,NULL)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO mapping_version(version_id,label,description,created_at,supersedes) "
            "VALUES (2,'映射 v2（修订版）','修订 v1：补登 U06->U07 合并边，西关历史件量正确归入枢纽队列',?,1)",
            (ts,),
        )

        # ---- 沿革边（v1 仅含 in_v1 的边；v2 全部）
        edge_id = 0
        for f, t, eff, etype, weight, in_v1, note in EDGES:
            for vid in (1, 2):
                if vid == 1 and not in_v1:
                    continue
                edge_id += 1
                # v1 边已审阅通过；v2 新增边处于待审阅
                status = "proposed" if (vid == 2 and not in_v1) else "approved"
                cur = conn.execute(
                    "INSERT INTO lineage_edge(version_id,from_unit,to_unit,effective_period,"
                    "edge_type,weight,review_status,note) VALUES (?,?,?,?,?,?,?,?)",
                    (vid, f, t, eff, etype, weight, status, note),
                )
                if status == "approved":
                    conn.execute(
                        "INSERT INTO edge_review(edge_id,action,old_weight,new_weight,reviewer,"
                        "reviewed_at,comment) VALUES (?, 'approved', NULL, ?, '沿革管理员', ?, ?)",
                        (cur.lastrowid, weight, ts, "建版时批量审阅通过"),
                    )

        # ---- 上报代码有效期映射（两版相同）
        for vid in (1, 2):
            for code, unit, vf, vt, _b, _g in REPORTED_SPECS:
                conn.execute(
                    "INSERT INTO mapping_entry(version_id,reported_code,canonical_unit,valid_from,valid_to)"
                    " VALUES (?,?,?,?,?)",
                    (vid, code, unit, vf, vt),
                )

        # ---- 源事件（accepted）
        conn.execute(
            "INSERT INTO source_batch(batch_id,source_name,received_at,note) VALUES (?,?,?,?)",
            ("B-SEED", "观测数据交换平台（虚构）", ts, "历史月度件量回填批次"),
        )
        spike_lookup = {(c, p): (extra, note) for c, p, extra, note in KNOWN_EVENTS}
        for code, unit, vf, vt, base, growth in REPORTED_SPECS:
            end = vt or DATA_END
            for i, period in enumerate(period_range(vf, end)):
                cur_base = base
                for (sc, sp), stepped in BASE_STEPS.items():
                    if sc == code and period >= sp:
                        cur_base = stepped
                trend = cur_base * ((1 + growth) ** i)
                parcels = round(trend * _seasonal(period) + rng.uniform(-180, 180))
                if (code, period) in spike_lookup:
                    extra, note = spike_lookup[(code, period)]
                    parcels = max(100, parcels + extra)
                else:
                    note = None
                parcels = max(100, parcels)
                late = round(parcels * rng.uniform(0.03, 0.06))
                on_time = parcels - late
                conn.execute(
                    "INSERT INTO source_event(batch_id,reported_code,period,parcels,on_time,late,"
                    "received_at,status,dedup_key,raw_note) VALUES (?,?,?,?,?,?,?, 'accepted', ?,?)",
                    ("B-SEED", code, period, parcels, on_time, late, ts,
                     f"{code}|{period}|{parcels}", note),
                )
                if note:
                    eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        "INSERT INTO event_flag(event_id,flag_type,severity,detail,created_at)"
                        " VALUES (?, 'spike', 'info', ?, ?)",
                        (eid, note, ts),
                    )

        # ---- 隔离区
        for code, period, parcels, reason, batch, note in QUARANTINE_CASES:
            exists_batch = conn.execute(
                "SELECT 1 FROM source_batch WHERE batch_id=?", (batch,)
            ).fetchone()
            if not exists_batch:
                conn.execute(
                    "INSERT INTO source_batch(batch_id,source_name,received_at,note) VALUES (?,?,?,?)",
                    (batch, "新到投递批次（虚构）", ts, "待来源接收服务校验"),
                )
            if parcels is None:  # 重复批次：复制 S08 当月已接受事件的件量
                row = conn.execute(
                    "SELECT parcels,on_time,late FROM source_event WHERE reported_code='S08' "
                    "AND period=? AND status='accepted'", (period,),
                ).fetchone()
                parcels, on_time, late = row["parcels"], row["on_time"], row["late"]
            else:
                late = round(parcels * 0.04)
                on_time = parcels - late
            conn.execute(
                "INSERT INTO source_event(batch_id,reported_code,period,parcels,on_time,late,"
                "received_at,status,quarantine_reason,dedup_key,raw_note) "
                "VALUES (?,?,?,?,?,?,?, 'quarantined', ?, ?,?)",
                (batch, code, period, parcels, on_time, late, ts, reason,
                 f"{code}|{period}|{parcels}|Q", note),
            )
            eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            sev = "critical" if reason in ("retired_target", "unknown_code") else "warning"
            conn.execute(
                "INSERT INTO event_flag(event_id,flag_type,severity,detail,created_at) VALUES (?,?,?,?,?)",
                (eid, reason, sev, note, ts),
            )

        conn.execute(
            "INSERT INTO audit_log(action,entity,entity_id,detail,created_at) VALUES (?,?,?,?,?)",
            ("seed", "database", None, "生成虚构观测网络主数据与历史源事件", ts),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/observatory.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    generate(target)
    print(f"seed data written to {target}")
