"""HTTP API + 静态资源服务（仅标准库）。

路由前缀 /api：
  GET  /api/meta                 单位、映射版本、分析版、队列总览
  GET  /api/units                规范单位清单
  GET  /api/lineage/edges?vid=   某映射版本的沿革边（含审阅状态）
  GET  /api/lineage/entries?vid= 上报代码有效期映射
  POST /api/lineage/review       审阅边 {edge_id, action, new_weight?, comment?}
  POST /api/lineage/revise       修订生成新映射版本 {label, description}
  POST /api/lineage/edge-weight  改边权重 {version_id, edge_id, weight}
  GET  /api/events?status=       源事件清单
  GET  /api/quarantine           隔离区
  POST /api/quarantine/release   解除隔离 {event_id}
  POST /api/quarantine/reject    拒绝 {event_id}
  POST /api/ingest               新批次投递 {batch_id, source_name, items[]}
  GET  /api/analysis/versions    分析版列表
  POST /api/analysis/version     新建分析版 {mapping_version_id, label, note?}
  GET  /api/analysis/runs?av=    运行列表
  POST /api/analysis/start       后台启动 {analysis_version_id}
  POST /api/analysis/interrupt   请求中断 {run_id}
  POST /api/analysis/resume      恢复运行 {run_id}
  POST /api/analysis/rerun       修订后重算 {analysis_version_id, reason?}
  GET  /api/analysis/series?av&kind&code   双口径序列
  GET  /api/analysis/decomp?av&kind&code   环比调整/有机分解
  GET  /api/analysis/adjustments?av&unit? 调整明细
  GET  /api/analysis/trace?av&unit&period  异常贡献追溯
  GET  /api/audit                审计日志
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .analysis import AnalysisError, AnalysisService
from .db import connect
from .ingestion import IngestionError, IngestionService, IngestItem
from .lineage import LineageError, LineageService
from .util import now_iso

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _row_to_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


class ApiHandler(BaseHTTPRequestHandler):
    db_path: str = ""  # 由 make_server 注入类属性

    # ------------------------------------------------------------ 基础框架
    def _open(self):
        return connect(self.db_path)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _ApiError(400, f"请求体不是合法 JSON：{exc}") from exc

    def _query(self, key: str, default=None):
        qs = parse_qs(urlparse(self.path).query)
        return qs.get(key, [default])[0]

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        return

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/"):
                self._route_get(path)
            else:
                self._serve_static(path)
        except _ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (AnalysisError, IngestionError, LineageError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器内部错误：{exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/"):
                raise _ApiError(404, "未知路径")
            self._route_post(path)
        except _ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except (AnalysisError, IngestionError, LineageError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器内部错误：{exc}"}, 500)

    def _route_get(self, path: str) -> None:
        conn = self._open()
        try:
            lin = LineageService(conn)
            ana = AnalysisService(conn, db_path=self.db_path)
            ing = IngestionService(conn)

            if path == "/api/meta":
                self._send_json(self._meta(conn, lin, ana))
            elif path == "/api/units":
                self._send_json(_rows(lin.list_units()))
            elif path == "/api/lineage/edges":
                vid = int(self._query("vid", "1"))
                self._send_json(_rows(lin.list_edges(vid)))
            elif path == "/api/lineage/entries":
                vid = int(self._query("vid", "1"))
                self._send_json(_rows(lin.list_entries(vid)))
            elif path == "/api/events":
                status = self._query("status")
                self._send_json(_rows(ing.list_events(status=status)))
            elif path == "/api/quarantine":
                self._send_json(_rows(ing.list_quarantine()))
            elif path == "/api/analysis/versions":
                self._send_json(_rows(ana.list_analysis_versions()))
            elif path == "/api/analysis/runs":
                av = self._query("av")
                self._send_json(_rows(ana.list_runs(int(av) if av else None)))
            elif path == "/api/analysis/series":
                av, kind, code = self._require_av_kind_code()
                self._send_json(_rows(ana.get_series(av, kind, code)))
            elif path == "/api/analysis/decomp":
                av, kind, code = self._require_av_kind_code()
                self._send_json(ana.growth_decomposition(av, kind, code))
            elif path == "/api/analysis/adjustments":
                av = int(self._query("av"))
                self._send_json(_rows(ana.get_adjustments(av, self._query("unit"))))
            elif path == "/api/analysis/trace":
                av = int(self._query("av"))
                unit = self._query("unit")
                period = self._query("period")
                if not unit or not period:
                    raise _ApiError(400, "需要 unit 与 period 参数")
                self._send_json(ana.trace_contributors(av, unit, period))
            elif path == "/api/audit":
                self._send_json(_rows(conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()))
            else:
                raise _ApiError(404, f"未知接口 {path}")
        finally:
            conn.close()

    def _route_post(self, path: str) -> None:
        body = self._read_body()
        conn = self._open()
        try:
            lin = LineageService(conn)
            ana = AnalysisService(conn, db_path=self.db_path)
            ing = IngestionService(conn, mapping_version_id=body.get("mapping_version_id", 1))

            if path == "/api/lineage/review":
                row = lin.review_edge(
                    int(body["edge_id"]), body["action"],
                    body.get("reviewer", "值班分析员"),
                    body.get("new_weight"), body.get("comment", ""),
                )
                conn.commit()
                self._send_json(_row_to_dict(row))
            elif path == "/api/lineage/revise":
                vid = lin.create_revised_version(body["label"], body["description"])
                conn.commit()
                self._send_json({"version_id": vid})
            elif path == "/api/lineage/edge-weight":
                lin.update_edge_weight(int(body["version_id"]), int(body["edge_id"]),
                                       float(body["weight"]))
                conn.commit()
                self._send_json({"ok": True})
            elif path == "/api/quarantine/release":
                self._send_json(ing.release_event(int(body["event_id"]),
                                                  body.get("reviewer", "值班分析员")))
            elif path == "/api/quarantine/reject":
                self._send_json(ing.reject_event(int(body["event_id"]),
                                                 body.get("reviewer", "值班分析员")))
            elif path == "/api/ingest":
                items = [IngestItem(**it) for it in body.get("items", [])]
                result = ing.ingest_batch(
                    body["batch_id"], body.get("source_name", "手工投递"), items,
                    body.get("note", ""),
                )
                self._send_json(result, 201)
            elif path == "/api/analysis/version":
                vid = ana.create_analysis_version(
                    int(body["mapping_version_id"]), body["label"], body.get("note", ""))
                self._send_json({"version_id": vid}, 201)
            elif path == "/api/analysis/start":
                run_id = ana.start_background(int(body["analysis_version_id"]))
                self._send_json({"run_id": run_id}, 202)
            elif path == "/api/analysis/interrupt":
                ana.request_interrupt(int(body["run_id"]))
                self._send_json({"ok": True})
            elif path == "/api/analysis/resume":
                ana.resume_background(int(body["run_id"]))
                self._send_json({"ok": True}, 202)
            elif path == "/api/analysis/rerun":
                run_id = ana.rerun(int(body["analysis_version_id"]),
                                   body.get("reason", "映射修订后重算"))
                conn.commit()
                ana.resume_background(run_id)
                self._send_json({"run_id": run_id}, 202)
            else:
                raise _ApiError(404, f"未知接口 {path}")
        finally:
            conn.close()

    # ------------------------------------------------------------ 聚合视图
    def _meta(self, conn, lin: LineageService, ana: AnalysisService) -> dict:
        mapping_versions = _rows(lin.list_versions())
        for mv in mapping_versions:
            mv["cohorts"] = lin.cohorts(mv["version_id"])
            mv["edges_pending"] = conn.execute(
                "SELECT COUNT(*) c FROM lineage_edge WHERE version_id=? AND review_status='proposed'",
                (mv["version_id"],),
            ).fetchone()["c"]
        q_counts = conn.execute(
            "SELECT quarantine_reason, COUNT(*) c FROM source_event "
            "WHERE status='quarantined' GROUP BY quarantine_reason"
        ).fetchall()
        return {
            "generated_at": now_iso(),
            "units": _rows(lin.list_units()),
            "mapping_versions": mapping_versions,
            "analysis_versions": _rows(ana.list_analysis_versions()),
            "quarantine_summary": {r["quarantine_reason"]: r["c"] for r in q_counts},
            "event_counts": {
                r["status"]: r["c"] for r in conn.execute(
                    "SELECT status, COUNT(*) c FROM source_event GROUP BY status"
                ).fetchall()
            },
        }

    def _require_av_kind_code(self):
        av = self._query("av")
        kind = self._query("kind")
        code = self._query("code")
        if not av or kind not in ("unit", "cohort") or not code:
            raise _ApiError(400, "需要 av、kind(unit|cohort)、code 三个参数")
        return int(av), kind, code

    # ------------------------------------------------------------ 静态资源
    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self.send_error(404, "Not Found")
            return
        suffix = target.suffix
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
                     suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    ApiHandler.db_path = db_path
    server = ThreadingHTTPServer((host, port), ApiHandler)
    return server
