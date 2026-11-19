"""来源接收服务。

职责
----
1. 接收投递批次：逐条校验上报代码与有效期映射，把不合法事件隔离而不是静默丢弃。
2. 四类校验：
   - unknown_code    主数据里不存在的上报代码
   - retired_target  映射到已退役单位（持续新数据不得落入已退役单位）
   - duplicate       同上报代码+期间+件量的重复批次（dedup_key 唯一约束兜底）
   - outlier         件量偏离该单位历史区间过大（阈值规则，可人工放行）
3. 隔离区动作：release（解除隔离并接受）/ reject（拒绝），全程写审计。
4. 事件状态变化后通知分析侧需要重算（由 API 层串联）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import connect
from .lineage import LineageService
from .util import compare_period, now_iso

OUTLIER_RATIO = 5.0


@dataclass
class IngestItem:
    reported_code: str
    period: str
    parcels: int
    on_time: int | None = None
    late: int | None = None
    raw_note: str | None = None


class IngestionError(ValueError):
    pass


class IngestionService:
    def __init__(self, conn: sqlite3.Connection, mapping_version_id: int = 1):
        self.conn = conn
        self.mapping_version_id = mapping_version_id
        self.lineage = LineageService(conn)

    # ------------------------------------------------------------------ 查询

    def list_events(self, status: str | None = None, limit: int = 500) -> list[sqlite3.Row]:
        sql = ("SELECT e.*, me.canonical_unit FROM source_event e "
               "LEFT JOIN mapping_entry me ON me.version_id=? "
               "  AND me.reported_code=e.reported_code "
               "  AND e.period BETWEEN me.valid_from AND COALESCE(me.valid_to, '9999-12') "
               "WHERE 1=1")
        params: list = [self.mapping_version_id]
        if status:
            sql += " AND e.status=?"
            params.append(status)
        sql += " ORDER BY e.period DESC, e.reported_code LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def list_quarantine(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_event WHERE status='quarantined' ORDER BY received_at DESC"
        ).fetchall()

    def event_flags(self, event_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM event_flag WHERE event_id=? ORDER BY id", (event_id,)
        ).fetchall()

    # ------------------------------------------------------------------ 接收与校验

    def _check_duplicate(self, item: IngestItem) -> bool:
        key = f"{item.reported_code}|{item.period}|{item.parcels}"
        return self.conn.execute(
            "SELECT 1 FROM source_event WHERE dedup_key=? AND status IN ('accepted','quarantined')",
            (key,),
        ).fetchone() is not None

    def _check_outlier(self, item: IngestItem) -> bool:
        row = self.conn.execute(
            "SELECT MAX(parcels) AS mx, AVG(parcels) AS av FROM source_event "
            "WHERE reported_code=? AND status='accepted'",
            (item.reported_code,),
        ).fetchone()
        if row["mx"] is None:
            return False
        return item.parcels > row["mx"] * OUTLIER_RATIO

    def validate(self, item: IngestItem) -> str | None:
        """返回隔离原因；None 表示通过。"""
        unit = self.lineage.resolve_reported_code(
            self.mapping_version_id, item.reported_code, item.period
        )
        if unit is None:
            return "unknown_code"
        u = self.lineage.get_unit(unit)
        if u is not None and u["status"] == "retired":
            # 退役单位可能存在“在其有效期内的历史补报”，只拦截晚于退役期间的数据
            if u["last_period"] and compare_period(item.period, u["last_period"]) > 0:
                return "retired_target"
        if self._check_duplicate(item):
            return "duplicate"
        if self._check_outlier(item):
            return "outlier"
        return None

    def ingest_batch(self, batch_id: str, source_name: str, items: list[IngestItem],
                     note: str = "") -> dict:
        self.conn.execute(
            "INSERT INTO source_batch(batch_id,source_name,received_at,note) VALUES (?,?,?,?)",
            (batch_id, source_name, now_iso(), note),
        )
        accepted, quarantined = [], []
        for item in items:
            reason = self.validate(item)
            late = item.late if item.late is not None else round(item.parcels * 0.04)
            on_time = item.on_time if item.on_time is not None else item.parcels - late
            suffix = "|Q" if reason else ""
            key = f"{item.reported_code}|{item.period}|{item.parcels}{suffix}"
            cur = self.conn.execute(
                "INSERT INTO source_event(batch_id,reported_code,period,parcels,on_time,late,"
                "received_at,status,quarantine_reason,dedup_key,raw_note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, item.reported_code, item.period, item.parcels, on_time, late,
                 now_iso(), "quarantined" if reason else "accepted", reason, key, item.raw_note),
            )
            if reason:
                quarantined.append((cur.lastrowid, reason))
            else:
                accepted.append(cur.lastrowid)
        for eid, reason in quarantined:
            self.conn.execute(
                "INSERT INTO event_flag(event_id,flag_type,severity,detail,created_at) VALUES (?,?,?,?,?)",
                (eid, reason, "critical" if reason in ("retired_target", "unknown_code") else "warning",
                 f"接收校验未通过：{reason}", now_iso()),
            )
        self._audit("ingest_batch", "source_batch", batch_id,
                    f"批次 {batch_id}：接受 {len(accepted)} 条，隔离 {len(quarantined)} 条")
        self.conn.commit()
        return {"batch_id": batch_id, "accepted": len(accepted),
                "quarantined": len(quarantined),
                "quarantined_events": [eid for eid, _ in quarantined]}

    # ------------------------------------------------------------------ 隔离区动作

    def release_event(self, event_id: int, reviewer: str = "值班分析员") -> dict:
        """解除隔离：接受事件。退役/未知代码必须先在映射侧处理，这里拒绝放行。"""
        ev = self.conn.execute(
            "SELECT * FROM source_event WHERE id=?", (event_id,)
        ).fetchone()
        if ev is None or ev["status"] != "quarantined":
            raise IngestionError("事件不存在或不在隔离区")
        if ev["quarantine_reason"] in ("retired_target", "unknown_code"):
            raise IngestionError(
                f"{ev['quarantine_reason']} 不能直接放行：需先在单位沿革映射中处理"
                "（退役单位的新数据应改投接续单位，未知代码需补主数据）"
            )
        new_key = f"{ev['reported_code']}|{ev['period']}|{ev['parcels']}"
        clash = self.conn.execute(
            "SELECT 1 FROM source_event WHERE dedup_key=? AND id<>?", (new_key, event_id)
        ).fetchone()
        if clash:
            raise IngestionError("放行会产生重复事件，请确认后改用「拒绝重复批次」")
        self.conn.execute(
            "UPDATE source_event SET status='accepted', quarantine_reason=NULL, dedup_key=? WHERE id=?",
            (new_key, event_id),
        )
        self.conn.execute(
            "INSERT INTO event_flag(event_id,flag_type,severity,detail,created_at) VALUES (?,?,?,?,?)",
            (event_id, "released", "info", f"{reviewer} 人工解除隔离并接受", now_iso()),
        )
        self._audit("release_event", "source_event", str(event_id),
                    f"{reviewer} 放行隔离事件 {ev['reported_code']} {ev['period']}")
        self.conn.commit()
        return {"event_id": event_id, "status": "accepted"}

    def reject_event(self, event_id: int, reviewer: str = "值班分析员") -> dict:
        ev = self.conn.execute(
            "SELECT * FROM source_event WHERE id=?", (event_id,)
        ).fetchone()
        if ev is None or ev["status"] != "quarantined":
            raise IngestionError("事件不存在或不在隔离区")
        self.conn.execute("UPDATE source_event SET status='rejected' WHERE id=?", (event_id,))
        self.conn.execute(
            "INSERT INTO event_flag(event_id,flag_type,severity,detail,created_at) VALUES (?,?,?,?,?)",
            (event_id, "rejected", "info", f"{reviewer} 拒绝该事件", now_iso()),
        )
        self._audit("reject_event", "source_event", str(event_id),
                    f"{reviewer} 拒绝隔离事件 {ev['reported_code']} {ev['period']} "
                    f"（原因 {ev['quarantine_reason']}）")
        self.conn.commit()
        return {"event_id": event_id, "status": "rejected"}

    def _audit(self, action: str, entity: str, entity_id: str | None, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(action,entity,entity_id,detail,created_at) VALUES (?,?,?,?,?)",
            (action, entity, entity_id, detail, now_iso()),
        )


def get_ingestion_service(db_path: str, mapping_version_id: int = 1) -> IngestionService:
    return IngestionService(connect(db_path), mapping_version_id)
