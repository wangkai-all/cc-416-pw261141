"""SQLite 存储层：全部表结构、连接管理与初始化。

设计要点
--------
* 单位沿革全部版本化（mapping_version / lineage_edge），分析版（analysis_version）
  绑定某一映射版本；修订映射后生成新分析版，旧版保留，两个口径可并列对比。
* 源事件不可变（accepted 后不允许修改），隔离项通过解除/拒绝动作流转。
* 分析运行带状态机与检查点表，中断后可从最后完成的期间恢复。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- 映射侧

CREATE TABLE IF NOT EXISTS canonical_unit (
    code        TEXT PRIMARY KEY,            -- 规范单位代码 U01..U10
    name        TEXT NOT NULL,
    region      TEXT NOT NULL,
    first_period TEXT NOT NULL,              -- 单位（在其当前身份下）首次存在的期间
    last_period  TEXT,                       -- NULL 表示至今仍在役
    status      TEXT NOT NULL CHECK (status IN ('active','retired'))
);

CREATE TABLE IF NOT EXISTS mapping_version (
    version_id  INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    supersedes  INTEGER REFERENCES mapping_version(version_id)
);

-- 拆/合/迁址边：from_unit 在 effective_period 把业务量按 weight 移交给 to_unit
CREATE TABLE IF NOT EXISTS lineage_edge (
    id              INTEGER PRIMARY KEY,
    version_id      INTEGER NOT NULL REFERENCES mapping_version(version_id),
    from_unit       TEXT NOT NULL REFERENCES canonical_unit(code),
    to_unit         TEXT NOT NULL REFERENCES canonical_unit(code),
    effective_period TEXT NOT NULL,
    edge_type       TEXT NOT NULL CHECK (edge_type IN ('split','merge','relocation')),
    weight          REAL NOT NULL CHECK (weight > 0 AND weight <= 1),
    review_status   TEXT NOT NULL DEFAULT 'proposed'
                    CHECK (review_status IN ('proposed','approved','rejected')),
    note            TEXT,
    UNIQUE (version_id, from_unit, to_unit, effective_period)
);

-- 上报代码 -> 规范单位的有效期映射（同一上报名称在不同期间可能指向不同单位）
CREATE TABLE IF NOT EXISTS mapping_entry (
    id            INTEGER PRIMARY KEY,
    version_id    INTEGER NOT NULL REFERENCES mapping_version(version_id),
    reported_code TEXT NOT NULL,
    canonical_unit TEXT NOT NULL REFERENCES canonical_unit(code),
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,                       -- NULL 表示至今
    UNIQUE (version_id, reported_code, valid_from)
);

-- ---------------------------------------------------------------- 源数据侧

CREATE TABLE IF NOT EXISTS source_batch (
    batch_id    TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    received_at TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS source_event (
    id             INTEGER PRIMARY KEY,
    batch_id       TEXT REFERENCES source_batch(batch_id),
    reported_code  TEXT NOT NULL,
    period         TEXT NOT NULL,
    parcels        INTEGER NOT NULL CHECK (parcels >= 0),
    on_time        INTEGER NOT NULL,
    late           INTEGER NOT NULL,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('accepted','quarantined','rejected')),
    quarantine_reason TEXT,
    dedup_key      TEXT NOT NULL UNIQUE,      -- 上报代码+期间+件量，重复批次据此识别
    raw_note       TEXT
);

CREATE TABLE IF NOT EXISTS event_flag (
    id        INTEGER PRIMARY KEY,
    event_id  INTEGER NOT NULL REFERENCES source_event(id),
    flag_type TEXT NOT NULL,                 -- spike / duplicate / retired_target ...
    severity  TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    detail    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- ---------------------------------------------------------------- 分析侧

CREATE TABLE IF NOT EXISTS analysis_version (
    version_id      INTEGER PRIMARY KEY,
    label           TEXT NOT NULL,
    mapping_version_id INTEGER NOT NULL REFERENCES mapping_version(version_id),
    created_at      TEXT NOT NULL,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS analysis_run (
    id                  INTEGER PRIMARY KEY,
    analysis_version_id INTEGER NOT NULL REFERENCES analysis_version(version_id),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL
                        CHECK (status IN ('running','interrupted','completed','failed','superseded')),
    last_checkpoint_period TEXT,              -- 已完成的最后期间（恢复起点）
    total_periods       INTEGER NOT NULL DEFAULT 0,
    fail_reason         TEXT
);

CREATE TABLE IF NOT EXISTS analysis_result (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES analysis_run(id),
    analysis_version_id INTEGER NOT NULL REFERENCES analysis_version(version_id),
    scope_kind          TEXT NOT NULL CHECK (scope_kind IN ('unit','cohort')),
    scope_code          TEXT NOT NULL,        -- 单位代码或队列 ID（C1..）
    period              TEXT NOT NULL,
    as_reported         INTEGER NOT NULL,     -- 当时口径：只含该期间实际以该代码上报的量
    restated            REAL NOT NULL,        -- 最新口径：按当前单位边界重述的量
    on_time             INTEGER NOT NULL,
    late                INTEGER NOT NULL,
    UNIQUE (analysis_version_id, scope_kind, scope_code, period)
);

-- 单位边界调整说明：某单位在某期间因拆/合/迁址承接或交出的件量（非业务增长）
CREATE TABLE IF NOT EXISTS adjustment (
    id                  INTEGER PRIMARY KEY,
    analysis_version_id INTEGER NOT NULL REFERENCES analysis_version(version_id),
    unit_code           TEXT NOT NULL REFERENCES canonical_unit(code),
    period              TEXT NOT NULL,
    edge_id             INTEGER NOT NULL REFERENCES lineage_edge(id),
    direction           TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
    parcels             INTEGER NOT NULL,
    UNIQUE (analysis_version_id, unit_code, period, edge_id, direction)
);

-- 队列（沿革连通分量）成员关系，按分析版快照保存
CREATE TABLE IF NOT EXISTS cohort_membership (
    analysis_version_id INTEGER NOT NULL REFERENCES analysis_version(version_id),
    cohort_id           TEXT NOT NULL,
    unit_code           TEXT NOT NULL REFERENCES canonical_unit(code),
    PRIMARY KEY (analysis_version_id, cohort_id, unit_code)
);

-- ---------------------------------------------------------------- 审阅与审计

CREATE TABLE IF NOT EXISTS edge_review (
    id          INTEGER PRIMARY KEY,
    edge_id     INTEGER NOT NULL REFERENCES lineage_edge(id),
    action      TEXT NOT NULL CHECK (action IN ('approved','rejected','weight_change')),
    old_weight  REAL,
    new_weight  REAL,
    reviewer    TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    comment     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_status_period ON source_event(status, period);
CREATE INDEX IF NOT EXISTS idx_result_lookup
    ON analysis_result(analysis_version_id, scope_kind, scope_code, period);
CREATE INDEX IF NOT EXISTS idx_edge_version ON lineage_edge(version_id);
CREATE INDEX IF NOT EXISTS idx_entry_lookup ON mapping_entry(version_id, reported_code, valid_from);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
