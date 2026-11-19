"""项目启动入口：初始化数据库 -> 预置分析版 -> 启动 Web 服务。

用法：
  python3 run.py                # 首次自动建库并写入虚构数据
  python3 run.py --reset        # 删除旧库后重建
  python3 run.py --port 8010
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from app.analysis import AnalysisService
from app.api import make_server
from app.db import connect
from app.seed import DATA_END, DATA_START, generate

DB_PATH = Path(__file__).resolve().parent / "data" / "observatory.db"


def bootstrap(db_path: Path) -> None:
    """首次启动时建库、造数，并产出两个基线分析版（v1 原口径 / v2 修订口径）。"""
    if db_path.exists():
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[bootstrap] 生成虚构观测数据（{DATA_START} ~ {DATA_END}）...")
    generate(db_path)

    conn = connect(db_path)
    try:
        svc = AnalysisService(conn, db_path=str(db_path))
        # 分析版 1：绑定映射 v1（含漏接边错误）
        av1 = svc.create_analysis_version(
            1, "分析版 2026-10-A（映射 v1 初版）",
            "基于初版映射计算；西关站 U06 的合并边漏接，老城队列历史不完整"
        )
        svc.run(av1)

        # 分析版 2：绑定映射 v2，先在“修订边待审阅”状态下计算一次，
        # 再演示审阅通过 -> 重算生成完整修订版
        av2 = svc.create_analysis_version(
            2, "分析版 2026-10-B（映射 v2 修订版）",
            "补登 U06->U07 合并边并重算；与 A 版并列展示差异"
        )
        svc.run(av2)
        pending = conn.execute(
            "SELECT id FROM lineage_edge WHERE version_id=2 AND review_status='proposed'"
        ).fetchall()
        for row in pending:
            svc.lineage.review_edge(row["id"], "approved", "沿革管理员",
                                    comment="启动预置：核对撤站文件后补登")
        conn.commit()
        new_run = svc.rerun(av2, "修订边审阅通过，基线重算")
        svc._process(new_run)
        print(f"[bootstrap] 基线分析版就绪：A=v{av1}（初版）, B=v{av2}（修订版）")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="观测数据加工流水线")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reset", action="store_true", help="删除旧库后重建")
    args = parser.parse_args()

    if args.reset and DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    bootstrap(DB_PATH)
    server = make_server(str(DB_PATH), args.host, args.port)
    print(f"[server] 观测分析台已启动: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 已停止")
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
