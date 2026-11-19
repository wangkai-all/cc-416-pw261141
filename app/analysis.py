"""队列分析服务。

核心口径
========
对每个分析版（绑定某一映射版本）按月度期间计算：

* as_reported（当时口径/直接重述历史）
  只汇总该期间实际上报、且经有效期映射落在本单位的源事件。站点拆分后，新单位
  在拆分生效前没有当时口径数据 —— 它当时还不存在。

* restated（最新口径/按当前单位边界重述）
  历史事件沿已批准沿革边的全部下游路径按累乘权重分摊到“今天的”单位/队列。
  队列是沿革连通分量，权重守恒，因此队列总量不受任何拆合迁址影响。

* 单位调整 vs 有机增长
  沿革边生效期间，承接单位计入 inbound 调整量（来源事件经路径分摊而来），
  移交单位计入 outbound。调整量解释环比变化中的“单位边界变化”部分，
  剩余部分才是 organic（真实业务增长/下滑）。对任意实体恒有：
      环比总变化 = 结构调整净额 + 有机变化
  队列层面结构调整净额恒为 0。

重算与恢复
==========
每个 run 逐期间推进并写 analysis_run.last_checkpoint_period；
中断/失败后对同一 run 调用 resume 即从下一期间续算，最终结果与一次跑完完全一致。
"""
from __future__ import annotations

import sqlite3
import threading
import typing
from collections import defaultdict
from pathlib import Path

from .db import connect
from .lineage import LineageService
from .util import compare_period, now_iso, period_range


