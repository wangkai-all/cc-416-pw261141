"""单位沿革映射服务。

职责
----
1. 上报代码 -> 规范单位的「有效期」解析：同一统计名称在不同期间可指向不同单位。
2. 拆/合/迁址沿革边的版本化管理与审阅（proposed -> approved/rejected，权重修订）。
3. 沿革图遍历：给出某规范单位事件沿「截至某版本」已批准边的全部下游路径及权重，
   供队列分析服务做重述。
4. 队列 = 沿革图（仅已批准边）的连通分量。
5. 修订映射 -> 生成新的 mapping_version（旧版只读保留）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from .db import connect
from .util import compare_period, now_iso


class LineageError(ValueError):
    pass


class LineageService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ 查询

    def list_units(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM canonical_unit ORDER BY code"
        ).fetchall()

    def get_unit(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM canonical_unit WHERE code=?", (code,)
        ).fetchone()

    def list_versions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mapping_version ORDER BY version_id"
        ).fetchall()

    def list_edges(self, version_id: int, include_unreviewed: bool = True) -> list[sqlite3.Row]:
        sql = ("SELECT e.*, fu.name AS from_name, tu.name AS to_name FROM lineage_edge e "
               "JOIN canonical_unit fu ON fu.code=e.from_unit "
               "JOIN canonical_unit tu ON tu.code=e.to_unit "
               "WHERE e.version_id=?")
        params: list = [version_id]
        if not include_unreviewed:
            sql += " AND e.review_status='approved'"
        sql += " ORDER BY e.effective_period, e.id"
        return self.conn.execute(sql, params).fetchall()

    def list_entries(self, version_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM mapping_entry WHERE version_id=? ORDER BY reported_code, valid_from",
            (version_id,),
        ).fetchall()

    def resolve_reported_code(self, version_id: int, reported_code: str, period: str) -> str | None:
        """按有效期解析上报代码在某期间对应的规范单位。"""
        rows = self.conn.execute(
            "SELECT canonical_unit, valid_from, valid_to FROM mapping_entry "
            "WHERE version_id=? AND reported_code=? ORDER BY valid_from",
            (version_id, reported_code),
        ).fetchall()
        for r in rows:
            if compare_period(period, r["valid_from"]) >= 0 and (
                r["valid_to"] is None or compare_period(period, r["valid_to"]) <= 0
            ):
                return r["canonical_unit"]
        return None

    def _approved_edges(self, version_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM lineage_edge WHERE version_id=? AND review_status='approved'",
            (version_id,),
        ).fetchall()

    def downstream_paths(self, version_id: int, unit: str,
                         as_of_period: str | None = None) -> list[tuple[str, float, list[str]]]:
        """从 unit 出发的全部下游路径。

        返回 [(终点单位, 路径累乘权重, 路径上的单位序列), ...]。
        as_of_period 给定时，只走 effective_period <= as_of 的边（点在时口径）；
        默认走全部已批准边（最新边界口径，队列分析重述使用）。
        """
        edges = self._approved_edges(version_id)
        adj: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for e in edges:
            if as_of_period is None or compare_period(e["effective_period"], as_of_period) <= 0:
                adj[e["from_unit"]].append(e)

        results: list[tuple[str, float, list[str]]] = []
        # DFS 携带累乘权重；沿革图为 DAG（单位只在退役后才有出边），无需 visited 防环，
        # 但仍加路径集合做防御。
        stack: list[tuple[str, float, list[str]]] = [(unit, 1.0, [unit])]
        while stack:
            node, weight, path = stack.pop()
            outgoing = adj.get(node, [])
            outgoing_sum = sum(e["weight"] for e in outgoing)
            # 无出边：全部份额停留；有出边但权重和 <1（映射不完整，如 v1 漏接边）：
            # 未覆盖份额停留在本节点。两种情况都保证权重守恒，缺口在队列审阅页可见。
            if not outgoing or outgoing_sum < 1.0:
                stay = weight if not outgoing else weight * (1.0 - outgoing_sum)
                if stay > 0:
                    results.append((node, stay, path))
            for e in outgoing:
                if e["to_unit"] in path:
                    raise LineageError(f"沿革图存在环：{' -> '.join(path + [e['to_unit']])}")
                stack.append((e["to_unit"], weight * e["weight"], path + [e["to_unit"]]))
        # 同一终点可能有多条路径（先拆后合），权重相加
        merged: dict[str, tuple[float, list[str]]] = {}
        for target, w, path in results:
            if target in merged:
                merged[target] = (merged[target][0] + w, merged[target][1])
            else:
                merged[target] = (w, path)
        return [(t, w, p) for t, (w, p) in sorted(merged.items())]

    def cohorts(self, version_id: int) -> dict[str, list[str]]:
        """沿革连通分量 -> {队列 ID: [单位代码...]}，队列按成员最小编号稳定命名。"""
        edges = self._approved_edges(version_id)
        parent: dict[str, str] = {u["code"]: u["code"] for u in self.list_units()}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                # 以最小代码为根，保证队列编号在版本间稳定
                root, child = (ra, rb) if ra < rb else (rb, ra)
                parent[child] = root

        for e in edges:
            union(e["from_unit"], e["to_unit"])

        groups: dict[str, list[str]] = defaultdict(list)
        for code in sorted(parent):
            groups[find(code)].append(code)
        out: dict[str, list[str]] = {}
        for idx, members in enumerate(sorted(groups.values(), key=lambda m: m[0]), start=1):
            out[f"C{idx}"] = members
        return out

    def cohort_of(self, version_id: int) -> dict[str, str]:
        return {u: cid for cid, members in self.cohorts(version_id).items() for u in members}

    # ------------------------------------------------------------------ 审阅与修订

    def review_edge(self, edge_id: int, action: str, reviewer: str,
                    new_weight: float | None = None, comment: str = "") -> sqlite3.Row:
        edge = self.conn.execute("SELECT * FROM lineage_edge WHERE id=?", (edge_id,)).fetchone()
        if edge is None:
            raise LineageError(f"沿革边 {edge_id} 不存在")
        if action not in ("approved", "rejected", "weight_change"):
            raise LineageError(f"未知审阅动作 {action}")

        old_weight = edge["weight"]
        if action == "weight_change":
            if not new_weight or not (0 < new_weight <= 1):
                raise LineageError("新权重必须在 (0,1] 区间")
            self.conn.execute(
                "UPDATE lineage_edge SET weight=?, review_status='approved' WHERE id=?",
                (new_weight, edge_id),
            )
        else:
            self.conn.execute(
                "UPDATE lineage_edge SET review_status=? WHERE id=?", (action, edge_id)
            )
        self.conn.execute(
            "INSERT INTO edge_review(edge_id,action,old_weight,new_weight,reviewer,reviewed_at,comment)"
            " VALUES (?,?,?,?,?,?,?)",
            (edge_id, action, old_weight, new_weight, reviewer, now_iso(), comment),
        )
        self._audit("review_edge", "lineage_edge", str(edge_id),
                    f"{action}：{edge['from_unit']}->{edge['to_unit']} 权重 {old_weight}"
                    f" -> {new_weight or old_weight}；{comment}")
        return self.conn.execute("SELECT * FROM lineage_edge WHERE id=?", (edge_id,)).fetchone()

    def create_revised_version(self, label: str, description: str) -> int:
        """基于当前最新版本复制一份可修订的新映射版本（边与有效期映射整体快照）。"""
        latest = self.conn.execute(
            "SELECT MAX(version_id) AS v FROM mapping_version"
        ).fetchone()["v"]
        new_vid = latest + 1
        self.conn.execute(
            "INSERT INTO mapping_version(version_id,label,description,created_at,supersedes)"
            " VALUES (?,?,?,?,?)",
            (new_vid, label, description, now_iso(), latest),
        )
        self.conn.execute(
            "INSERT INTO lineage_edge(version_id,from_unit,to_unit,effective_period,edge_type,"
            "weight,review_status,note) "
            "SELECT ?,from_unit,to_unit,effective_period,edge_type,weight,review_status,note "
            "FROM lineage_edge WHERE version_id=?",
            (new_vid, latest),
        )
        self.conn.execute(
            "INSERT INTO mapping_entry(version_id,reported_code,canonical_unit,valid_from,valid_to)"
            "SELECT ?,reported_code,canonical_unit,valid_from,valid_to FROM mapping_entry WHERE version_id=?",
            (new_vid, latest),
        )
        self._audit("create_mapping_version", "mapping_version", str(new_vid),
                    f"基于 v{latest} 修订生成 {label}")
        return new_vid

    def update_edge_weight(self, version_id: int, edge_pk: int, weight: float) -> None:
        if not (0 < weight <= 1):
            raise LineageError("权重必须在 (0,1] 区间")
        edge = self.conn.execute(
            "SELECT * FROM lineage_edge WHERE id=? AND version_id=?", (edge_pk, version_id)
        ).fetchone()
        if edge is None:
            raise LineageError("该版本下找不到指定沿革边")
        self.conn.execute("UPDATE lineage_edge SET weight=? WHERE id=?", (weight, edge_pk))
        self._audit("update_edge_weight", "lineage_edge", str(edge_pk),
                    f"v{version_id} 权重修订 {edge['from_unit']}->{edge['to_unit']}: "
                    f"{edge['weight']} -> {weight}")

    def _audit(self, action: str, entity: str, entity_id: str | None, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(action,entity,entity_id,detail,created_at) VALUES (?,?,?,?,?)",
            (action, entity, entity_id, detail, now_iso()),
        )


def get_lineage_service(db_path: str) -> LineageService:
    return LineageService(connect(db_path))