def _allocate(total: int, weights: list[float]) -> list[int]:
    """最大余数法把整数 total 按 weights 拆成整数份额，份额之和严格等于 total。"""
    if not weights:
        return []
    raw = [total * w for w in weights]
    floors = [int(x) for x in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors


class AnalysisError(RuntimeError):
    pass


class AnalysisService:
    # 进程级中断开关：run_id -> True。线程内轮询，模拟“中断重算后恢复”。
    _interrupt_flags: dict[int, bool] = {}
    # 测试钩子：run_id -> 已处理期间数达到 N 时自动请求中断
    _interrupt_after: dict[int, int] = {}
    _lock = threading.Lock()

    def __init__(self, conn: sqlite3.Connection, db_path: str | Path | None = None):
        self.conn = conn
        self._db_path = str(db_path) if db_path else None
        self.lineage = LineageService(conn)

    # ------------------------------------------------------------------ 版本管理

    def list_analysis_versions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT av.*, mv.label AS mapping_label, mv.version_id AS mapping_version_id "
            "FROM analysis_version av JOIN mapping_version mv ON mv.version_id=av.mapping_version_id "
            "ORDER BY av.version_id"
        ).fetchall()

    def create_analysis_version(self, mapping_version_id: int, label: str, note: str = "") -> int:
        mv = self.conn.execute(
            "SELECT * FROM mapping_version WHERE version_id=?", (mapping_version_id,)
        ).fetchone()
        if mv is None:
            raise AnalysisError(f"映射版本 v{mapping_version_id} 不存在")
        row = self.conn.execute(
            "SELECT MAX(version_id) AS v FROM analysis_version"
        ).fetchone()
        vid = (row["v"] or 0) + 1
        self.conn.execute(
            "INSERT INTO analysis_version(version_id,label,mapping_version_id,created_at,note)"
            " VALUES (?,?,?,?,?)",
            (vid, label, mapping_version_id, now_iso(), note),
        )
        self._audit("create_analysis_version", "analysis_version", str(vid),
                    f"创建分析版 {label}，绑定映射 v{mapping_version_id}")
        self.conn.commit()
        return vid

    def list_runs(self, analysis_version_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT r.*, av.label AS analysis_label FROM analysis_run r "
               "JOIN analysis_version av ON av.version_id=r.analysis_version_id WHERE 1=1")
        params: list = []
        if analysis_version_id is not None:
            sql += " AND r.analysis_version_id=?"
            params.append(analysis_version_id)
        sql += " ORDER BY r.id DESC"
        return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ 运行控制

    def request_interrupt(self, run_id: int) -> None:
        with self._lock:
            self._interrupt_flags[run_id] = True

    @classmethod
    def _is_interrupted(cls, run_id: int) -> bool:
        with cls._lock:
            return cls._interrupt_flags.get(run_id, False)

    @classmethod
    def clear_interrupt(cls, run_id: int) -> None:
        with cls._lock:
            cls._interrupt_flags.pop(run_id, None)
            cls._interrupt_after.pop(run_id, None)

    @classmethod
    def interrupt_after_periods(cls, run_id: int, n: int) -> None:
        """测试/演练钩子：处理满 n 个期间后自动中断。"""
        with cls._lock:
            cls._interrupt_after[run_id] = n

    def start_run(self, analysis_version_id: int) -> int:
        av = self.conn.execute(
            "SELECT * FROM analysis_version WHERE version_id=?", (analysis_version_id,)
        ).fetchone()
        if av is None:
            raise AnalysisError("分析版不存在")
        existing = self.conn.execute(
            "SELECT id, status FROM analysis_run WHERE analysis_version_id=? "
            "AND status IN ('running','interrupted','failed') ORDER BY id",
            (analysis_version_id,),
        ).fetchall()
        if existing:
            raise AnalysisError(
                "该分析版已有未完成的运行（id=%s，状态=%s），请先恢复或作废"
                % (existing[0]["id"], existing[0]["status"])
            )
        cur = self.conn.execute(
            "INSERT INTO analysis_run(analysis_version_id,started_at,status,total_periods)"
            " VALUES (?,?, 'running', ?)",
            (analysis_version_id, now_iso(),
             len(self._all_periods())),
        )
        self.conn.commit()
        return cur.lastrowid

    def resume_run(self, run_id: int) -> sqlite3.Row:
        run = self.conn.execute("SELECT * FROM analysis_run WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise AnalysisError("运行不存在")
        if run["status"] not in ("interrupted", "failed"):
            raise AnalysisError(f"运行状态为 {run['status']}，无需恢复")
        self.clear_interrupt(run_id)
        self.conn.execute(
            "UPDATE analysis_run SET status='running', fail_reason=NULL WHERE id=?", (run_id,)
        )
        self.conn.commit()
        return self._process(run_id)

    def abort_run(self, run_id: int, reason: str = "人工作废") -> None:
        run = self.conn.execute("SELECT * FROM analysis_run WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise AnalysisError("运行不存在")
        if run["status"] not in ("interrupted", "failed", "running"):
            raise AnalysisError(f"运行状态为 {run['status']}，不能作废")
        self.clear_interrupt(run_id)
        self.conn.execute(
            "UPDATE analysis_run SET status='failed', fail_reason=? WHERE id=?", (reason, run_id)
        )
        self._audit("abort_run", "analysis_run", str(run_id), f"作废运行：{reason}")
        self.conn.commit()

    def rerun(self, analysis_version_id: int, reason: str = "映射修订后重算") -> int:
        """映射/数据修订后的重算入口：作废旧运行（保留审计痕迹）并启动新运行。"""
        old = self.conn.execute(
            "SELECT id FROM analysis_run WHERE analysis_version_id=? ORDER BY id DESC LIMIT 1",
            (analysis_version_id,),
        ).fetchone()
        if old and self.conn.execute(
            "SELECT status FROM analysis_run WHERE id=?", (old["id"],)
        ).fetchone()["status"] in ("interrupted", "failed", "running"):
            raise AnalysisError("存在未完成运行，请先恢复或作废")
        if old:
            self.conn.execute(
                "UPDATE analysis_run SET status='superseded', fail_reason=? WHERE id=?",
                (reason, old["id"]),
            )
            self._audit("supersede_run", "analysis_run", str(old["id"]),
                        f"重算取代旧运行：{reason}")
            self.conn.commit()
        return self.start_run(analysis_version_id)

    def _all_periods(self) -> list[str]:
        row = self.conn.execute(
            "SELECT MIN(period) AS lo, MAX(period) AS hi FROM source_event WHERE status='accepted'"
        ).fetchone()
        return period_range(row["lo"], row["hi"])

    def _checkpoint(self, run_id: int, period: str) -> None:
        self.conn.execute(
            "UPDATE analysis_run SET last_checkpoint_period=? WHERE id=?", (period, run_id)
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 主计算

    def run(self, analysis_version_id: int) -> sqlite3.Row:
        """新建运行并在当前线程同步跑完（供脚本调用）。"""
        run_id = self.start_run(analysis_version_id)
        return self._process(run_id)

    def start_background(self, analysis_version_id: int,
                         on_done: typing.Callable[[sqlite3.Row], None] | None = None) -> int:
        """后台线程运行；每期间落检查点，可用 request_interrupt 请求中断。"""
        run_id = self.start_run(analysis_version_id)

        def worker() -> None:
            conn = connect(self._db_path)
            svc = AnalysisService(conn)
            try:
                row = svc._process(run_id)
                if on_done:
                    on_done(row)
            finally:
                conn.close()

        t = threading.Thread(target=worker, name=f"analysis-run-{run_id}", daemon=True)
        t.start()
        return run_id

    def resume_background(self, run_id: int) -> None:
        def worker() -> None:
            conn = connect(self._db_path)
            svc = AnalysisService(conn)
            try:
                svc._process(run_id)
            finally:
                conn.close()

        self.clear_interrupt(run_id)
        self.conn.execute(
            "UPDATE analysis_run SET status='running', fail_reason=NULL WHERE id=?", (run_id,)
        )
        self.conn.commit()
        threading.Thread(target=worker, name=f"analysis-resume-{run_id}", daemon=True).start()

    def _process(self, run_id: int) -> sqlite3.Row:
        run = self.conn.execute("SELECT * FROM analysis_run WHERE id=?", (run_id,)).fetchone()
        av_id = run["analysis_version_id"]
        av = self.conn.execute("SELECT * FROM analysis_version WHERE version_id=?", (av_id,)).fetchone()
        mapping_vid = av["mapping_version_id"]
        all_periods = self._all_periods()
        start_after = run["last_checkpoint_period"]
        todo = [p for p in all_periods
                if start_after is None or compare_period(p, start_after) > 0]

        units = [u["code"] for u in self.lineage.list_units()]
        cohorts = self.lineage.cohorts(mapping_vid)
        unit_to_cohort = {u: cid for cid, members in cohorts.items() for u in members}

        # 首次运行时快照队列成员关系
        if start_after is None:
            self.conn.execute("DELETE FROM cohort_membership WHERE analysis_version_id=?", (av_id,))
            self.conn.executemany(
                "INSERT INTO cohort_membership(analysis_version_id,cohort_id,unit_code) VALUES (?,?,?)",
                [(av_id, cid, u) for cid, members in cohorts.items() for u in members],
            )
            # 清掉历史结果（幂等重算）
            self.conn.execute("DELETE FROM analysis_result WHERE analysis_version_id=?", (av_id,))
            self.conn.execute("DELETE FROM adjustment WHERE analysis_version_id=?", (av_id,))
            self.conn.commit()

        # 预取已批准边（调整分解用）
        edges = self.lineage._approved_edges(mapping_vid)
        edge_by_id = {e["id"]: e for e in edges}

        for period in todo:
            if self._is_interrupted(run_id):
                self.conn.execute(
                    "UPDATE analysis_run SET status='interrupted' WHERE id=?", (run_id,)
                )
                self._audit("interrupt_run", "analysis_run", str(run_id),
                            f"运行在检查点 {run['last_checkpoint_period']} 后中断")
                self.conn.commit()
                return self.conn.execute("SELECT * FROM analysis_run WHERE id=?", (run_id,)).fetchone()

            self._process_period(run_id, av_id, mapping_vid, period, units,
                                 unit_to_cohort, edge_by_id)
            self._checkpoint(run_id, period)
            with self._lock:
                after = self._interrupt_after.get(run_id)
            if after is not None:
                done = len(period_range(all_periods[0], period))
                if done >= after:
                    self.request_interrupt(run_id)

        self.conn.execute(
            "UPDATE analysis_run SET status='completed', finished_at=? WHERE id=?",
            (now_iso(), run_id),
        )
        self._audit("complete_run", "analysis_run", str(run_id),
                    f"分析版 v{av_id} 运行完成，共 {len(all_periods)} 个期间")
        self.conn.commit()
        return self.conn.execute("SELECT * FROM analysis_run WHERE id=?", (run_id,)).fetchone()

    def _period_events(self, period: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM source_event WHERE period=? AND status='accepted' ORDER BY id",
            (period,),
        ).fetchall()

    def _process_period(self, run_id: int, av_id: int, mapping_vid: int, period: str,
                        units: list[str], unit_to_cohort: dict[str, str],
                        edge_by_id: dict[int, sqlite3.Row]) -> None:
        # 1. 解析当期事件 -> 来源规范单位
        events = self._period_events(period)
        reported: dict[str, dict[str, int]] = defaultdict(lambda: {"parcels": 0, "on_time": 0, "late": 0})
        for ev in events:
            unit = self.lineage.resolve_reported_code(mapping_vid, ev["reported_code"], period)
            if unit is None:
                continue  # accepted 事件理论上都能解析；防御性跳过
            bucket = reported[unit]
            bucket["parcels"] += ev["parcels"]
            bucket["on_time"] += ev["on_time"]
            bucket["late"] += ev["late"]

        # 2. 重述：沿下游路径分摊（最新边界口径）
        restated: dict[str, float] = defaultdict(float)
        restated_ot: dict[str, float] = defaultdict(float)
        restated_late: dict[str, float] = defaultdict(float)

        for src_unit, bucket in reported.items():
            paths = self.lineage.downstream_paths(mapping_vid, src_unit)
            weights = [w for _, w, _ in paths]
            shares_p = _allocate(bucket["parcels"], weights)
            shares_ot = _allocate(bucket["on_time"], weights)
            shares_late = _allocate(bucket["late"], weights)
            for (target, _w, _path), sp, so, sl in zip(paths, shares_p, shares_ot, shares_late):
                restated[target] += sp
                restated_ot[target] += so
                restated_late[target] += sl

        # 2b. 结构调整：单位边界变化是一次性事件，只在边的生效当期记录。
        #     源单位在生效当期通常已停止上报（旧代码随拆分/迁址停用），调整基准取
        #     “该边源单位最后一个在役期间的重述承接量 × 边权重”——即从旧边界的最后
        #     一个稳态口径搬到新单位的件量，与环比分解口径匹配，不混入全部历史存量。
        #     多跳场景中间节点的 inbound 与后续 outbound 自然抵消，队列层面净额恒为 0。
        adj: dict[tuple[str, int, str], int] = defaultdict(int)
        for edge in edge_by_id.values():
            if edge["effective_period"] != period:
                continue
            base_period = self._last_active_period(mapping_vid, edge["from_unit"], period)
            if base_period is None:
                continue
            # 基准取源单位最后在役期的“当时口径”件量：这是旧边界下真实挂在该单位
            # 名下、需要按边权重移交给新单位的量（重述到自身会因全量转出而为 0）。
            base = self._as_reported_in_period(mapping_vid, edge["from_unit"], base_period)
            if base <= 0:
                continue
            flow = _allocate(base, [edge["weight"]])[0]
            if flow:
                adj[(edge["from_unit"], edge["id"], "outbound")] += flow
                adj[(edge["to_unit"], edge["id"], "inbound")] += flow

        # 3. 单位结果：全单位写行（as_reported 可能为 0 —— 当时尚不存在/已移交）。
        #    on_time/late 随重述口径分摊，描述“按当前边界重述后”的服务质量。
        for unit in units:
            rep = reported.get(unit, {"parcels": 0, "on_time": 0, "late": 0})
            self.conn.execute(
                "INSERT INTO analysis_result(run_id,analysis_version_id,scope_kind,scope_code,"
                "period,as_reported,restated,on_time,late) VALUES (?,?, 'unit', ?,?,?,?,?,?)",
                (run_id, av_id, unit, period,
                 rep["parcels"], round(restated.get(unit, 0.0)),
                 round(restated_ot.get(unit, 0.0)), round(restated_late.get(unit, 0.0))),
            )

        # 4. 队列结果：as_reported 为成员当期实际上报之和；restated 为成员重述之和
        cohort_members: dict[str, list[str]] = defaultdict(list)
        for u, cid in unit_to_cohort.items():
            cohort_members[cid].append(u)
        for cid, members in cohort_members.items():
            ar = sum(reported.get(u, {"parcels": 0})["parcels"] for u in members)
            rs = sum(restated.get(u, 0.0) for u in members)
            ot = sum(restated_ot.get(u, 0.0) for u in members)
            lt = sum(restated_late.get(u, 0.0) for u in members)
            self.conn.execute(
                "INSERT INTO analysis_result(run_id,analysis_version_id,scope_kind,scope_code,"
                "period,as_reported,restated,on_time,late) VALUES (?,?, 'cohort', ?,?,?,?,?,?)",
                (run_id, av_id, cid, period, ar, round(rs), round(ot), round(lt)),
            )

        # 5. 调整明细落库
        for (unit, edge_id, direction), parcels in adj.items():
            if edge_id not in edge_by_id or parcels == 0:
                continue
            self.conn.execute(
                "INSERT INTO adjustment(analysis_version_id,unit_code,period,edge_id,"
                "direction,parcels) VALUES (?,?,?,?,?,?)",
                (av_id, unit, period, edge_id, direction, parcels),
            )

    def _last_active_period(self, version_id: int, unit: str, before_period: str) -> str | None:
        """单位在 before_period（不含）之前最后一个有已接受事件的期间。"""
        row = self.conn.execute(
            "SELECT MAX(se.period) AS p FROM source_event se "
            "JOIN mapping_entry me ON me.reported_code = se.reported_code "
            "  AND me.version_id = ? AND me.canonical_unit = ? "
            "  AND se.period BETWEEN me.valid_from AND COALESCE(me.valid_to, '9999-12') "
            "WHERE se.status='accepted' AND se.period < ?",
            (version_id, unit, before_period),
        ).fetchone()
        return row["p"] if row and row["p"] else None

    def _as_reported_in_period(self, version_id: int, unit: str, period: str) -> int:
        """某单位在某期的当时口径件量：当期实际上报且经有效期映射落在本单位的事件量。"""
        total = 0
        for ev in self._period_events(period):
            if self.lineage.resolve_reported_code(version_id, ev["reported_code"], period) == unit:
                total += ev["parcels"]
        return total

    # ------------------------------------------------------------------ 结果查询

    def get_series(self, analysis_version_id: int, scope_kind: str, scope_code: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM analysis_result WHERE analysis_version_id=? AND scope_kind=? AND scope_code=? "
            "ORDER BY period",
            (analysis_version_id, scope_kind, scope_code),
        ).fetchall()

    def get_adjustments(self, analysis_version_id: int, unit_code: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT a.*, e.from_unit, e.to_unit, e.edge_type, e.effective_period, e.note "
               "FROM adjustment a JOIN lineage_edge e ON e.id=a.edge_id "
               "WHERE a.analysis_version_id=?")
        params: list = [analysis_version_id]
        if unit_code:
            sql += " AND a.unit_code=?"
            params.append(unit_code)
        sql += " ORDER BY a.period, a.id"
        return self.conn.execute(sql, params).fetchall()

    def growth_decomposition(self, analysis_version_id: int, scope_kind: str,
                             scope_code: str) -> list[dict]:
        """环比变化分解：总变化 = 结构调整净额 + 有机变化（基于当时口径）。"""
        rows = self.get_series(analysis_version_id, scope_kind, scope_code)
        out: list[dict] = []
        prev: sqlite3.Row | None = None
        for r in rows:
            adjustment_net = 0
            if scope_kind == "unit":
                adj_rows = self.conn.execute(
                    "SELECT direction, SUM(parcels) AS s FROM adjustment "
                    "WHERE analysis_version_id=? AND unit_code=? AND period=? GROUP BY direction",
                    (analysis_version_id, scope_code, r["period"]),
                ).fetchall()
                d = {x["direction"]: x["s"] for x in adj_rows}
                adjustment_net = d.get("inbound", 0) - d.get("outbound", 0)
            total_change = None if prev is None else r["as_reported"] - prev["as_reported"]
            organic = None if total_change is None else total_change - adjustment_net
            out.append({
                "period": r["period"],
                "as_reported": r["as_reported"],
                "restated": r["restated"],
                "total_change": total_change,
                "structural_adjustment": adjustment_net,
                "organic_change": organic,
            })
            prev = r
        return out

    def trace_contributors(self, analysis_version_id: int, unit_code: str,
                           period: str) -> list[dict]:
        """异常贡献追溯：某单位某期间重述量由哪些源事件、沿什么路径贡献而来。"""
        av = self.conn.execute(
            "SELECT * FROM analysis_version WHERE version_id=?", (analysis_version_id,)
        ).fetchone()
        result = []
        events = self._period_events(period)
        for ev in events:
            src = self.lineage.resolve_reported_code(
                av["mapping_version_id"], ev["reported_code"], period
            )
            if src is None:
                continue
            for target, weight, path in self.lineage.downstream_paths(
                av["mapping_version_id"], src
            ):
                if target == unit_code and weight > 0:
                    result.append({
                        "source_event_id": ev["id"],
                        "reported_code": ev["reported_code"],
                        "source_unit": src,
                        "path": path,
                        "weight": round(weight, 4),
                        "parcels": ev["parcels"],
                        "contribution": round(ev["parcels"] * weight),
                        "raw_note": ev["raw_note"],
                        "batch_id": ev["batch_id"],
                    })
        result.sort(key=lambda x: -x["contribution"])
        return result

    def _audit(self, action: str, entity: str, entity_id: str | None, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(action,entity,entity_id,detail,created_at) VALUES (?,?,?,?,?)",
            (action, entity, entity_id, detail, now_iso()),
        )


def run_analysis(db_path: str, analysis_version_id: int) -> int:
    """脚本入口：同步跑完一个分析版，返回 run_id。"""
    conn = connect(db_path)
    try:
        svc = AnalysisService(conn, db_path=db_path)
        run_id = svc.start_run(analysis_version_id)
        svc._process(run_id)
        return run_id
    finally:
        conn.close()
