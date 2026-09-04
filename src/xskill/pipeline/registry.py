"""
pipeline/registry.py -- SQLite 路径注册表 + Registry 实体类
============================================================

管理 ``~/.xskill/registry.db``，**只存路径和状态**，不存内容。

两张表：

- ``watch_dirs``   用户注册的待监听目录
- ``trajectories`` 每条轨迹文件的发现/索引状态

模块底部的 ``Registry`` 类把上面的模块函数包装为 OOP 接口；所有
watch_dir + trajectory 反查走这个类。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import zlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Optional

from xskill._sqlite_connect import connect_with_lock
from xskill.config import get_registry_db_path
from xskill.types import WatchDir

logger = logging.getLogger("xskill.registry")

_REGISTRY_DB_LOCKS_GUARD = threading.Lock()
_REGISTRY_WAL_LOCKS: dict[Path, threading.Lock] = {}
_REGISTRY_SCHEMA_LOCKS: dict[Path, threading.Lock] = {}
_REGISTRY_WAL_READY: dict[Path, tuple[int, int]] = {}
# 线程本地的按 DB 连接槽位（pooled_connection 用）；线程死亡随 GC 释放
_REGISTRY_THREAD_POOL = threading.local()
_REGISTRY_THREAD_POOL_CAPACITY = 4


def _registry_db_lock(
    mapping: dict[Path, threading.Lock],
    db_path: Path,
) -> threading.Lock:
    """返回进程内按 registry DB 文件共享的锁。"""
    with _REGISTRY_DB_LOCKS_GUARD:
        return mapping.setdefault(db_path, threading.Lock())


def _registry_db_identity(db_path: Path) -> tuple[int, int]:
    stat = db_path.stat()
    return stat.st_dev, stat.st_ino


class TrajectoryStatus(str, Enum):
    DISCOVERED = "discovered"
    UPDATED = "updated"
    SPLITTING = "splitting"
    SPLIT_DONE = "split_done"
    INDEXED = "indexed"
    DONE = "done"
    FILTERED = "filtered"
    ERROR = "error"
    CLUSTERING = "clustering"
    META_DONE = "meta_done"


class ProcessAction(str, Enum):
    CLUSTERED = "clustered"
    NOT_FIT = "not_fit"


# 流水线尚未处理完的状态；不含 error（重试由 max_retries 有界，耗尽即终态）。
PENDING_TRAJECTORY_STATUSES = (
    TrajectoryStatus.DISCOVERED.value,
    TrajectoryStatus.UPDATED.value,
    TrajectoryStatus.SPLITTING.value,
    TrajectoryStatus.SPLIT_DONE.value,
    TrajectoryStatus.INDEXED.value,
    TrajectoryStatus.CLUSTERING.value,
)

# 暂停目录在面板上仍保留已有处理结果，只隐藏尚未完成拆分的积压。恢复目录后
# auto_index=1，所有状态会立即重新纳入展示和统计，无需改写 trajectory 行。
DASHBOARD_VISIBLE_WHEN_PAUSED_STATUSES = (
    TrajectoryStatus.SPLIT_DONE.value,
    TrajectoryStatus.INDEXED.value,
    TrajectoryStatus.CLUSTERING.value,
    TrajectoryStatus.DONE.value,
    TrajectoryStatus.FILTERED.value,
    TrajectoryStatus.ERROR.value,
)


def dashboard_visible_trajectory_sql(
    trajectory_alias: str = "t",
    watch_dir_alias: str = "w",
) -> str:
    """返回面板统一使用的轨迹可见性 SQL 条件。

    未暂停目录全量可见；暂停目录只显示已完成拆分或已到终态的轨迹。参数只接受
    内部调用方传入的固定 SQL alias，不接收请求输入。
    """
    statuses = ",".join(
        f"'{status}'" for status in DASHBOARD_VISIBLE_WHEN_PAUSED_STATUSES
    )
    return (
        f"(COALESCE({watch_dir_alias}.auto_index,1)=1 OR "
        f"{trajectory_alias}.status IN ({statuses}))"
    )

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watch_dirs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT UNIQUE NOT NULL,
    label      TEXT DEFAULT '',
    auto_index INTEGER DEFAULT 1,
    ecosystem  TEXT DEFAULT 'manual',
    source_scope_id TEXT UNIQUE,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trajectories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_dir_id  INTEGER NOT NULL REFERENCES watch_dirs(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    has_meta      INTEGER DEFAULT 0,
    has_embedding INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'discovered',
    process_action TEXT,
    interest_fingerprint TEXT,
    skill_generated TEXT,
    skill_used    TEXT,
    canary_side   TEXT,
    source_model  TEXT,
    source_harness TEXT,
    user_key      TEXT DEFAULT '',
    ux_score      REAL,
    error_msg     TEXT,
    retry_count   INTEGER DEFAULT 0,
    file_mtime    REAL DEFAULT 0,
    discovered_at TEXT DEFAULT (datetime('now')),
    indexed_at    TEXT,
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(watch_dir_id, filename)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT DEFAULT (datetime('now')),
    step         TEXT,
    model        TEXT,
    prompt       INTEGER DEFAULT 0,
    completion   INTEGER DEFAULT 0,
    total        INTEGER DEFAULT 0,
    cost_usd     REAL DEFAULT 0,
    price_source TEXT,
    usage_event_id TEXT UNIQUE,
    usage_plane TEXT DEFAULT 'xskill_processing',
    measurement_quality TEXT,
    allocation_mode TEXT DEFAULT 'unattributed',
    estimation_method TEXT,
    unavailable_reason TEXT,
    tenant_id TEXT,
    task_scope_id TEXT,
    source_scope_id TEXT,
    traj_id TEXT,
    atom_id TEXT,
    generation_id TEXT,
    legacy INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);

-- 埋点(instrumentation,在代码里插记录点):三类事件,供看板算衍生率 --
CREATE TABLE IF NOT EXISTS recommendation_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT DEFAULT (datetime('now')),
    client_id TEXT,
    skill     TEXT,
    side      TEXT,          -- main / staging
    bucket    TEXT           -- ranked / recommended
);
CREATE INDEX IF NOT EXISTS idx_reco_skill ON recommendation_log(skill);

CREATE TABLE IF NOT EXISTS atom_adoption (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT (datetime('now')),
    atom_id     TEXT,
    skill       TEXT,
    weightscore INTEGER,
    was_new     INTEGER       -- 1=首次加入 0=覆盖
);
CREATE INDEX IF NOT EXISTS idx_atom_adopt ON atom_adoption(atom_id);

CREATE TABLE IF NOT EXISTS canary_decision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT DEFAULT (datetime('now')),
    skill           TEXT,
    action          TEXT,     -- promoted / rejected / timeout_discarded
    main_avg        REAL,
    staging_avg     REAL,
    main_samples    INTEGER,
    staging_samples INTEGER,
    age_days        REAL
);

-- P2-2.4 控制面:用户/全局 skill 偏好(pinned|blocked)。
-- user_key='*global*' 为全局 pin/屏蔽(admin 设);其余为 user_name(D5)。
-- 超量校验在写入侧(D8),sync 读路径永不因此报错。
CREATE TABLE IF NOT EXISTS skill_prefs (
    user_key   TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    pref       TEXT NOT NULL CHECK(pref IN ('pinned','blocked')),
    set_by     TEXT DEFAULT '',
    -- pin 时可钉 side（main|staging）；空串=走 pick_side / resolve_side
    side       TEXT NOT NULL DEFAULT '',
    ts         TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_key, skill_name)
);

-- client 截取安装数 take_n：对服务器推送队列取前 N 装入 harness；NULL=跟服务器 skill_slots。
CREATE TABLE IF NOT EXISTS user_client_settings (
    user_key   TEXT PRIMARY KEY,
    take_n     INTEGER,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- P2-2.4c skill 生命周期:retired=下线(停止分发/推荐,数据与 git 历史保留)。
-- 删除是物理动作不入此表;"在役"=无行。
CREATE TABLE IF NOT EXISTS skill_lifecycle (
    skill_name TEXT PRIMARY KEY,
    state      TEXT NOT NULL CHECK(state IN ('retired')),
    set_by     TEXT DEFAULT '',
    ts         TEXT DEFAULT (datetime('now'))
);

-- skills 列表投影表：磁盘为真相源；写出口 UPSERT；dashboard 分页查表。
-- catalog_key=native:{name} | skillhub:{skill_id}；root_key=自产 skill 根目录。
CREATE TABLE IF NOT EXISTS skills_catalog (
    catalog_key       TEXT PRIMARY KEY,
    root_key          TEXT NOT NULL DEFAULT '',
    name              TEXT NOT NULL,
    repo_name         TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL CHECK(source IN ('native','skillhub')),
    state             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    version           INTEGER NOT NULL DEFAULT 0,
    candidates_count  INTEGER NOT NULL DEFAULT 0,
    main_sha          TEXT NOT NULL DEFAULT '',
    staging_sha       TEXT NOT NULL DEFAULT '',
    distributable     INTEGER NOT NULL DEFAULT 0,
    search_id         TEXT NOT NULL DEFAULT '',
    hub               TEXT NOT NULL DEFAULT '',
    skill_id          TEXT NOT NULL DEFAULT '',
    use_count         INTEGER NOT NULL DEFAULT 0,
    content_sha       TEXT NOT NULL DEFAULT '',
    updated_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_skills_catalog_root
    ON skills_catalog(root_key);
CREATE INDEX IF NOT EXISTS idx_skills_catalog_root_name
    ON skills_catalog(root_key, name);
CREATE INDEX IF NOT EXISTS idx_skills_catalog_root_state
    ON skills_catalog(root_key, state);

CREATE TABLE IF NOT EXISTS skills_catalog_meta (
    root_key      TEXT PRIMARY KEY,
    backfilled_at TEXT NOT NULL,
    skillhub_key  TEXT NOT NULL DEFAULT '',
    generation    INTEGER NOT NULL DEFAULT 0
);

-- 预计算推荐结果：/sync 只读；重活进程写入（脏算）。
CREATE TABLE IF NOT EXISTS client_recommend_slots (
    user_key     TEXT PRIMARY KEY,
    slots_json   TEXT NOT NULL DEFAULT '[]',
    fingerprint  TEXT NOT NULL DEFAULT '',
    computed_at  TEXT NOT NULL,
    stale        INTEGER NOT NULL DEFAULT 0
);

-- 推荐脏队列：仅重活进程消费。
CREATE TABLE IF NOT EXISTS recommend_dirty (
    user_key   TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    marked_at  TEXT NOT NULL
);

-- skills_catalog → vector index 增量同步队列。generation 是消费 fence：
-- worker 只能清理自己实际处理的版本，并发晚到写入会保留到下一轮。
CREATE TABLE IF NOT EXISTS catalog_vector_dirty (
    catalog_key TEXT PRIMARY KEY,
    generation  INTEGER NOT NULL DEFAULT 1,
    dirty       INTEGER NOT NULL DEFAULT 1 CHECK(dirty IN (0,1)),
    operation   TEXT NOT NULL CHECK(operation IN ('upsert','delete')),
    content_sha TEXT NOT NULL DEFAULT '',
    marked_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_vector_dirty_marked
    ON catalog_vector_dirty(dirty, marked_at, catalog_key);

-- 全量向量对账水位：首次升级、模型/算法变化和低频修复时更新。
-- sweep_seeded_fingerprint 非空 = 这个指纹对应的全量播种已经做过、还没
-- 追平（见 _migrate 里的注释，issue #328 review）。
CREATE TABLE IF NOT EXISTS catalog_vector_sync_meta (
    singleton                   INTEGER PRIMARY KEY CHECK(singleton=1),
    model_fingerprint           TEXT NOT NULL DEFAULT '',
    reconciled_at               REAL NOT NULL DEFAULT 0,
    sweep_seeded_fingerprint    TEXT NOT NULL DEFAULT ''
);

-- 用户画像脏队列：generation 让“计算期间又发生变化”不会被旧任务误清。
CREATE TABLE IF NOT EXISTS profile_dirty (
    user_key    TEXT PRIMARY KEY,
    generation  INTEGER NOT NULL DEFAULT 1,
    reason      TEXT NOT NULL DEFAULT '',
    marked_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profile_dirty_marked
    ON profile_dirty(marked_at, user_key);

-- 画像算法/embedding 输入版本与低频全量对账水位。
CREATE TABLE IF NOT EXISTS profile_refresh_meta (
    singleton          INTEGER PRIMARY KEY CHECK(singleton=1),
    input_fingerprint  TEXT NOT NULL DEFAULT '',
    reconciled_at      REAL NOT NULL DEFAULT 0
);

-- P3-3.1 events:四类既有事实源的消费者(D7),通知+世界消息共用。
-- kind: feedback(他人触发+ux打分) / push_edit(修改分支) / canary(裁决) / pin。
-- targets 单独成表:一条事件可通知多个贡献者;世界消息 feed 直接读 events。
-- 已读状态是每用户一个游标(event_reads.last_read_id),不做逐行 read 标记。
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT DEFAULT (datetime('now')),
    kind    TEXT NOT NULL CHECK(kind IN ('feedback','push_edit','canary','pin')),
    actor   TEXT DEFAULT '',
    skill   TEXT DEFAULT '',
    traj_id TEXT DEFAULT '',
    payload TEXT DEFAULT '{}'
);
-- D7 扇出去重:同一轨迹多 atom 命中同一 skill 只发一条 feedback
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_feedback_dedup
    ON events(skill, traj_id) WHERE kind='feedback';

CREATE TABLE IF NOT EXISTS event_targets (
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_key TEXT NOT NULL,
    PRIMARY KEY (event_id, user_key)
);
CREATE INDEX IF NOT EXISTS idx_event_targets_user ON event_targets(user_key);

CREATE TABLE IF NOT EXISTS event_reads (
    user_key     TEXT PRIMARY KEY,
    last_read_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skill_trigger_eval (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT DEFAULT (datetime('now')),
    skill        TEXT,        -- skill slug
    version_sha  TEXT,        -- 评测时该 skill 的 main sha(首版/未提交可空)
    exp_id       TEXT,        -- .description_optimization 实验目录号
    train_score  REAL,        -- 中选描述在 train 集触发准确率
    test_score   REAL,        -- 中选描述在 held-out test 集触发准确率(选优依据)
    n_cases      INTEGER,     -- 合成 case 总数
    catalog_size INTEGER      -- 诱饵清单平均大小(竞争对手数)
);
CREATE INDEX IF NOT EXISTS idx_trig_skill ON skill_trigger_eval(skill);

-- #106 画像散点物化缓存:事件触发重算落盘,scatter 端点退化为纯读。
-- payload=JSON 坐标包;fingerprint=散点输入的廉价内容指纹(输入不变则命中不重算)。
CREATE TABLE IF NOT EXISTS scatter_cache (
    user_key    TEXT NOT NULL,
    method      TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL,
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_key, method)
);

-- UX 体验分事实源（盘上 .ux_scores.jsonl 由定时任务 sync 入库；读路径查本表）
CREATE TABLE IF NOT EXISTS ux_scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name  TEXT NOT NULL,
    side        TEXT NOT NULL,
    commit_sha  TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL,
    scored_at   TEXT NOT NULL,
    atom_id     TEXT NOT NULL DEFAULT '',
    traj_id     TEXT NOT NULL DEFAULT '',
    reasons     TEXT NOT NULL DEFAULT '',
    user_model  TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ux_atom
    ON ux_scores(skill_name, side, atom_id) WHERE atom_id != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_ux_traj
    ON ux_scores(skill_name, side, traj_id) WHERE traj_id != '';
CREATE INDEX IF NOT EXISTS idx_ux_rank
    ON ux_scores(skill_name, side, commit_sha, scored_at);

CREATE TABLE IF NOT EXISTS ux_scores_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- atom 在途 pending 投影：真相仍是各 skill 的 .candidates.yml；
-- 写出口（candidates 落盘闸）同步；dashboard 读路径只查本表，禁止 per-atom 扫盘。
CREATE TABLE IF NOT EXISTS atom_candidate_pending (
    atom_id     TEXT NOT NULL,
    skill       TEXT NOT NULL,
    weightscore INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (atom_id, skill)
);
CREATE INDEX IF NOT EXISTS idx_acp_skill ON atom_candidate_pending(skill);

-- root_key = skill_dir 绝对路径 → backfill/reconcile ready 标记；
-- root_key = pending_mtime:{skill} → .candidates.yml mtime（合扫增量跳过）。
CREATE TABLE IF NOT EXISTS atom_candidate_pending_meta (
    root_key      TEXT PRIMARY KEY,
    backfilled_at TEXT NOT NULL
);

-- SkillEdit 持久脏队列：候选写入只合并同一 skill 的 generation；watcher
-- 以 compare-and-delete 确认已观察版本，避免编辑期间的新写入被旧任务误清。
CREATE TABLE IF NOT EXISTS skill_edit_dirty (
    root_key   TEXT NOT NULL,
    skill      TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    reason     TEXT NOT NULL DEFAULT '',
    marked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (root_key, skill)
);

-- 纳入 / generate 发起人。首次写入生效，供自动灰度对象（与用量最多的用户取并）。
CREATE TABLE IF NOT EXISTS skill_origin (
    skill_name TEXT PRIMARY KEY,
    user_key   TEXT NOT NULL,
    source     TEXT NOT NULL,
    ts         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_skill_origin_user ON skill_origin(user_key);

-- Logical Task 事实源位于 task_graph/scopes；下列表均为可重建查询投影。
CREATE TABLE IF NOT EXISTS task_graph_generations (
    tenant_id          TEXT NOT NULL,
    task_scope_id      TEXT NOT NULL,
    generation_id     TEXT NOT NULL,
    source_revision    TEXT NOT NULL,
    generator_json    TEXT NOT NULL DEFAULT '{}',
    base_override_seq  INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    task_count         INTEGER NOT NULL DEFAULT 0,
    atom_count         INTEGER NOT NULL DEFAULT 0,
    candidate_count    INTEGER NOT NULL DEFAULT 0,
    model_judgement_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, task_scope_id)
);

CREATE TABLE IF NOT EXISTS logical_tasks (
    tenant_id          TEXT NOT NULL,
    task_scope_id      TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    generation_id      TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    lifecycle          TEXT NOT NULL,
    outcome            TEXT NOT NULL,
    verification       TEXT NOT NULL,
    user_disposition   TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    tombstoned         INTEGER NOT NULL DEFAULT 0,
    aliases_json       TEXT NOT NULL DEFAULT '[]',
    decisions_json     TEXT NOT NULL DEFAULT '[]',
    primary_atom_count INTEGER NOT NULL DEFAULT 0,
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    execution_tokens   INTEGER,
    execution_cost_usd REAL,
    PRIMARY KEY (tenant_id, task_scope_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_logical_tasks_scope_state
    ON logical_tasks(tenant_id, task_scope_id, tombstoned, created_at, task_id);
CREATE INDEX IF NOT EXISTS idx_logical_tasks_tenant_state
    ON logical_tasks(tenant_id, tombstoned, created_at, task_id);

CREATE TABLE IF NOT EXISTS task_atom_memberships (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    membership_id   TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    source_scope_id TEXT NOT NULL,
    traj_id          TEXT NOT NULL,
    atom_id          TEXT NOT NULL,
    role             TEXT NOT NULL,
    decision         TEXT NOT NULL,
    confidence       REAL,
    decided_by       TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    observed_at      TEXT NOT NULL,
    stale            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, task_scope_id, membership_id)
);
CREATE INDEX IF NOT EXISTS idx_task_membership_atom
    ON task_atom_memberships(
        tenant_id, task_scope_id, source_scope_id, traj_id, atom_id, decision, role
    );
CREATE INDEX IF NOT EXISTS idx_task_membership_task
    ON task_atom_memberships(tenant_id, task_scope_id, task_id, decision, role);
CREATE INDEX IF NOT EXISTS idx_task_membership_session
    ON task_atom_memberships(
        tenant_id, source_scope_id, traj_id, atom_id, decision, role
    );

CREATE TABLE IF NOT EXISTS task_relations (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    relation_id     TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    from_task_id    TEXT NOT NULL,
    to_task_id      TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    decision        TEXT NOT NULL,
    confidence      REAL,
    decided_by      TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    observed_at     TEXT NOT NULL,
    stale           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, task_scope_id, relation_id)
);
CREATE INDEX IF NOT EXISTS idx_task_relations_from
    ON task_relations(tenant_id, task_scope_id, from_task_id, decision);
CREATE INDEX IF NOT EXISTS idx_task_relations_to
    ON task_relations(tenant_id, task_scope_id, to_task_id, decision);

CREATE TABLE IF NOT EXISTS task_attempts (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    attempt_id       TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    task_id          TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    lifecycle        TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    verification     TEXT NOT NULL,
    user_disposition TEXT NOT NULL,
    decisions_json   TEXT NOT NULL DEFAULT '[]',
    execution_identity_json TEXT NOT NULL DEFAULT '{}',
    evidence_count   INTEGER NOT NULL DEFAULT 0,
    execution_tokens INTEGER,
    execution_cost_usd REAL,
    PRIMARY KEY (tenant_id, task_scope_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_task_attempts_task
    ON task_attempts(tenant_id, task_scope_id, task_id, started_at, attempt_id);

CREATE TABLE IF NOT EXISTS task_evidence_ranges (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    evidence_id      TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    attempt_id       TEXT NOT NULL,
    source_scope_id TEXT NOT NULL,
    traj_id          TEXT NOT NULL,
    atom_id          TEXT,
    locator_kind     TEXT NOT NULL,
    locator_start    TEXT NOT NULL,
    locator_end      TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    atom_hash        TEXT NOT NULL DEFAULT '',
    stale            INTEGER NOT NULL DEFAULT 0,
    model_json       TEXT NOT NULL DEFAULT '{}',
    harness_json     TEXT NOT NULL DEFAULT '{}',
    skills_json      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (tenant_id, task_scope_id, evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_task_evidence_session
    ON task_evidence_ranges(tenant_id, task_scope_id, source_scope_id, traj_id);
CREATE INDEX IF NOT EXISTS idx_task_evidence_attempt
    ON task_evidence_ranges(tenant_id, task_scope_id, attempt_id);

CREATE TABLE IF NOT EXISTS task_attempt_relations (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    relation_id      TEXT NOT NULL,
    generation_id    TEXT NOT NULL,
    from_attempt_id  TEXT NOT NULL,
    to_attempt_id    TEXT NOT NULL,
    relation_type    TEXT NOT NULL,
    decision         TEXT NOT NULL,
    confidence       REAL,
    decided_by       TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    observed_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, task_scope_id, relation_id)
);
CREATE INDEX IF NOT EXISTS idx_task_attempt_relations_from
    ON task_attempt_relations(tenant_id, task_scope_id, from_attempt_id);
CREATE INDEX IF NOT EXISTS idx_task_attempt_relations_to
    ON task_attempt_relations(tenant_id, task_scope_id, to_attempt_id);

-- 原 Harness execution usage 是不可由 Atom 重建的 append-only 事实。
CREATE TABLE IF NOT EXISTS execution_usage_events (
    usage_event_id      TEXT PRIMARY KEY,
    usage_plane         TEXT NOT NULL CHECK(usage_plane='execution'),
    source_event_id     TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    task_scope_id       TEXT NOT NULL,
    source_scope_id     TEXT NOT NULL,
    traj_id             TEXT NOT NULL,
    model_json          TEXT NOT NULL DEFAULT '{}',
    harness_json        TEXT NOT NULL DEFAULT '{}',
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    cache_read_tokens   INTEGER,
    cost_usd            REAL,
    measurement_quality TEXT NOT NULL,
    estimation_method   TEXT NOT NULL DEFAULT '',
    unavailable_reason  TEXT NOT NULL DEFAULT '',
    observed_at         TEXT NOT NULL,
    ingested_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_execution_usage_session
    ON execution_usage_events(
        tenant_id, task_scope_id, source_scope_id, traj_id, observed_at
    );

CREATE TABLE IF NOT EXISTS task_usage_allocations (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    allocation_id   TEXT NOT NULL,
    generation_id   TEXT NOT NULL,
    usage_event_id  TEXT NOT NULL,
    usage_plane     TEXT NOT NULL,
    allocation_mode TEXT NOT NULL,
    fraction        REAL NOT NULL,
    task_id         TEXT,
    attempt_id      TEXT,
    processing_step TEXT,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    total_tokens    INTEGER,
    cache_read_tokens INTEGER,
    cost_usd        REAL,
    method          TEXT NOT NULL DEFAULT '',
    method_version  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, task_scope_id, allocation_id)
);
CREATE INDEX IF NOT EXISTS idx_task_usage_task
    ON task_usage_allocations(tenant_id, task_scope_id, task_id, usage_plane);
CREATE INDEX IF NOT EXISTS idx_task_usage_event
    ON task_usage_allocations(usage_plane, usage_event_id);

CREATE TABLE IF NOT EXISTS task_graph_source_state (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    source_scope_id TEXT NOT NULL,
    watch_dir_id     INTEGER NOT NULL,
    traj_id          TEXT NOT NULL,
    source_revision  TEXT NOT NULL,
    generation_id    TEXT NOT NULL,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_scope_id, traj_id)
);
CREATE INDEX IF NOT EXISTS idx_task_source_scope
    ON task_graph_source_state(tenant_id, task_scope_id);

CREATE TABLE IF NOT EXISTS task_graph_dirty_sources (
    watch_dir_id     INTEGER NOT NULL,
    filename         TEXT NOT NULL,
    source_scope_id TEXT,
    tenant_id        TEXT,
    task_scope_id    TEXT,
    deleted          INTEGER NOT NULL DEFAULT 0,
    generation       INTEGER NOT NULL DEFAULT 1,
    reason           TEXT NOT NULL DEFAULT '',
    marked_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (watch_dir_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_task_dirty_marked
    ON task_graph_dirty_sources(marked_at, watch_dir_id, filename);

CREATE TABLE IF NOT EXISTS task_graph_dirty_scopes (
    tenant_id        TEXT NOT NULL,
    task_scope_id    TEXT NOT NULL,
    generation       INTEGER NOT NULL DEFAULT 1,
    reason           TEXT NOT NULL DEFAULT '',
    marked_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tenant_id, task_scope_id)
);
CREATE INDEX IF NOT EXISTS idx_task_dirty_scope_marked
    ON task_graph_dirty_scopes(marked_at, tenant_id, task_scope_id);

-- Task-grounded learning feed. Rows are a durable, coalesced projection of the
-- latest TaskEvidenceBundle for each Task; bundle content remains rebuildable
-- from the immutable Task Graph generation rather than being copied here.
CREATE TABLE IF NOT EXISTS task_evidence_feed (
    tenant_id              TEXT NOT NULL,
    task_scope_id          TEXT NOT NULL,
    task_id                TEXT NOT NULL,
    task_generation_id     TEXT NOT NULL,
    task_evidence_fingerprint TEXT NOT NULL,
    learning_eligibility   TEXT NOT NULL,
    eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
    status                 TEXT NOT NULL
        CHECK(status IN ('pending','processed','fallback','rejected')),
    generation             INTEGER NOT NULL DEFAULT 1,
    marked_at              TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at           TEXT,
    PRIMARY KEY (tenant_id, task_scope_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_task_evidence_feed_status
    ON task_evidence_feed(
        status, marked_at, tenant_id, task_scope_id, task_id
    );
"""

_WATCH_STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_trajectories_watch_status_newest
    ON trajectories(
        watch_dir_id,
        status,
        discovered_at DESC,
        file_mtime DESC,
        id DESC
    )
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """打开（或创建）注册表 DB。schema 过期（含新建）时自动建表 + 迁移。"""
    if db_path is None:
        db_path = get_registry_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_key = db_path.expanduser().resolve()
    conn = connect_with_lock(sqlite3.connect, str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    # ``journal_mode=WAL`` is a persistent database setting, not a per-
    # connection option.  Reissuing the assignment on every telemetry write
    # makes otherwise independent hot connections contend for SQLite's schema
    # lock.  Reading the current mode is lock-free once WAL is active; only a
    # new/legacy database needs the mutating pragma.
    conn.execute("PRAGMA busy_timeout=10000")
    wal_lock = _registry_db_lock(_REGISTRY_WAL_LOCKS, db_key)
    schema_lock = _registry_db_lock(_REGISTRY_SCHEMA_LOCKS, db_key)
    try:
        db_identity = _registry_db_identity(db_key)
        with _REGISTRY_DB_LOCKS_GUARD:
            wal_ready = _REGISTRY_WAL_READY.get(db_key) == db_identity
        if not wal_ready:
            # 新 DB 的多个首请求可能同时打开 delete-mode
            # connection。SQLite 会让这些已打开连接保留旧查询结果，
            # 所以不能只在锁内重查，还要记录本进程已成功设置。
            # 记录文件 inode，同路径 DB 被删除重建时会重新初始化。
            with wal_lock:
                db_identity = _registry_db_identity(db_key)
                with _REGISTRY_DB_LOCKS_GUARD:
                    wal_ready = _REGISTRY_WAL_READY.get(db_key) == db_identity
                if not wal_ready:
                    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    if str(journal_mode).lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL")
                    with _REGISTRY_DB_LOCKS_GUARD:
                        _REGISTRY_WAL_READY[db_key] = _registry_db_identity(db_key)
        conn.execute("PRAGMA foreign_keys=ON")
        # schema 内容指纹存 DB 头：指纹一致则跳过建表/迁移（此函数在热路径高频调用）
        schema_fingerprint = zlib.crc32(
            (_SCHEMA_SQL + _WATCH_STATUS_INDEX_SQL).encode()
        ) & 0x7FFFFFFF
        if conn.execute("PRAGMA user_version").fetchone()[0] != schema_fingerprint:
            # WAL 设置完成不代表 schema 已就绪。首次并发连接的
            # 建表/迁移也必须按 DB 串行，并在锁内重查指纹。
            with schema_lock:
                if conn.execute("PRAGMA user_version").fetchone()[0] != schema_fingerprint:
                    conn.executescript(_SCHEMA_SQL)
                    # Migrate existing DBs that lack new columns
                    _migrate(conn)
                    conn.execute(f"PRAGMA user_version={schema_fingerprint}")
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def pooled_connection(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """线程内复用的 registry 连接；退出时回滚未提交事务，但不 close。

    高频调用点（``record_usage`` 每次 LLM/embedding 调用一次）每次 open/close
    会把连接 finalize 打成热事件，而 finalize 走 ``_SQLITE_CALL_GATE`` 独占侧，
    高负载下整个进程的 SQLite 调用都会 park 在这把门后（futex convoy）。线程内
    复用后 finalize 只发生在线程退出 / DB 重建 / 槽位淘汰这些低频时刻。

    同线程重入（外层还没退出又请求同一 DB）退回一次性连接，保证两层事务互不
    可见；DB 文件被删除重建（rebuild / 测试 tmp 目录）靠 inode 变化识别并重开。
    """
    if db_path is None:
        db_path = get_registry_db_path()
    db_key = db_path.expanduser().resolve()
    slots = getattr(_REGISTRY_THREAD_POOL, "slots", None)
    if slots is None:
        slots = {}
        _REGISTRY_THREAD_POOL.slots = slots
    try:
        identity = _registry_db_identity(db_key)
    except OSError:
        identity = None
    slot = slots.get(db_key)
    if slot is not None and slot.busy:
        connection = get_connection(db_path)
        try:
            yield connection
        finally:
            connection.close()
        return
    if slot is not None and (identity is None or slot.identity != identity):
        slots.pop(db_key, None)
        try:
            slot.conn.close()
        except Exception:
            logger.debug("closing stale pooled connection failed", exc_info=True)
        slot = None
    if slot is None:
        connection = get_connection(db_path)
        slot = SimpleNamespace(conn=connection,
                               identity=_registry_db_identity(db_key),
                               busy=False)
        while len(slots) >= _REGISTRY_THREAD_POOL_CAPACITY:
            _oldest_key, oldest_slot = next(iter(slots.items()))
            slots.pop(_oldest_key)
            try:
                oldest_slot.conn.close()
            except Exception:
                logger.debug("evicting pooled connection failed", exc_info=True)
        slots[db_key] = slot
    else:
        # 手动 LRU：命中的槽位挪到 dict 尾部，淘汰永远从头部取最旧
        slots.pop(db_key)
        slots[db_key] = slot
    slot.busy = True
    try:
        yield slot.conn
        if slot.conn.in_transaction:
            slot.conn.rollback()
    except Exception:
        try:
            if slot.conn.in_transaction:
                slot.conn.rollback()
        except Exception:
            slots.pop(db_key, None)
            try:
                slot.conn.close()
            except Exception:
                logger.debug("closing broken pooled connection failed",
                             exc_info=True)
        raise
    finally:
        slot.busy = False


def _migrate_atom_candidate_pending(conn: sqlite3.Connection) -> None:
    """Replace the legacy key and invalidate stale projection readiness."""
    table_info = conn.execute(
        "PRAGMA table_info(atom_candidate_pending)",
    ).fetchall()
    primary_key = [
        row[1]
        for row in sorted(table_info, key=lambda row: row[5])
        if row[5]
    ]
    if primary_key == ["atom_id", "skill"]:
        return

    required_columns = {"atom_id", "skill", "weightscore", "updated_at"}
    actual_columns = {row[1] for row in table_info}
    if not required_columns.issubset(actual_columns):
        raise sqlite3.DatabaseError(
            "cannot migrate atom_candidate_pending with columns "
            f"{sorted(actual_columns)!r}",
        )

    conn.execute("SAVEPOINT migrate_atom_candidate_pending")
    try:
        conn.execute(
            """
            CREATE TABLE atom_candidate_pending_v2 (
                atom_id     TEXT NOT NULL,
                skill       TEXT NOT NULL,
                weightscore INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (atom_id, skill)
            )
            """,
        )
        conn.execute(
            """
            INSERT INTO atom_candidate_pending_v2(
                atom_id, skill, weightscore, updated_at
            )
            SELECT atom_id, skill, weightscore, updated_at
            FROM atom_candidate_pending
            """,
        )
        conn.execute("DROP TABLE atom_candidate_pending")
        conn.execute(
            "ALTER TABLE atom_candidate_pending_v2 "
            "RENAME TO atom_candidate_pending",
        )
        conn.execute(
            "CREATE INDEX idx_acp_skill ON atom_candidate_pending(skill)",
        )
        # The legacy table retained at most one skill for each Atom.  Force a
        # reconcile from the authoritative .candidates.yml files so cached
        # root/mtime markers cannot hide associations lost before migration.
        conn.execute("DELETE FROM atom_candidate_pending_meta")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT migrate_atom_candidate_pending")
        conn.execute("RELEASE SAVEPOINT migrate_atom_candidate_pending")
        raise
    conn.execute("RELEASE SAVEPOINT migrate_atom_candidate_pending")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from older schema versions."""
    # ── trajectories ──
    cur = conn.execute("PRAGMA table_info(trajectories)")
    cols = {row[1] for row in cur.fetchall()}
    # status 列是否本次才补上——决定要不要跑下方那条历史状态回填(只该一次性)。
    status_was_missing = "status" not in cols
    discovered_at_was_missing = "discovered_at" not in cols
    migrations = [
        ("status", "TEXT DEFAULT 'discovered'"),
        ("process_action", "TEXT"),
        ("interest_fingerprint", "TEXT"),
        ("skill_generated", "TEXT"),
        ("ux_score", "REAL"),
        ("error_msg", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("file_mtime", "REAL DEFAULT 0"),
        # SQLite 的 ADD COLUMN 不接受 CURRENT_TIMESTAMP 等非恒定默认值。
        # 旧表先补普通 TEXT 列、下方回填；新写入则显式写 discovered_at。
        ("discovered_at", "TEXT"),
        ("updated_at", "TEXT"),
        ("process_log", "TEXT"),
        # v2: AtomTask 流水线状态
        ("tasks_extracted", "INTEGER DEFAULT 0"),
        ("last_offset", "INTEGER DEFAULT 0"),
        ("last_atom_id", "TEXT"),
        # 用户 agent 模型(批2,Issue #43 关联):discover 时从 .json sidecar 写入
        ("source_model", "TEXT"),
        # 用户 coding agent(harness):discover 时从 .json sidecar 的 harness 写入。
        # team server 据此按真实 coding agent 分组,替代把所有上传一律标 team_client。
        ("source_harness", "TEXT"),
        # P2-2.1 归因地基(D5):canonical 身份键=user_name。team_client 桶
        # discover 时从 watch_dirs.label(=sessions 桶目录名)写入;存量用
        # scripts/backfill_user_key.py 一次性回填。非 team 目录留空。
        ("user_key", "TEXT DEFAULT ''"),
    ]
    for col, typedef in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE trajectories ADD COLUMN {col} {typedef}")
    if discovered_at_was_missing:
        conn.execute(
            "UPDATE trajectories SET discovered_at=datetime('now')"
            " WHERE discovered_at IS NULL"
        )
    # ── watch_dirs ──
    # ── recommendation_log ──（审计 P0-2：曝光去重根治注水）
    # 加 sha 列 + (client_id,skill,side,sha) 唯一索引；建索引前一次性清历史重复行
    # （每次 sync 每 slot 插一条的注水数据），只保留每组最早一条（保住首次曝光时间）。
    cur = conn.execute("PRAGMA table_info(recommendation_log)")
    reco_cols = {row[1] for row in cur.fetchall()}
    if "sha" not in reco_cols:
        conn.execute("ALTER TABLE recommendation_log ADD COLUMN sha TEXT DEFAULT ''")
    has_dedup_idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_reco_dedup'"
    ).fetchone()
    if not has_dedup_idx:
        conn.execute(
            "DELETE FROM recommendation_log WHERE id NOT IN ("
            " SELECT MIN(id) FROM recommendation_log"
            " GROUP BY client_id, skill, side, COALESCE(sha,''))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_reco_dedup ON"
            " recommendation_log(client_id, skill, side, sha)"
        )

    # ── canary_decision ──（D9：裁决可定位到 commit——进化图数据地基）
    cur = conn.execute("PRAGMA table_info(canary_decision)")
    cd_cols = {row[1] for row in cur.fetchall()}
    for col in ("main_sha", "staging_sha"):
        if col not in cd_cols:
            conn.execute(
                f"ALTER TABLE canary_decision ADD COLUMN {col} TEXT DEFAULT ''")

    cur = conn.execute("PRAGMA table_info(watch_dirs)")
    wd_cols = {row[1] for row in cur.fetchall()}
    if "ecosystem" not in wd_cols:
        conn.execute(
            "ALTER TABLE watch_dirs ADD COLUMN ecosystem TEXT DEFAULT 'manual'"
        )
        # 已有行历史上都是用户手动 register，标 'manual'
        conn.execute("UPDATE watch_dirs SET ecosystem='manual' WHERE ecosystem IS NULL")
    if "source_scope_id" not in wd_cols:
        conn.execute("ALTER TABLE watch_dirs ADD COLUMN source_scope_id TEXT")
    missing_source_scopes = conn.execute(
        "SELECT id, path FROM watch_dirs"
        " WHERE source_scope_id IS NULL OR source_scope_id=''"
    ).fetchall()
    for watch_dir_row in missing_source_scopes:
        conn.execute(
            "UPDATE watch_dirs SET source_scope_id=? WHERE id=?",
            (f"src_{uuid.uuid4().hex}", watch_dir_row["id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_dirs_source_scope"
        " ON watch_dirs(source_scope_id)"
        " WHERE source_scope_id IS NOT NULL AND source_scope_id != ''"
    )

    # Logical Task usage identity.  Historical rows intentionally retain their
    # original numeric values without inventing measurement quality or scope.
    cur = conn.execute("PRAGMA table_info(llm_usage)")
    usage_cols = {row[1] for row in cur.fetchall()}
    usage_migrations = [
        ("usage_event_id", "TEXT"),
        ("usage_plane", "TEXT DEFAULT 'xskill_processing'"),
        ("measurement_quality", "TEXT"),
        ("allocation_mode", "TEXT DEFAULT 'unattributed'"),
        ("estimation_method", "TEXT"),
        ("unavailable_reason", "TEXT"),
        ("tenant_id", "TEXT"),
        ("task_scope_id", "TEXT"),
        ("source_scope_id", "TEXT"),
        ("traj_id", "TEXT"),
        ("atom_id", "TEXT"),
        ("generation_id", "TEXT"),
        ("legacy", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for column_name, column_type in usage_migrations:
        if column_name not in usage_cols:
            conn.execute(
                f"ALTER TABLE llm_usage ADD COLUMN {column_name} {column_type}"
            )
    conn.execute(
        "UPDATE llm_usage SET usage_event_id='xsp_legacy_' || id,"
        " usage_plane='xskill_processing', allocation_mode='unattributed',"
        " legacy=1 WHERE usage_event_id IS NULL OR usage_event_id=''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_usage_event"
        " ON llm_usage(usage_event_id) WHERE usage_event_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_scope"
        " ON llm_usage(tenant_id, task_scope_id, usage_plane)"
    )
    task_usage_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(task_usage_allocations)"
        ).fetchall()
    }
    if "cache_read_tokens" not in task_usage_columns:
        conn.execute(
            "ALTER TABLE task_usage_allocations"
            " ADD COLUMN cache_read_tokens INTEGER"
        )
    task_evidence_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(task_evidence_ranges)"
        ).fetchall()
    }
    if "atom_hash" not in task_evidence_columns:
        conn.execute(
            "ALTER TABLE task_evidence_ranges"
            " ADD COLUMN atom_hash TEXT NOT NULL DEFAULT ''"
        )
    execution_usage_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(execution_usage_events)"
        ).fetchall()
    }
    if "estimation_method" not in execution_usage_columns:
        conn.execute(
            "ALTER TABLE execution_usage_events"
            " ADD COLUMN estimation_method TEXT NOT NULL DEFAULT ''"
        )
    task_generation_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(task_graph_generations)"
        ).fetchall()
    }
    if "generator_json" not in task_generation_columns:
        conn.execute(
            "ALTER TABLE task_graph_generations"
            " ADD COLUMN generator_json TEXT NOT NULL DEFAULT '{}'"
        )

    # skills_catalog.content_sha：与 Milvus 向量索引对齐的一致性键
    cur = conn.execute("PRAGMA table_info(skills_catalog)")
    sc_cols = {row[1] for row in cur.fetchall()}
    if "content_sha" not in sc_cols:
        conn.execute(
            "ALTER TABLE skills_catalog ADD COLUMN content_sha TEXT NOT NULL DEFAULT ''"
        )

    # skills_catalog_meta.generation：Cluster 路由表的跨进程失效键。必须先补列，
    # 后续 catalog 写出口才能在同一事务内 bump；旧库从 0 起步，下一次目录
    # 语义变化会推进版本。
    cur = conn.execute("PRAGMA table_info(skills_catalog_meta)")
    scm_cols = {row[1] for row in cur.fetchall()}
    if "generation" not in scm_cols:
        conn.execute(
            "ALTER TABLE skills_catalog_meta"
            " ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
        )

    # skill_prefs.side：pin 时可钉灰度侧（空=自动分流）
    cur = conn.execute("PRAGMA table_info(skill_prefs)")
    pref_cols = {row[1] for row in cur.fetchall()}
    if "side" not in pref_cols:
        conn.execute(
            "ALTER TABLE skill_prefs ADD COLUMN side TEXT NOT NULL DEFAULT ''"
        )

    # catalog_vector_sync_meta.sweep_seeded_fingerprint：全量对账「已播种但
    # 还没追平」的目标指纹（issue #328 review）。只在这里非空时代表某个
    # bootstrap/model_changed/periodic/ephemeral 触发的全量播种正在进行中；
    # 不能只看 catalog_vector_dirty 是否为空来判断——那张表也会因为普通的
    # catalog 编辑产生脏项，和「已经播种过」是两回事，混用会导致播种被
    # 有机脏项误判为「已经播种」而永久跳过，见 review 复现。
    cur = conn.execute("PRAGMA table_info(catalog_vector_sync_meta)")
    cvsm_cols = {row[1] for row in cur.fetchall()}
    if "sweep_seeded_fingerprint" not in cvsm_cols:
        conn.execute(
            "ALTER TABLE catalog_vector_sync_meta"
            " ADD COLUMN sweep_seeded_fingerprint TEXT NOT NULL DEFAULT ''"
        )

    _migrate_atom_candidate_pending(conn)

    # Backfill status from has_meta/has_embedding —— **只在首次补 status 列时跑一次**。
    # 以前每次 get_connection 都跑这条,会把任何 status='discovered' 的**活行**
    # （rebuild 重置 / error 重试 / 僵尸清理刚翻回的）在下次连接时打回 'indexed'，
    # 导致 watcher 永不重拆（0 atom/0 skill 的真凶,见 test_rebuild_resplit_repro）。
    # 真·一次性迁移只该在那个加列的连接里跑,之后 status 是权威状态,不能再覆盖。
    if status_was_missing:
        conn.execute(
            "UPDATE trajectories SET status='indexed'"
            " WHERE has_embedding=1 AND (status IS NULL OR status='discovered')"
        )
        conn.execute(
            "UPDATE trajectories SET status='meta_done'"
            " WHERE has_meta=1 AND has_embedding=0 AND (status IS NULL OR status='discovered')"
        )
    # 旧表先补齐并回填 status / discovered_at / file_mtime，再一次性建索引，
    # 避免历史状态更新额外维护刚创建的索引。
    conn.execute(_WATCH_STATUS_INDEX_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# LLM usage / cost accounting  (Issue #43)  —— 唯一"无家可归"数据的持久化
# ---------------------------------------------------------------------------

def record_usage(*, step: str, model: str, prompt: Optional[int],
                 completion: Optional[int], total: Optional[int],
                 cost_usd: Optional[float], price_source: str,
                 usage_event_id: Optional[str] = None,
                 measurement_quality: str = "measured",
                 allocation_mode: str = "unattributed",
                 estimation_method: Optional[str] = None,
                 unavailable_reason: Optional[str] = None,
                 tenant_id: Optional[str] = None,
                 task_scope_id: Optional[str] = None,
                 source_scope_id: Optional[str] = None,
                 traj_id: Optional[str] = None,
                 atom_id: Optional[str] = None,
                 generation_id: Optional[str] = None,
                 db_path: Optional[Path] = None) -> str:
    """追加一条 LLM/embedding 调用的 token+成本记录。旁路 telemetry。"""
    if measurement_quality not in ("measured", "estimated", "unavailable"):
        raise ValueError("invalid measurement_quality")
    if allocation_mode not in ("direct", "shared", "unattributed"):
        raise ValueError("invalid allocation_mode")
    if usage_event_id is None:
        event_id = f"xsp_{uuid.uuid4().hex}"
    elif not isinstance(usage_event_id, str) or not usage_event_id.strip():
        raise ValueError("usage_event_id must be a non-empty string")
    else:
        event_id = usage_event_id.strip()
    if len(event_id) > 200:
        raise ValueError("usage_event_id is limited to 200 characters")

    def non_negative_number(value, name: str, *, integer: bool):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{name} must be numeric or None")
        if integer:
            if not isinstance(value, int):
                raise ValueError(f"{name} must be an integer or None")
            normalized = value
        else:
            if not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric or None")
            normalized = float(value)
        if not math.isfinite(float(normalized)) or normalized < 0:
            raise ValueError(f"{name} must be non-negative")
        return normalized

    prompt_value = non_negative_number(prompt, "prompt", integer=True)
    completion_value = non_negative_number(
        completion, "completion", integer=True,
    )
    total_value = non_negative_number(total, "total", integer=True)
    cost_value = non_negative_number(cost_usd, "cost_usd", integer=False)
    if measurement_quality == "estimated" and not str(
        estimation_method or ""
    ).strip():
        raise ValueError("estimated usage requires estimation_method")
    if measurement_quality == "unavailable":
        if not str(unavailable_reason or "").strip():
            raise ValueError("unavailable usage requires unavailable_reason")
        prompt_value = completion_value = total_value = cost_value = None
    elif all(
        value is None
        for value in (
            prompt_value, completion_value, total_value, cost_value,
        )
    ):
        raise ValueError("measured or estimated usage requires a numeric value")
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO llm_usage("
            "step,model,prompt,completion,total,cost_usd,price_source,"
            "usage_event_id,usage_plane,measurement_quality,allocation_mode,"
            "estimation_method,unavailable_reason,tenant_id,task_scope_id,"
            "source_scope_id,traj_id,atom_id,generation_id,legacy)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                step, model, prompt_value, completion_value, total_value,
                cost_value, price_source, event_id, "xskill_processing",
                measurement_quality, allocation_mode, estimation_method,
                unavailable_reason, tenant_id, task_scope_id, source_scope_id,
                traj_id, atom_id, generation_id,
            ),
        )
        conn.commit()
    return event_id


# ---------------------------------------------------------------------------
# #106 画像散点物化缓存 —— 事件触发重算写入，scatter 端点只读命中
# ---------------------------------------------------------------------------

def read_scatter_cache(user_key: str, method: str, *,
                       db_path: Optional[Path] = None) -> Optional[dict]:
    """返回某用户某算法的散点缓存 ``{payload, fingerprint, computed_at}``；无行 → None。"""
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT payload, fingerprint, computed_at FROM scatter_cache"
            " WHERE user_key=? AND method=?",
            (user_key, method),
        ).fetchone()
        if row is None:
            return None
        return {"payload": row["payload"], "fingerprint": row["fingerprint"],
                "computed_at": row["computed_at"]}


def write_scatter_cache(user_key: str, method: str, fingerprint: str,
                        payload: dict, *,
                        db_path: Optional[Path] = None) -> None:
    """物化一条散点坐标包（``payload`` dict → JSON 存储），覆盖旧值并刷新 computed_at。"""
    payload_json = json.dumps(payload, ensure_ascii=False)
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO scatter_cache(user_key,method,fingerprint,payload)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(user_key,method) DO UPDATE SET"
            " fingerprint=excluded.fingerprint, payload=excluded.payload,"
            " computed_at=datetime('now')",
            (user_key, method, fingerprint, payload_json),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 埋点(instrumentation)：三类事件的记录 + 聚合，供看板算衍生率
# 记录函数走旁路 telemetry——调用点用 try/except 包，记录失败绝不阻断管线。
# ---------------------------------------------------------------------------

GLOBAL_PREF_KEY = "*global*"


class PinQuotaExceeded(ValueError):
    """pinned 总量(用户+全局)超 slot 上限——写入侧拒绝(D8)。"""


def set_skill_pref(*, user_key: str, skill_name: str, pref: str, set_by: str,
                   max_pinned: Optional[int] = None,
                   side: Optional[str] = None,
                   db_path: Optional[Path] = None) -> None:
    """写入/覆盖一条偏好(pinned|blocked)。

    D8:``max_pinned`` 给定且 pref='pinned' 时在**写入侧**校验——该用户
    pinned + 全局 pinned 合计(去重,含本次)不得超过 max_pinned,超量抛
    ``PinQuotaExceeded``,超量状态根本不可能入库;全局 pin 则对**全员**
    逐一校验合计。sync 读路径永远不需要处理该错误。

    ``side``: 仅 pin 有意义；``None``=更新时保留原 side / 新建为空；
    ``''``=清除 side 覆盖；``main``|``staging``=钉到该侧。blocked 写入时
    强制清空 side。
    """
    if pref not in ("pinned", "blocked"):
        raise ValueError(f"pref 必须是 pinned|blocked,得到 {pref!r}")
    if not user_key or not skill_name:
        raise ValueError("user_key/skill_name 不能为空")
    if side is not None and side not in ("", "main", "staging"):
        raise ValueError(f"side 必须是 main|staging|空串,得到 {side!r}")
    with pooled_connection(db_path) as conn:
        if pref == "pinned" and max_pinned is not None:
            _check_pin_quota(conn, user_key=user_key, skill_name=skill_name,
                             max_pinned=max_pinned)
        if pref == "blocked":
            side_val = ""
            update_side = True
        elif side is None:
            side_val = ""
            update_side = False
        else:
            side_val = side
            update_side = True
        if update_side:
            conn.execute(
                "INSERT INTO skill_prefs(user_key,skill_name,pref,set_by,side)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_key,skill_name)"
                " DO UPDATE SET pref=excluded.pref, set_by=excluded.set_by,"
                "               side=excluded.side, ts=datetime('now')",
                (user_key, skill_name, pref, set_by, side_val),
            )
        else:
            conn.execute(
                "INSERT INTO skill_prefs(user_key,skill_name,pref,set_by,side)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_key,skill_name)"
                " DO UPDATE SET pref=excluded.pref, set_by=excluded.set_by,"
                "               ts=datetime('now')",
                (user_key, skill_name, pref, set_by, side_val),
            )
        conn.commit()
    try:
        from xskill.recommend.recommend_store import mark_recommend_dirty

        mark_recommend_dirty(user_key, reason=f"pref_{pref}", db_path=db_path)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("mark recommend dirty after pref failed", exc_info=True)


def clear_skill_pref_side(*, user_key: str, skill_name: str,
                          db_path: Optional[Path] = None) -> bool:
    """清除 pin 的 side 覆盖（保留 pin）。无 pinned 行 → False。"""
    with pooled_connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE skill_prefs SET side='', ts=datetime('now')"
            " WHERE user_key=? AND skill_name=? AND pref='pinned'",
            (user_key, skill_name),
        )
        conn.commit()
        cleared = cur.rowcount > 0
    if cleared:
        try:
            from xskill.recommend.recommend_store import mark_recommend_dirty

            mark_recommend_dirty(
                user_key, reason="pref_side_cleared", db_path=db_path)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug(
                "mark recommend dirty after clear side failed", exc_info=True)
    return cleared


def _check_pin_quota(conn, *, user_key: str, skill_name: str,
                     max_pinned: int) -> None:
    """pinned 配额校验(含本次写入)。全局 pin 对全员合计逐一校验。"""
    def pinned_set(key: str) -> set:
        return {r["skill_name"] for r in conn.execute(
            "SELECT skill_name FROM skill_prefs WHERE user_key=? AND pref='pinned'",
            (key,),
        ).fetchall()}

    global_pinned = pinned_set(GLOBAL_PREF_KEY)
    if user_key == GLOBAL_PREF_KEY:
        global_pinned = global_pinned | {skill_name}
        user_keys = [r["user_key"] for r in conn.execute(
            "SELECT DISTINCT user_key FROM skill_prefs WHERE user_key!=?",
            (GLOBAL_PREF_KEY,),
        ).fetchall()]
        for uk in user_keys + [None]:  # None=只有全局 pin 的裸用户基线
            merged = global_pinned | (pinned_set(uk) if uk else set())
            if len(merged) > max_pinned:
                raise PinQuotaExceeded(
                    f"全局 pin {skill_name!r} 会使用户 {uk or '(baseline)'} 的"
                    f" pinned 合计 {len(merged)} 超过 slot 上限 {max_pinned}")
    else:
        merged = global_pinned | pinned_set(user_key) | {skill_name}
        if len(merged) > max_pinned:
            raise PinQuotaExceeded(
                f"pin {skill_name!r} 会使 pinned 合计(含全局) {len(merged)}"
                f" 超过 slot 上限 {max_pinned}")


def clear_skill_pref(*, user_key: str, skill_name: str,
                     db_path: Optional[Path] = None) -> bool:
    """删除一条偏好(pin 取消/屏蔽恢复)。返回是否真的删了行。"""
    with pooled_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM skill_prefs WHERE user_key=? AND skill_name=?",
            (user_key, skill_name),
        )
        conn.commit()
        deleted = cur.rowcount > 0
    if deleted:
        try:
            from xskill.recommend.recommend_store import mark_recommend_dirty

            mark_recommend_dirty(user_key, reason="pref_cleared", db_path=db_path)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("mark recommend dirty after clear pref failed", exc_info=True)
    return deleted


_SKILL_ORIGIN_SOURCES = frozenset({"import", "generate"})
_AUTO_CANARY_TTL_SEC = 5.0
_auto_canary_cache_lock = threading.Lock()
_auto_canary_cache: dict[str, tuple[float, dict[str, set[str]]]] = {}


def _auto_canary_cache_key(db_path: Optional[Path]) -> str:
    path = db_path if db_path is not None else get_registry_db_path()
    return str(Path(path).expanduser().resolve())


def clear_auto_canary_cache_for_tests() -> None:
    with _auto_canary_cache_lock:
        _auto_canary_cache.clear()


def record_skill_origin(
    *,
    skill_name: str,
    user_key: str,
    source: str,
    db_path: Optional[Path] = None,
) -> bool:
    """记录纳入或 generate 发起人。首次写入生效，后写忽略。"""
    skill_name = (skill_name or "").strip()
    user_key = (user_key or "").strip()
    source = (source or "").strip()
    if not skill_name or not user_key or source not in _SKILL_ORIGIN_SOURCES:
        return False
    with pooled_connection(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO skill_origin(skill_name, user_key, source)"
            " VALUES(?,?,?)",
            (skill_name, user_key, source),
        )
        conn.commit()
        inserted = cur.rowcount > 0
    if inserted:
        clear_auto_canary_cache_for_tests()
    return inserted


def skill_origin_user(skill_name: str, *,
                      db_path: Optional[Path] = None) -> Optional[str]:
    skill_name = (skill_name or "").strip()
    if not skill_name:
        return None
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_key FROM skill_origin WHERE skill_name=?",
            (skill_name,),
        ).fetchone()
    user = (row["user_key"] or "").strip() if row else ""
    return user or None


def _traj_id_from_ux_row(traj_id: str, atom_id: str) -> str:
    tid = (traj_id or "").strip()
    if tid:
        return tid
    atom = atom_id or ""
    if not atom.startswith("atom_"):
        return ""
    body = atom[5:]
    idx = body.rfind("_")
    return body[:idx] if idx > 0 else ""


def _build_auto_canary_users(db_path: Optional[Path]) -> dict[str, set[str]]:
    """skill_name → {纳入/生成发起人, 体验分用量最多的用户}。"""
    with pooled_connection(db_path) as conn:
        origin_rows = conn.execute(
            "SELECT skill_name, user_key FROM skill_origin",
        ).fetchall()
        traj_rows = conn.execute(
            "SELECT filename, user_key FROM trajectories WHERE user_key!=''",
        ).fetchall()
        ux_rows = conn.execute(
            "SELECT skill_name, traj_id, atom_id FROM ux_scores",
        ).fetchall()
    out: dict[str, set[str]] = {}
    for row in origin_rows:
        skill = (row["skill_name"] or "").strip()
        user = (row["user_key"] or "").strip()
        if skill and user:
            out.setdefault(skill, set()).add(user)
    traj_user: dict[str, str] = {}
    for row in traj_rows:
        fn = row["filename"] or ""
        stem = fn[:-3] if fn.endswith(".md") else fn
        user = (row["user_key"] or "").strip()
        if stem and user:
            traj_user[stem] = user
    usage: dict[str, dict[str, int]] = {}
    for row in ux_rows:
        skill = (row["skill_name"] or "").strip()
        traj = _traj_id_from_ux_row(row["traj_id"] or "", row["atom_id"] or "")
        user = traj_user.get(traj, "")
        if not skill or not user:
            continue
        bucket = usage.setdefault(skill, {})
        bucket[user] = bucket.get(user, 0) + 1
    for skill, by_user in usage.items():
        ranked = sorted(by_user.items(), key=lambda kv: (-kv[1], kv[0]))
        if ranked:
            out.setdefault(skill, set()).add(ranked[0][0])
    return out


def auto_canary_users_by_skill(
    *, db_path: Optional[Path] = None,
) -> dict[str, set[str]]:
    """短 TTL 缓存：每个 skill 的自动灰度对象集合。"""
    key = _auto_canary_cache_key(db_path)
    now = time.monotonic()
    with _auto_canary_cache_lock:
        hit = _auto_canary_cache.get(key)
        if hit is not None and now - hit[0] < _AUTO_CANARY_TTL_SEC:
            return {name: set(users) for name, users in hit[1].items()}
    built = _build_auto_canary_users(db_path)
    with _auto_canary_cache_lock:
        _auto_canary_cache[key] = (now, built)
    return {name: set(users) for name, users in built.items()}


def auto_canary_users(skill_name: str, *,
                      db_path: Optional[Path] = None) -> set[str]:
    skill_name = (skill_name or "").strip()
    if not skill_name:
        return set()
    return auto_canary_users_by_skill(db_path=db_path).get(skill_name, set())


def is_auto_canary_user(user_key: str, skill_name: str, *,
                        db_path: Optional[Path] = None) -> bool:
    user_key = (user_key or "").strip()
    if not user_key:
        return False
    return user_key in auto_canary_users(skill_name, db_path=db_path)


def prefs_for(user_key: str, *, db_path: Optional[Path] = None) -> list[dict]:
    """某 user_key(或 '*global*')的全部偏好行。"""
    with pooled_connection(db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_key, skill_name, pref, set_by, side, ts FROM skill_prefs"
            " WHERE user_key=? ORDER BY ts",
            (user_key,),
        ).fetchall()]


def get_client_take_n(user_key: str, *, default: int,
                      db_path: Optional[Path] = None) -> int:
    """读用户 client 截取安装数；无行或 NULL → ``default``（通常=服务器 skill_slots）。"""
    if not user_key:
        return max(0, int(default))
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT take_n FROM user_client_settings WHERE user_key=?",
            (user_key,),
        ).fetchone()
    if row is None or row["take_n"] is None:
        return max(0, int(default))
    return max(0, int(row["take_n"]))


def set_client_take_n(user_key: str, take_n: int, *, max_n: int,
                      db_path: Optional[Path] = None) -> int:
    """写入 take_n，夹取到 ``[0, max_n]``，返回落盘值。"""
    if not user_key:
        raise ValueError("user_key required")
    n = max(0, min(int(max_n), int(take_n)))
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO user_client_settings(user_key, take_n, updated_at)"
            " VALUES(?,?,datetime('now'))"
            " ON CONFLICT(user_key) DO UPDATE SET"
            " take_n=excluded.take_n, updated_at=datetime('now')",
            (user_key, n),
        )
        conn.commit()
    return n


def effective_prefs(user_key: str, *, db_path: Optional[Path] = None) -> dict:
    """manifest 注入用的合并视图:{'pinned': [...有序...], 'blocked': set,
    'pin_meta': {skill: set_by}, 'side': {skill: main|staging}}。

    合并规则:全局行先于用户行(admin 全局 pin 排最前);同一 skill 用户行
    与全局行冲突时,**blocked 优先**(任何一侧屏蔽即不分发——全局屏蔽用户
    不能自行恢复;用户自己屏蔽的全局推荐仅对自己生效)。
    side 覆盖同序合并：后写的用户行覆盖全局；用户 pin 且 side 为空则清除
    该 skill 的 side 覆盖。
    """
    with pooled_connection(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT user_key, skill_name, pref, set_by, side, ts FROM skill_prefs"
            " WHERE user_key IN (?, ?) ORDER BY CASE user_key WHEN ? THEN 0"
            " ELSE 1 END, ts",
            (GLOBAL_PREF_KEY, user_key, GLOBAL_PREF_KEY),
        ).fetchall()]
    blocked = {r["skill_name"] for r in rows if r["pref"] == "blocked"}
    pinned: list[str] = []
    pin_meta: dict = {}
    side_map: dict[str, str] = {}
    for r in rows:
        name = r["skill_name"]
        if r["pref"] == "pinned" and name not in blocked:
            if name not in pinned:
                pinned.append(name)
            pin_meta[name] = {
                "set_by": r["set_by"],
                "scope": "global" if r["user_key"] == GLOBAL_PREF_KEY else "user",
            }
            ov = (r.get("side") or "").strip()
            if ov in ("main", "staging"):
                side_map[name] = ov
            elif r["user_key"] != GLOBAL_PREF_KEY:
                side_map.pop(name, None)
    return {
        "pinned": pinned, "blocked": blocked,
        "pin_meta": pin_meta, "side": side_map,
    }


def manifest_control_plane_snapshot(
    *, db_path: Optional[Path] = None,
) -> dict:
    """一次查询取得 manifest 所需的全部偏好和下线状态。"""
    with pooled_connection(db_path) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT user_key, skill_name, pref, set_by, side, ts FROM skill_prefs"
            " ORDER BY CASE user_key WHEN ? THEN 0 ELSE 1 END, ts",
            (GLOBAL_PREF_KEY,),
        ).fetchall()]
        retired = {
            row["skill_name"] for row in conn.execute(
                "SELECT skill_name FROM skill_lifecycle WHERE state='retired'"
            ).fetchall()
        }
    return {"prefs": rows, "retired": retired}


def effective_prefs_from_snapshot(snapshot: dict, user_key: str) -> dict:
    """从控制面快照计算单个用户的全局+个人偏好合并视图。"""
    rows = [
        row for row in snapshot.get("prefs", [])
        if row["user_key"] in (GLOBAL_PREF_KEY, user_key)
    ]
    blocked = {row["skill_name"] for row in rows if row["pref"] == "blocked"}
    pinned: list[str] = []
    pin_meta: dict = {}
    for row in rows:
        skill_name = row["skill_name"]
        if (
            row["pref"] == "pinned"
            and skill_name not in blocked
            and skill_name not in pinned
        ):
            pinned.append(skill_name)
            pin_meta[skill_name] = {
                "set_by": row["set_by"],
                "scope": (
                    "global" if row["user_key"] == GLOBAL_PREF_KEY else "user"
                ),
            }
    return {"pinned": pinned, "blocked": blocked, "pin_meta": pin_meta}


# ---------------------------------------------------------------------------
# P2-2.4c skill 生命周期
# ---------------------------------------------------------------------------

def retire_skill(*, skill_name: str, set_by: str,
                 db_path: Optional[Path] = None) -> None:
    """下线:停止分发与推荐,数据与 git 历史保留。幂等。"""
    with pooled_connection(db_path) as conn:
        already_retired = conn.execute(
            "SELECT 1 FROM skill_lifecycle WHERE skill_name=? AND state='retired'",
            (skill_name,),
        ).fetchone() is not None
        conn.execute(
            "INSERT INTO skill_lifecycle(skill_name,state,set_by) VALUES(?,?,?)"
            " ON CONFLICT(skill_name) DO UPDATE SET state='retired',"
            " set_by=excluded.set_by, ts=datetime('now')",
            (skill_name, "retired", set_by),
        )
        if not already_retired:
            rows = conn.execute(
                "SELECT catalog_key FROM skills_catalog WHERE name=?",
                (skill_name,),
            ).fetchall()
            from xskill.recommend.vector_dirty import (
                mark_catalog_vector_dirty_on_connection,
            )
            for row in rows:
                mark_catalog_vector_dirty_on_connection(
                    conn, row["catalog_key"], operation="delete",
                )
        conn.commit()


def unretire_skill(*, skill_name: str, db_path: Optional[Path] = None) -> bool:
    """恢复在役。返回是否真的有行被删。"""
    with pooled_connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM skill_lifecycle WHERE skill_name=?", (skill_name,))
        if cur.rowcount > 0:
            from xskill.recommend.skill_vector_store import (
                catalog_row_is_indexable,
            )
            from xskill.recommend.vector_dirty import (
                mark_catalog_vector_dirty_on_connection,
            )
            rows = conn.execute(
                """
                SELECT catalog_key, name, source, description, content_sha,
                       distributable
                FROM skills_catalog WHERE name=?
                """,
                (skill_name,),
            ).fetchall()
            for row in rows:
                stored = dict(row)
                if catalog_row_is_indexable(stored):
                    mark_catalog_vector_dirty_on_connection(
                        conn,
                        row["catalog_key"],
                        operation="upsert",
                        content_sha=row["content_sha"] or "",
                    )
        conn.commit()
        return cur.rowcount > 0


def retired_skills(*, db_path: Optional[Path] = None) -> set:
    with pooled_connection(db_path) as conn:
        return {r["skill_name"] for r in conn.execute(
            "SELECT skill_name FROM skill_lifecycle WHERE state='retired'"
        ).fetchall()}


def purge_skill_records(*, skill_name: str,
                        db_path: Optional[Path] = None) -> None:
    """删除 skill 时清掉它的 prefs/lifecycle 行——"删后同名 skill 再生"从
    零开始,不继承旧 pin/屏蔽/下线状态(语义拍板,见 tasks.md 2.4c)。
    评分/推荐等历史埋点(recommendation_log 等)保留作审计。"""
    with pooled_connection(db_path) as conn:
        conn.execute("DELETE FROM skill_prefs WHERE skill_name=?", (skill_name,))
        conn.execute("DELETE FROM skill_lifecycle WHERE skill_name=?", (skill_name,))
        conn.commit()


def record_recommendation(*, client_id: str, skill: str, side: str, bucket: str,
                          sha: str = "", db_path: Optional[Path] = None) -> None:
    """记一次"把 skill(某版本)推荐给某用户"——曝光事件。

    OR IGNORE 命中唯一索引 (client_id,skill,side,sha)：同一曝光对只记首次，
    反复 sync 不再膨胀触发率分母（审计 P0-2）。
    """
    record_recommendations(
        client_id=client_id,
        records=[(skill, side, bucket, sha)],
        db_path=db_path,
    )


def record_recommendations(
    *,
    client_id: str,
    records: list[tuple[str, str, str, str]],
    db_path: Optional[Path] = None,
) -> None:
    """在一个事务中记录同一用户的一批推荐曝光。"""
    if not records:
        return
    with pooled_connection(db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO recommendation_log(client_id,skill,side,bucket,sha)"
            " VALUES(?,?,?,?,?)",
            [(client_id, skill, side, bucket, sha or "")
             for skill, side, bucket, sha in records],
        )
        conn.commit()


def record_atom_adoption(*, atom_id: str, skill: str, weightscore: int,
                         was_new: bool, db_path: Optional[Path] = None) -> None:
    """记一次"某 atom 被聚进某 skill"。供算原子采纳率(采纳原子/总原子)。"""
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO atom_adoption(atom_id,skill,weightscore,was_new) VALUES(?,?,?,?)",
            (atom_id, skill, int(weightscore), 1 if was_new else 0),
        )
        conn.commit()


def _atom_pending_root_key(skill_dir: Path | str) -> str:
    path = Path(skill_dir).expanduser()
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path.absolute())


def sync_atom_candidate_pending_for_skill(
    skill: str,
    candidates: list,
    *,
    db_path: Optional[Path] = None,
    dirty_root_key: str | None = None,
) -> None:
    """按某 skill 当前 candidates 快照替换其 pending 投影行。

    ``(atom_id, skill)`` 为关联主键；同 atom 可独立挂到多个 skill。
    """
    rows: list[tuple[str, str, int]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        atom_id = candidate.get("atom_id") or ""
        if not atom_id:
            continue
        rows.append((str(atom_id), skill, int(candidate.get("weightscore") or 0)))
    with pooled_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM atom_candidate_pending WHERE skill=?", (skill,),
        )
        if rows:
            conn.executemany(
                """
                INSERT INTO atom_candidate_pending(atom_id, skill, weightscore)
                VALUES (?, ?, ?)
                ON CONFLICT(atom_id, skill) DO UPDATE SET
                    weightscore=excluded.weightscore,
                    updated_at=datetime('now')
                """,
                rows,
            )
        if dirty_root_key is not None:
            _mark_skill_edit_dirty_conn(
                conn,
                root_key=dirty_root_key,
                skill=skill,
                reason="candidates",
            )
        conn.commit()


def delete_atom_candidate_pending_for_skill(
    skill: str,
    *,
    db_path: Optional[Path] = None,
    dirty_root_key: str | None = None,
) -> None:
    with pooled_connection(db_path) as conn:
        conn.execute(
            "DELETE FROM atom_candidate_pending WHERE skill=?", (skill,),
        )
        if dirty_root_key is not None:
            _mark_skill_edit_dirty_conn(
                conn,
                root_key=dirty_root_key,
                skill=skill,
                reason="candidates_missing",
            )
        conn.commit()


def notify_atom_pending_sync(
    skill_path: Path | str,
    candidates: list,
    *,
    db_path: Path | str | None = None,
) -> None:
    """candidates 落盘钩子：投影失败只记日志，不阻断磁盘写。"""
    try:
        from xskill.skill.catalog_store import resolve_catalog_db_path
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "atom_candidate_pending skip sync (no registry_db_path): %s",
                skill_path,
            )
            return
        sync_atom_candidate_pending_for_skill(
            Path(skill_path).name,
            candidates,
            db_path=resolved,
            dirty_root_key=_atom_pending_root_key(Path(skill_path).parent),
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "atom_candidate_pending sync failed: %s", skill_path,
        )


def notify_atom_pending_delete(
    skill: str,
    *,
    db_path: Path | str | None = None,
) -> None:
    try:
        from xskill.skill.catalog_store import resolve_catalog_db_path
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "atom_candidate_pending skip delete (no registry_db_path): %s",
                skill,
            )
            return
        delete_atom_candidate_pending_for_skill(skill, db_path=resolved)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "atom_candidate_pending delete failed: %s", skill,
        )


def _mark_skill_edit_dirty_conn(
    conn: sqlite3.Connection,
    *,
    root_key: str,
    skill: str,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO skill_edit_dirty(root_key, skill, generation, reason)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(root_key, skill) DO UPDATE SET
            generation=skill_edit_dirty.generation + 1,
            reason=excluded.reason,
            marked_at=datetime('now')
        """,
        (root_key, skill, reason),
    )


def mark_skill_edit_dirty(
    skill_path: Path | str,
    *,
    reason: str = "candidates",
    db_path: Optional[Path] = None,
) -> None:
    """持久标记一个 skill；重复标记只递增 generation，不增加队列行。"""
    path = Path(skill_path)
    with pooled_connection(db_path) as conn:
        _mark_skill_edit_dirty_conn(
            conn,
            root_key=_atom_pending_root_key(path.parent),
            skill=path.name,
            reason=reason,
        )
        conn.commit()


def list_skill_edit_dirty(
    skill_dir: Path | str,
    *,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """返回一个 skill root 当前待检查的持久脏项。"""
    root_key = _atom_pending_root_key(skill_dir)
    with pooled_connection(db_path) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT skill, generation, reason, marked_at "
                "FROM skill_edit_dirty WHERE root_key=? ORDER BY skill",
                (root_key,),
            ).fetchall()
        ]


def skill_edit_dirty_generation(
    skill_path: Path | str,
    *,
    db_path: Optional[Path] = None,
) -> int | None:
    """读取一个 skill 当前 generation；无脏项返回 ``None``。"""
    path = Path(skill_path)
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT generation FROM skill_edit_dirty "
            "WHERE root_key=? AND skill=?",
            (_atom_pending_root_key(path.parent), path.name),
        ).fetchone()
    return None if row is None else int(row["generation"])


def acknowledge_skill_edit_dirty(
    skill_dir: Path | str,
    skill: str,
    generation: int,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """仅确认仍是所观察 generation 的脏项，避免吞掉并发候选写入。"""
    root_key = _atom_pending_root_key(skill_dir)
    with pooled_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM skill_edit_dirty "
            "WHERE root_key=? AND skill=? AND generation=?",
            (root_key, skill, int(generation)),
        )
        conn.commit()
        return cursor.rowcount > 0


def reconcile_skill_edit_dirty(
    skill_dir: Path | str,
    *,
    db_path: Optional[Path] = None,
) -> int:
    """低频扫一次 skill root，为缺失的脏项补行；已有行不改 generation。"""
    root = Path(skill_dir)
    if not root.is_dir():
        return 0
    skill_names = sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    if not skill_names:
        return 0
    root_key = _atom_pending_root_key(root)
    with pooled_connection(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO skill_edit_dirty(root_key, skill, generation, reason)
            VALUES (?, ?, 1, 'reconcile')
            ON CONFLICT(root_key, skill) DO NOTHING
            """,
            ((root_key, skill) for skill in skill_names),
        )
        inserted = conn.total_changes - before
        conn.commit()
    return inserted


_ATOM_PENDING_BACKFILL_LOCK = threading.Lock()


def backfill_atom_candidate_pending(
    skill_dir: Path | str,
    *,
    db_path: Optional[Path] = None,
) -> int:
    """一次扫盘把各 skill 的 .candidates.yml 灌进 pending 投影表。"""
    skill_dir = Path(skill_dir)
    root = _atom_pending_root_key(skill_dir)
    from xskill.skill.candidates import load_candidates

    rows: list[tuple[str, str, int]] = []
    if skill_dir.is_dir():
        for skill_path in sorted(skill_dir.iterdir()):
            if not skill_path.is_dir() or skill_path.name.startswith("."):
                continue
            try:
                data = load_candidates(skill_path)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "atom_candidate_pending backfill skip %s",
                    skill_path, exc_info=True,
                )
                continue
            for candidate in data.get("candidates", []) or []:
                if not isinstance(candidate, dict):
                    continue
                atom_id = candidate.get("atom_id") or ""
                if not atom_id:
                    continue
                rows.append((
                    str(atom_id),
                    skill_path.name,
                    int(candidate.get("weightscore") or 0),
                ))
    with pooled_connection(db_path) as conn:
        conn.execute("DELETE FROM atom_candidate_pending")
        if rows:
            conn.executemany(
                """
                INSERT INTO atom_candidate_pending(atom_id, skill, weightscore)
                VALUES (?, ?, ?)
                ON CONFLICT(atom_id, skill) DO UPDATE SET
                    weightscore=excluded.weightscore,
                    updated_at=datetime('now')
                """,
                rows,
            )
        conn.execute(
            """
            INSERT INTO atom_candidate_pending_meta(root_key, backfilled_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(root_key) DO UPDATE SET
                backfilled_at=datetime('now')
            """,
            (root,),
        )
        conn.commit()
    logger.info(
        "atom_candidate_pending backfill: root=%s rows=%d", root, len(rows),
    )
    return len(rows)


def ensure_atom_pending_backfilled(
    skill_dir: Path | str,
    db_path: Optional[Path] = None,
) -> None:
    """该 root 尚未 backfill 时做一次扫盘灌表（对齐 skills_catalog ensure）。"""
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        return
    root = _atom_pending_root_key(skill_dir)
    with pooled_connection(db_path) as conn:
        ready = conn.execute(
            "SELECT 1 FROM atom_candidate_pending_meta WHERE root_key=?",
            (root,),
        ).fetchone()
        if ready is not None:
            return
    with _ATOM_PENDING_BACKFILL_LOCK:
        with pooled_connection(db_path) as conn:
            ready = conn.execute(
                "SELECT 1 FROM atom_candidate_pending_meta WHERE root_key=?",
                (root,),
            ).fetchone()
            if ready is not None:
                return
        backfill_atom_candidate_pending(skill_dir, db_path=db_path)


def record_trigger_eval(*, skill: str, version_sha: Optional[str], exp_id: str,
                        train_score: float, test_score: float, n_cases: int,
                        catalog_size: int,
                        db_path: Optional[Path] = None) -> None:
    """记一次离线探针触发评测结果(中选描述的 train/test 触发准确率)。

    供看板展示 per-skill/版本"离线探针触发率"——区别于 mark_skill_used 记的
    线上真实使用频次,两者语义不同不可混。
    """
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO skill_trigger_eval"
            "(skill,version_sha,exp_id,train_score,test_score,n_cases,catalog_size)"
            " VALUES(?,?,?,?,?,?,?)",
            (skill, version_sha, exp_id, float(train_score), float(test_score),
             int(n_cases), int(catalog_size)),
        )
        conn.commit()


def trigger_eval_for_skill(skill: str, *, db_path: Optional[Path] = None) -> list:
    """取某 skill 的离线触发评测历史(按时间升序),供看板趋势图。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT ts,version_sha,exp_id,train_score,test_score,n_cases,"
            "catalog_size FROM skill_trigger_eval WHERE skill=? ORDER BY id ASC",
            (skill,),
        ).fetchall()
        return [dict(r) for r in rows]


def record_canary_decision(*, skill: str, action: str, main_avg: float,
                           staging_avg: float, main_samples: int,
                           staging_samples: int, age_days: float,
                           main_sha: str = "", staging_sha: str = "",
                           db_path: Optional[Path] = None) -> None:
    """记一次灰度裁决(promoted/rejected/timeout_discarded)。供算晋升率。

    ``main_sha``/``staging_sha`` 是裁决时两侧 HEAD——进化图据此把裁决挂到
    具体 commit（D9）；存量无 sha 的历史裁决在图上显式标"无法定位"。
    """
    with pooled_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO canary_decision(skill,action,main_avg,staging_avg,"
            "main_samples,staging_samples,age_days,main_sha,staging_sha)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (skill, action, main_avg, staging_avg, int(main_samples),
             int(staging_samples), float(age_days),
             main_sha or "", staging_sha or ""),
        )
        conn.commit()


def clear_rebuild_derived_state(
    *, registry_path: Optional[Path] = None,
) -> dict[str, int]:
    """Clear global derived dashboard state for ``xskill rebuild --force``.

    ``llm_usage`` is deliberately retained: it is cost accounting for already
    paid calls, not skill state derived from the current repository contents.
    """
    deleted_counts: dict[str, int] = {}
    with pooled_connection(registry_path) as connection:
        cursor = connection.execute("DELETE FROM recommendation_log")
        deleted_counts["recommendation_log"] = cursor.rowcount
        cursor = connection.execute("DELETE FROM atom_adoption")
        deleted_counts["atom_adoption"] = cursor.rowcount
        cursor = connection.execute("DELETE FROM canary_decision")
        deleted_counts["canary_decision"] = cursor.rowcount
        cursor = connection.execute("DELETE FROM skill_trigger_eval")
        deleted_counts["skill_trigger_eval"] = cursor.rowcount
        for table_name in (
            "task_evidence_feed",
            "task_usage_allocations", "task_attempt_relations",
            "task_evidence_ranges", "task_attempts", "task_relations",
            "task_atom_memberships", "logical_tasks", "task_graph_generations",
            "task_graph_source_state",
            "task_graph_dirty_sources",
            "task_graph_dirty_scopes",
        ):
            connection.execute(f"DELETE FROM {table_name}")
        connection.commit()
        return deleted_counts


def usage_summary(db_path: Optional[Path] = None) -> dict:
    """跨重启的持久汇总:累计 token/$、今日 $、按 step / model 分解。"""
    with pooled_connection(db_path) as conn:
        tot = conn.execute(
            "SELECT COALESCE(SUM(total),0) t, COALESCE(SUM(cost_usd),0) c, COUNT(*) n"
            " FROM llm_usage"
        ).fetchone()
        # 本地日界（ts 存的是 UTC；'localtime','start of day','utc' = 本地零点的
        # UTC 表示）。旧口径 date('now') 是 UTC 日界，UTC+8 每天 08:00 前错位 8h。
        today = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage"
            " WHERE ts >= datetime('now','localtime','start of day','utc')"
        ).fetchone()[0]
        estimated = conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE price_source != 'config'"
        ).fetchone()[0] > 0
        by_step = [dict(r) for r in conn.execute(
            "SELECT step, SUM(total) tokens, SUM(cost_usd) cost, COUNT(*) calls"
            " FROM llm_usage GROUP BY step ORDER BY cost DESC"
        ).fetchall()]
        by_model = [dict(r) for r in conn.execute(
            "SELECT model, SUM(total) tokens, SUM(cost_usd) cost, COUNT(*) calls"
            " FROM llm_usage GROUP BY model ORDER BY cost DESC"
        ).fetchall()]
        return {
            "total_tokens": tot["t"], "total_usd": round(tot["c"], 6),
            "total_calls": tot["n"], "today_usd": round(today, 6),
            "estimated": estimated, "by_step": by_step, "by_model": by_model,
        }


def _sidecar_field(md_path: Path, key: str) -> Optional[str]:
    """从 traj_*.md 的同名 .json sidecar 读某字段（model / harness 等）。"""
    try:
        meta = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        v = meta.get(key)
        return str(v) if v else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _sidecar_model(md_path: Path) -> Optional[str]:
    """从 traj_*.md 的同名 .json sidecar 读用户 agent 模型(meta['model'])。"""
    return _sidecar_field(md_path, "model")


# 每条轨迹的 coding agent(harness)推断：
#   1) 优先 client 上报的 source_harness（team 上传带）；
#   2) 缺失时,非 team_client 目录的 ecosystem 本身就是 harness
#      （本机 claude_code / codex / opencode sessions 目录）；
#   3) 都没有（团队上传但旧 client 没带 harness）→ 兜底标签（默认 'unknown'，
#      看板可经 config 的 dashboard.default_harness 改成别的已知 harness）。
# 这样既替代了"全是 team_client"的无信息分组,也不需要为本机轨迹回填。
# 兜底标签经 SQL 命名绑定参数 ``:hlabel`` 注入（自由字符串，防注入/引号问题）。
_HARNESS_EXPR = (
    "COALESCE(NULLIF(t.source_harness,''),"
    " CASE WHEN wd.ecosystem NOT IN ('team_client','manual')"
    " THEN wd.ecosystem END, :hlabel)"
)


def harness_share(db_path: Optional[Path] = None, *,
                  unknown_label: str = "unknown",
                  exclude_paused_backlog: bool = False) -> list[dict]:
    """用户 coding agent(harness)分布(按轨迹数),供看板按 coding agent 显示占比。

    ``unknown_label``：harness 完全缺失时的归类桶，默认 'unknown'。看板层据
    config 传入 dashboard.default_harness 覆盖；canary/stats 等调用不传，保持
    'unknown' 语义不变。
    """
    visibility = (
        f" WHERE {dashboard_visible_trajectory_sql('t', 'wd')}"
        if exclude_paused_backlog else ""
    )
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_HARNESS_EXPR} AS harness, COUNT(*) AS trajs"
            " FROM trajectories t JOIN watch_dirs wd ON t.watch_dir_id=wd.id"
            f"{visibility}"
            f" GROUP BY {_HARNESS_EXPR} ORDER BY trajs DESC",
            {"hlabel": unknown_label},
        ).fetchall()
        total = sum(r["trajs"] for r in rows) or 1
        return [{"harness": r["harness"], "trajs": r["trajs"],
                 "pct": round(100 * r["trajs"] / total, 1)} for r in rows]


def model_share(db_path: Optional[Path] = None, *,
                unknown_label: str = "unknown",
                exclude_paused_backlog: bool = False) -> list[dict]:
    """用户 agent 模型分布(按轨迹数),供 server stats 显示占比。source_model 缺失
    → ``unknown_label``（默认 'unknown'，经命名参数 ``:mlabel`` 注入）。

    注意：canary 的 ``eligible_models`` 把 'unknown' 当“未归属、留在 main”的哨兵，
    所以那条路径必须用默认 'unknown'——只有看板展示层才传入 config 的覆盖值。
    """
    visibility = (
        f" WHERE {dashboard_visible_trajectory_sql('t', 'wd')}"
        if exclude_paused_backlog else ""
    )
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT COALESCE(t.source_model,:mlabel) AS model, COUNT(*) AS trajs"
            " FROM trajectories t JOIN watch_dirs wd ON t.watch_dir_id=wd.id"
            f"{visibility}"
            " GROUP BY COALESCE(t.source_model,:mlabel)"
            " ORDER BY trajs DESC",
            {"mlabel": unknown_label},
        ).fetchall()
        total = sum(r["trajs"] for r in rows) or 1
        return [{"model": r["model"], "trajs": r["trajs"],
                 "pct": round(100 * r["trajs"] / total, 1)} for r in rows]


# ---------------------------------------------------------------------------
# Watch directory management
# ---------------------------------------------------------------------------

def register_dir(
    dir_path: str | Path,
    label: str = "",
    auto_index: bool = True,
    ecosystem: str = "manual",
    *,
    db_path: Optional[Path] = None,
) -> int:
    """注册一个目录。幂等：已存在则更新 label/auto_index/ecosystem，返回 id。

    ``ecosystem`` 标记目录来源，便于 list / search 时区分：
      - ``manual`` (默认)：用户手动 ``xskill registry add`` 注册的
      - ``claude_code``：daemon 启动时自动发现的 Claude Code 会话桥接目录
      - 未来：``codex``、``opencode`` 等
    """
    dir_path = str(Path(dir_path).resolve())
    with pooled_connection(db_path) as conn:
        source_scope_id = f"src_{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO watch_dirs (path, label, auto_index, ecosystem, source_scope_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET"
            "   label=excluded.label,"
            "   auto_index=excluded.auto_index,"
            "   ecosystem=excluded.ecosystem",
            (dir_path, label, int(auto_index), ecosystem, source_scope_id),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM watch_dirs WHERE path=?", (dir_path,)).fetchone()
        return row["id"]


def ensure_watch_dir_source_scope(
    watch_dir_id: int, *, db_path: Optional[Path] = None,
) -> str:
    """Return the persisted opaque ingestion namespace for one watch dir."""
    if isinstance(watch_dir_id, bool) or not isinstance(watch_dir_id, int):
        raise ValueError("watch_dir_id must be an integer")
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT source_scope_id FROM watch_dirs WHERE id=?",
            (watch_dir_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"watch_dir id={watch_dir_id} does not exist")
        existing = str(row["source_scope_id"] or "").strip()
        if existing:
            return existing
        source_scope_id = f"src_{uuid.uuid4().hex}"
        conn.execute(
            "UPDATE watch_dirs SET source_scope_id=?"
            " WHERE id=? AND (source_scope_id IS NULL OR source_scope_id='')",
            (source_scope_id, watch_dir_id),
        )
        conn.commit()
        resolved = conn.execute(
            "SELECT source_scope_id FROM watch_dirs WHERE id=?",
            (watch_dir_id,),
        ).fetchone()["source_scope_id"]
        return str(resolved)


def unregister_dir(dir_path: str | Path, *, db_path: Optional[Path] = None) -> bool:
    """移除目录及其轨迹记录。返回 True 表示找到并删除。

    级联删轨迹的同时清对应 ``atom_adoption`` 行——否则采纳率分子留历史累计、
    分母被删小，比率虚高（审计 P1-7）。
    """
    dir_path = str(Path(dir_path).resolve())
    with pooled_connection(db_path) as conn:
        source_rows = conn.execute(
            "SELECT t.watch_dir_id,t.filename,w.source_scope_id"
            " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
            " WHERE w.path=?",
            (dir_path,),
        ).fetchall()
        for source_row in source_rows:
            stem = (
                source_row["filename"][:-3]
                if source_row["filename"].endswith(".md")
                else source_row["filename"]
            )
            conn.execute("DELETE FROM atom_adoption WHERE atom_id GLOB ?",
                         (f"atom_{stem}_*",))
            state = conn.execute(
                "SELECT tenant_id,task_scope_id FROM task_graph_source_state"
                " WHERE source_scope_id=? AND traj_id=?",
                (source_row["source_scope_id"], stem),
            ).fetchone()
            if state is not None:
                conn.execute(
                    "INSERT INTO task_graph_dirty_sources("
                    "watch_dir_id,filename,source_scope_id,tenant_id,task_scope_id,"
                    "deleted,generation,reason,marked_at)"
                    " VALUES(?,?,?,?,?,1,1,'watch_dir_removed',datetime('now'))"
                    " ON CONFLICT(watch_dir_id,filename) DO UPDATE SET"
                    " deleted=1,generation=generation+1,reason='watch_dir_removed',"
                    " marked_at=datetime('now')",
                    (
                        source_row["watch_dir_id"], source_row["filename"],
                        source_row["source_scope_id"], state["tenant_id"],
                        state["task_scope_id"],
                    ),
                )
        cur = conn.execute("DELETE FROM watch_dirs WHERE path=?", (dir_path,))
        conn.commit()
        return cur.rowcount > 0


def list_watch_dirs(
    *,
    db_path: Optional[Path] = None,
    exclude_paused_backlog: bool = False,
) -> list[dict]:
    """返回所有注册目录及统计信息。"""
    visibility = (
        f" AND {dashboard_visible_trajectory_sql('t', 'w')}"
        if exclude_paused_backlog else ""
    )
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "  (SELECT COUNT(*) FROM trajectories t WHERE t.watch_dir_id=w.id"
            f"{visibility}) AS traj_count,"
            "  (SELECT COUNT(*) FROM trajectories t WHERE t.watch_dir_id=w.id"
            "   AND t.has_embedding=1"
            f"{visibility}) AS indexed_count"
            " FROM watch_dirs w ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]


def list_watcher_dirs(*, db_path: Optional[Path] = None) -> list[dict]:
    """返回 watcher 调度所需目录，不计算 dashboard 统计。

    ``list_watch_dirs`` 会为每个目录统计历史轨迹数和已索引数，适合 CLI 与
    dashboard，但不适合每几秒调用一次的 watcher 热路径。这里仅返回
    ``watch_dirs`` 行；是否暂停仍由 watcher 在内存中判断，使查询不依赖旧库
    可能缺少的 ``auto_index`` 列，并让空闲成本不随历史轨迹数量增长。
    """
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM watch_dirs ORDER BY id",
        ).fetchall()
        return [dict(row) for row in rows]


def get_watch_dir(dir_path: str | Path, *, db_path: Optional[Path] = None) -> dict | None:
    """查询单个目录记录。"""
    dir_path = str(Path(dir_path).resolve())
    with pooled_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM watch_dirs WHERE path=?", (dir_path,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Trajectory tracking
# ---------------------------------------------------------------------------

# mtime 变更检测时**不触碰**的中间态：split / cluster 正在 in-flight 跑，
# 此刻翻 updated 会和在飞 future 的状态回写打架。留着旧 mtime,等它落定下一轮
# scan 再检出变更（续写重拆最终收敛,不丢更新）。
_ACTIVE_STATUSES = ("splitting", "clustering")


def discover_trajectories(
    watch_dir_id: int,
    dir_path: Path,
    *,
    db_path: Optional[Path] = None,
) -> list[str]:
    """扫描目录中的 traj_*.md，upsert 到 DB。返回新发现的文件名列表。

    续写重拆触发：已存在的文件若 mtime 增大（客户端追加内容后重传覆盖写,
    mtime 变更），把它从"已落定"状态翻回 ``updated``——watcher 下一轮会像
    ``discovered`` 一样重新提交 split，TaskAgent 用 ``last_offset`` 续接点
    只拆新增内容。``updated`` 不计入返回的 new_files（只统计真·新文件）。
    """
    dir_path = Path(dir_path)
    new_files: list[str] = []
    with pooled_connection(db_path) as conn:
        # P2-2.1 归因(D5):入库即把 watch_dir 的 label 写进 user_key,聚合层
        # 不再 JOIN watch_dirs.label——source 唯一。CS 模式各用户桶(label=
        # sessions 桶目录名=user_name/client_id)不论 ecosystem 是 team_client
        # 还是 bridge 检出的真实生态(ngagent 等)都归因;无 label 的本地目录
        # 留空,聚合层显示 '(local)'。
        wd = conn.execute(
            "SELECT label FROM watch_dirs WHERE id=?",
            (watch_dir_id,),
        ).fetchone()
        user_key = (wd["label"] or "") if wd else ""

        existing = {
            row["filename"]: row
            for row in conn.execute(
                "SELECT filename, status, process_action, file_mtime FROM trajectories"
                " WHERE watch_dir_id=?",
                (watch_dir_id,),
            ).fetchall()
        }

        for md in sorted(dir_path.glob("traj_*.md")):
            if md.name.endswith(".meta"):
                continue
            mtime = md.stat().st_mtime
            row = existing.get(md.name)
            if row is None:
                conn.execute(
                    "INSERT INTO trajectories"
                    " (watch_dir_id, filename, file_mtime, source_model,"
                    "  source_harness, user_key, discovered_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                    (watch_dir_id, md.name, mtime, _sidecar_model(md),
                     _sidecar_field(md, "harness"), user_key),
                )
                new_files.append(md.name)
                continue

            stored_mtime = row["file_mtime"] or 0
            if mtime <= stored_mtime:
                continue  # 没变化
            status = row["status"]
            if status in _ACTIVE_STATUSES:
                # 正在 split/cluster——别打架,留旧 mtime,落定后下一轮再检出。
                continue
            if status == "discovered":
                # 还没开拆,后续 split 会读到最新内容（last_offset=0 全量拆）。
                # 只更 mtime,不必翻 updated。
                conn.execute(
                    "UPDATE trajectories SET file_mtime=?"
                    " WHERE watch_dir_id=? AND filename=?",
                    (mtime, watch_dir_id, md.name),
                )
                continue
            if (
                status == TrajectoryStatus.FILTERED.value
                and row["process_action"] == ProcessAction.NOT_FIT.value
            ):
                # Interest-filtered trajectories re-enter only after interests change.
                conn.execute(
                    "UPDATE trajectories SET file_mtime=?"
                    " WHERE watch_dir_id=? AND filename=?",
                    (mtime, watch_dir_id, md.name),
                )
                continue
            # 已落定（done/indexed/split_done/error/filtered/updated）+ 内容变更
            # → 翻 updated,等下一轮重新 split（续接点续拆）。
            conn.execute(
                "UPDATE trajectories SET status='updated', file_mtime=?,"
                " updated_at=datetime('now')"
                " WHERE watch_dir_id=? AND filename=?",
                (mtime, watch_dir_id, md.name),
            )

        conn.commit()
        return new_files


def mark_meta_done(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET has_meta=1 WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        )
        conn.commit()


def mark_indexed(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET has_embedding=1, indexed_at=?"
            " WHERE watch_dir_id=? AND filename=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), watch_dir_id, filename),
        )
        conn.commit()


def mark_skill_used(
    watch_dir_id: int,
    filename: str,
    skill_used: str,
    canary_side: str,
    *,
    db_path: Optional[Path] = None,
) -> None:
    """记录该轨迹触发了哪个 skill / 哪个灰度 side。

    设计：skill 版本(sha) / 用户**不落 trajectories 列**,而是看板 metrics 查询时
    从 traj .md 头 `<!-- xskill:skill=X side=Y sha=Z -->` 分析式解析(版本)、JOIN
    watch_dirs.label 现算(用户)。与工具调用/ token 同属"按轨迹文本现算",保持
    "分析而非埋点"一致——免迁移、不改这条打分热路径的写入语义。
    """
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET skill_used=?, canary_side=?"
            " WHERE watch_dir_id=? AND filename=?",
            (skill_used, canary_side, watch_dir_id, filename),
        )
        conn.commit()


def get_unindexed(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回缺少 meta 或 embedding 的文件名。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND (has_meta=0 OR has_embedding=0)"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]


def get_needs_meta(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回缺少 meta 的文件名。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND has_meta=0"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]


def get_needs_embedding(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回有 meta 但缺 embedding 的文件名。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND has_meta=1 AND has_embedding=0"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]


# ---------------------------------------------------------------------------
# Cross-dataset search support
# ---------------------------------------------------------------------------

def all_index_paths(*, db_path: Optional[Path] = None) -> list[Path]:
    """返回所有注册目录中实际存在 index.pkl 的路径。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute("SELECT path FROM watch_dirs ORDER BY id").fetchall()
        result = []
        for r in rows:
            p = Path(r["path"])
            if (p / "index.pkl").is_file():
                result.append(p)
        return result


def find_traj_file(
    traj_id: str,
    suffix: str = ".md",
    *,
    db_path: Optional[Path] = None,
) -> Path | None:
    """跨所有注册的 watch dir 查找 ``<traj_id><suffix>``。

    各 dir 先按"扁平布局"（``<wd>/<traj_id><suffix>``）直查，未命中再递归
    rglob。返回第一个命中；都没有就返回 None 并打 warning。

    用于替代历史上写死的 ``skill_dir.parent.parent / "data"`` 反推路径。
    那条 v1 假设在轨迹搬到 Registry 注册任意目录后已失效，会让
    eval / candidate / SWE-bench 收集等多处静默拿不到源轨迹。
    """
    filename = f"{traj_id}{suffix}"
    watch_dirs = list_watch_dirs(db_path=db_path)
    if not watch_dirs:
        logger.warning(
            "find_traj_file(%s): no watch dirs registered; "
            "run `xskill registry add <path>` to register a trajectory directory",
            filename,
        )
        return None
    searched: list[str] = []
    for wd in watch_dirs:
        wd_path = Path(wd["path"])
        if not wd_path.is_dir():
            continue
        searched.append(str(wd_path))
        direct = wd_path / filename
        if direct.is_file():
            return direct
        for hit in wd_path.rglob(filename):
            return hit
    logger.warning(
        "find_traj_file(%s): not found in any registered watch dir "
        "(searched %d dir(s): %s)",
        filename, len(searched), ", ".join(searched) or "(none reachable)",
    )
    return None


# ---------------------------------------------------------------------------
# Status management
# ---------------------------------------------------------------------------

_NOW = "datetime('now')"


def update_traj_status(
    watch_dir_id: int,
    filename: str,
    status: str,
    *,
    process_action: str | None = None,
    skill_generated: str | None = None,
    ux_score: float | None = None,
    error_msg: str | None = None,
    retry_count: int | None = None,
    db_path: Optional[Path] = None,
) -> None:
    """更新轨迹状态及关联字段。

    ``retry_count`` 显式传入时覆盖列上的值——cluster 阶段 partial-fail
    会算好"重试次数 + 1"再回写，沿着 ``retry_count < max_retries``
    继续重试，超过门槛后兜底标 done + WARNING。
    """
    with pooled_connection(db_path) as conn:
        sets = ["updated_at=datetime('now')"]
        vals: list = []
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if process_action is not None:
            sets.append("process_action=?")
            vals.append(process_action)
        if skill_generated is not None:
            sets.append("skill_generated=?")
            vals.append(skill_generated)
        if ux_score is not None:
            sets.append("ux_score=?")
            vals.append(ux_score)
        if error_msg is not None:
            sets.append("error_msg=?")
            vals.append(error_msg)
        if retry_count is not None:
            sets.append("retry_count=?")
            vals.append(int(retry_count))
        vals.extend([watch_dir_id, filename])
        conn.execute(
            f"UPDATE trajectories SET {', '.join(sets)}"
            " WHERE watch_dir_id=? AND filename=?",
            vals,
        )
        conn.commit()


def get_traj_retry_count(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None,
) -> int:
    """返回 ``trajectories.retry_count``。行不存在 / 列为 NULL → 0。

    cluster partial-fail 重试用：先读当前 retry_count，+1 后回写
    ``update_traj_status(..., retry_count=N+1)``。和 ``increment_retry``
    的差异是这里**只读不写**，由调用方决定何时 +1。
    """
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT retry_count FROM trajectories"
            " WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        ).fetchone()
        if row is None:
            return 0
        return int(row["retry_count"] or 0)


def update_traj_log(
    watch_dir_id: int,
    filename: str,
    log_json: str,
    *,
    db_path: Optional[Path] = None,
) -> None:
    """Store process log (JSON string) for a trajectory."""
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET process_log=?, updated_at=datetime('now')"
            " WHERE watch_dir_id=? AND filename=?",
            (log_json, watch_dir_id, filename),
        )
        conn.commit()


def get_traj_log(
    watch_dir_id: int,
    filename: str,
    *,
    db_path: Optional[Path] = None,
) -> str | None:
    """Retrieve the stored process log JSON for a trajectory."""
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT process_log FROM trajectories WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        ).fetchone()
        return row["process_log"] if row else None


def update_traj_offset(
    watch_dir_id: int,
    filename: str,
    *,
    last_offset: int,
    last_atom_id: str | None,
    tasks_extracted: int,
    db_path: Optional[Path] = None,
) -> None:
    """更新轨迹的 AtomTask 增量进度指针。

    watcher 每次跑完 TaskAgent 后调，让下次 scan 用最新的 offset 决定 delta。
    ``last_atom_id`` 为 None 表示当前轨迹还没切出任何 atom（罕见——通常拆出
    至少 1 个）。
    """
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET last_offset=?, last_atom_id=?, "
            "tasks_extracted=?, updated_at=datetime('now')"
            " WHERE watch_dir_id=? AND filename=?",
            (int(last_offset), last_atom_id, int(tasks_extracted),
             watch_dir_id, filename),
        )
        conn.commit()


def mark_not_fit(
    watch_dir_id: int,
    filename: str,
    reason: str,
    interest_fingerprint: str,
    *,
    db_path: Optional[Path] = None,
) -> None:
    """Mark a trajectory as terminally filtered by the configured interests."""
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET status=?, process_action=?, error_msg=?, "
            "interest_fingerprint=?, updated_at=datetime('now')"
            " WHERE watch_dir_id=? AND filename=?",
            (
                TrajectoryStatus.FILTERED.value,
                ProcessAction.NOT_FIT.value,
                reason,
                interest_fingerprint,
                watch_dir_id,
                filename,
            ),
        )
        conn.commit()


def reset_not_fit_for_interest_change(
    *,
    old_interest_fingerprint: str,
    new_interest_fingerprint: str,
    db_path: Optional[Path] = None,
) -> int:
    """Reset only stale filtered/not_fit trajectories for re-evaluation."""
    if old_interest_fingerprint == new_interest_fingerprint:
        return 0
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT t.id, t.filename, t.user_key, w.path FROM trajectories t "
            "JOIN watch_dirs w ON t.watch_dir_id = w.id "
            "WHERE t.status=? AND t.process_action=? "
            "AND (t.interest_fingerprint IS NULL OR t.interest_fingerprint != ?)",
            (
                TrajectoryStatus.FILTERED.value,
                ProcessAction.NOT_FIT.value,
                new_interest_fingerprint,
            ),
        ).fetchall()
        directories_seen: set[str] = set()
        profile_store_roots: set[str] = set()
        trajectories_by_directory: dict[str, set[str]] = {}
        for row in rows:
            conn.execute(
                "UPDATE trajectories SET status=?, process_action=NULL, "
                "error_msg=NULL, interest_fingerprint=NULL, last_offset=0, "
                "last_atom_id=NULL, tasks_extracted=0, has_meta=0, "
                "has_embedding=0, indexed_at=NULL, updated_at=datetime('now') "
                "WHERE id=?",
                (TrajectoryStatus.DISCOVERED.value, row["id"]),
            )
            trajectory_stem = (
                row["filename"][:-3]
                if row["filename"].endswith(".md")
                else row["filename"]
            )
            tasks_directory = Path(row["path"]) / trajectory_stem / "tasks"
            if tasks_directory.is_dir():
                for atom_file in tasks_directory.glob("atom_*.json"):
                    atom_file.unlink()
            directories_seen.add(row["path"])
            if row["user_key"]:
                profile_store_roots.add(row["path"])
            trajectories_by_directory.setdefault(row["path"], set()).add(
                trajectory_stem,
            )
        if profile_store_roots:
            from xskill.recommend.profile_dirty import (
                mark_profile_dirty_on_connection,
                profile_user_key_for_store_root,
            )
            for store_root in profile_store_roots:
                mark_profile_dirty_on_connection(
                    conn,
                    profile_user_key_for_store_root(store_root),
                    reason="atom_reset",
                )
        conn.commit()
        from xskill.pipeline.atom import AtomTaskStore
        for directory_path, trajectory_ids in trajectories_by_directory.items():
            AtomTaskStore(Path(directory_path)).remove_locations_for_trajs(
                trajectory_ids,
            )
        for directory_path in directories_seen:
            index_path = Path(directory_path) / "index.pkl"
            if index_path.is_file():
                index_path.unlink()
        return len(rows)


def reset_trajectories(
    *,
    eco: Optional[str] = None,
    traj_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[int]:
    """删除已拆 atom + 重置状态，让 watcher 从头重拆（``xskill rebuild`` 用）。

    **关键正确性点**：TaskAgent 的续接点取自 atom **文件**
    （``AtomTaskStore.last_offset`` = 各 ``<traj_id>/tasks/atom_*.json`` 的
    ``max(offset_end)``），**不读 DB 的 ``last_offset`` 列**。所以只翻 DB 状态
    而不删 atom 文件 → ``last_offset ≥ EOF`` → TaskAgent 直接返回空 → 重拆失效
    （0.6.1a1 的洞）。因此本函数**必删 atom 文件**，这才是真正触发重拆的动作。

    同时删对应的 Atom 向量投影行和该目录的 ``index.pkl`` 兼容标记——否则
    cluster 阶段向量检索可能命中已不存在的 atom。

    DB ``status`` 翻回 ``discovered`` 让 watcher 下轮重新排 split；轨迹级
    skill / canary / UX 派生字段一并清空，避免 rebuild 后看板继续挂旧 skill。

    Args:
        eco: 只重置该生态（``watch_dirs.ecosystem``）的轨迹；None=全部。
        traj_id: 只重置该轨迹（按文件名 stem 匹配）；None=不按轨迹过滤。

    Returns:
        被重置的轨迹 id 列表（cold-start 快照即由此而来）。
    """
    with pooled_connection(db_path) as conn:
        query_text = (
            "SELECT t.id, t.watch_dir_id, t.filename, t.user_key, w.path,"
            " w.source_scope_id FROM trajectories t "
            "JOIN watch_dirs w ON t.watch_dir_id = w.id WHERE 1=1"
        )
        query_parameters: list = []
        if eco:
            query_text += " AND w.ecosystem = ?"
            query_parameters.append(eco)
        if traj_id:
            query_text += " AND (t.filename = ? OR t.filename = ?)"
            query_parameters += [traj_id, f"{traj_id}.md"]
        trajectory_rows = conn.execute(query_text, query_parameters).fetchall()

        directories_seen: set[str] = set()
        profile_store_roots: set[str] = set()
        trajectories_by_directory: dict[str, set[str]] = {}
        for trajectory_row in trajectory_rows:
            conn.execute(
                "UPDATE trajectories SET status='discovered', process_action=NULL, "
                "error_msg=NULL, interest_fingerprint=NULL, last_offset=0, "
                "last_atom_id=NULL, tasks_extracted=0, "
                "has_meta=0, has_embedding=0, indexed_at=NULL, "
                "skill_generated=NULL, skill_used=NULL, canary_side=NULL, "
                "ux_score=NULL, "
                "updated_at=datetime('now') WHERE id=?",
                (trajectory_row["id"],),
            )
            trajectory_stem = (
                trajectory_row["filename"][:-3]
                if trajectory_row["filename"].endswith(".md")
                else trajectory_row["filename"]
            )
            tasks_directory = Path(trajectory_row["path"]) / trajectory_stem / "tasks"
            if tasks_directory.is_dir():
                for atom_file in tasks_directory.glob("atom_*.json"):
                    atom_file.unlink()
            # atom 文件已删、tasks_extracted 已归零——采纳事件一并清，
            # 否则采纳率分子留历史累计、分母归零后比率虚高（审计 P1-7）。
            conn.execute("DELETE FROM atom_adoption WHERE atom_id GLOB ?",
                         (f"atom_{trajectory_stem}_*",))
            source_state = conn.execute(
                "SELECT tenant_id,task_scope_id FROM task_graph_source_state"
                " WHERE source_scope_id=? AND traj_id=?",
                (trajectory_row["source_scope_id"], trajectory_stem),
            ).fetchone()
            if source_state is not None:
                conn.execute(
                    "INSERT INTO task_graph_dirty_sources("
                    "watch_dir_id,filename,source_scope_id,tenant_id,task_scope_id,"
                    "deleted,generation,reason,marked_at)"
                    " VALUES(?,?,?,?,?,1,1,'atom_reset',datetime('now'))"
                    " ON CONFLICT(watch_dir_id,filename) DO UPDATE SET"
                    " deleted=1,generation=generation+1,reason='atom_reset',"
                    " marked_at=datetime('now')",
                    (
                        trajectory_row["watch_dir_id"],
                        trajectory_row["filename"],
                        trajectory_row["source_scope_id"],
                        source_state["tenant_id"],
                        source_state["task_scope_id"],
                    ),
                )
            directories_seen.add(trajectory_row["path"])
            if trajectory_row["user_key"]:
                profile_store_roots.add(trajectory_row["path"])
            trajectories_by_directory.setdefault(
                trajectory_row["path"],
                set(),
            ).add(trajectory_stem)
        if profile_store_roots:
            from xskill.recommend.profile_dirty import (
                mark_profile_dirty_on_connection,
                profile_user_key_for_store_root,
            )
            for store_root in profile_store_roots:
                mark_profile_dirty_on_connection(
                    conn,
                    profile_user_key_for_store_root(store_root),
                    reason="atom_reset",
                )
        conn.commit()
        from xskill.pipeline.atom import AtomTaskStore
        for directory_path, trajectory_ids in trajectories_by_directory.items():
            AtomTaskStore(Path(directory_path)).remove_locations_for_trajs(
                trajectory_ids,
            )
        # 向量投影行已按轨迹清理；再删兼容发现标记，等待重拆后重新发布。
        for directory_path in directories_seen:
            index_path = Path(directory_path) / "index.pkl"
            if index_path.is_file():
                index_path.unlink()
        return [trajectory_row["id"] for trajectory_row in trajectory_rows]


def get_trajs_by_status(
    watch_dir_id: int,
    status: str,
    *,
    limit: int = 0,
    max_retries: int = 3,
    db_path: Optional[Path] = None,
) -> list[str]:
    """按状态查询文件名，新的在前（discovered_at、file_mtime、id 降序）。
    error 状态自动过滤超过 max_retries 的。"""
    with pooled_connection(db_path) as conn:
        sql = "SELECT filename FROM trajectories WHERE watch_dir_id=? AND status=?"
        params: list = [watch_dir_id, status]
        if status == "error":
            sql += " AND retry_count < ?"
            params.append(max_retries)
        sql += " ORDER BY discovered_at DESC, file_mtime DESC, id DESC"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [r["filename"] for r in rows]


def get_pending_traj_ids(
    trajectory_ids: Optional[list[int]] = None,
    *,
    db_path: Optional[Path] = None,
) -> list[int]:
    """返回处于 pending 状态的轨迹 id；``trajectory_ids=None`` 时查全库。

    只计 ``auto_index=1`` 的 watch dir——watcher 不处理关闭索引的目录，其中
    的轨迹永远不会离开 pending，计入会让 cold-start 只能靠超时退出。
    """
    with pooled_connection(db_path) as conn:
        status_placeholders = ",".join("?" * len(PENDING_TRAJECTORY_STATUSES))
        base_sql = (
            "SELECT t.id FROM trajectories t "
            "JOIN watch_dirs w ON t.watch_dir_id = w.id "
            f"WHERE w.auto_index=1 AND t.status IN ({status_placeholders})"
        )
        if trajectory_ids is None:
            rows = conn.execute(base_sql, PENDING_TRAJECTORY_STATUSES).fetchall()
            return [row["id"] for row in rows]
        pending_ids: list[int] = []
        # 分块进 IN 子句：老版本 SQLite 绑定变量上限只有 999。
        for chunk_start in range(0, len(trajectory_ids), 500):
            chunk = trajectory_ids[chunk_start:chunk_start + 500]
            id_placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                base_sql + f" AND t.id IN ({id_placeholders})",
                (*PENDING_TRAJECTORY_STATUSES, *chunk),
            ).fetchall()
            pending_ids += [row["id"] for row in rows]
        return pending_ids


def increment_retry(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    with pooled_connection(db_path) as conn:
        conn.execute(
            "UPDATE trajectories SET retry_count = retry_count + 1"
            " WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        )
        conn.commit()


def get_status_counts(
    watch_dir_id: int | None = None, *, db_path: Optional[Path] = None
) -> dict[str, int]:
    """返回各状态的轨迹数量。watch_dir_id=None 时统计全部。"""
    with pooled_connection(db_path) as conn:
        if watch_dir_id is not None:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM trajectories"
                " WHERE watch_dir_id=? GROUP BY status",
                (watch_dir_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM trajectories GROUP BY status"
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


# =============================================================================
# Registry 实体类 —— 包装上面的模块函数为 OOP 接口
# =============================================================================
# 所有 watch_dir + trajectory 反查走这个类。


class Registry:
    """监听目录注册表 + 轨迹处理状态查询。

    数据存于 ~/.xskill/registry.db。所有方法直接代理本模块函数；
    本类只负责 Pythonic 接口与 dataclass 包装。
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path  # None = 用 config 默认

    # ─── watch_dir 管理 ───────────────────────────────────────────
    def add(self, path: str | Path, label: str = "",
            ecosystem: str = "manual") -> WatchDir:
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {p}")
        register_dir(p, label=label, ecosystem=ecosystem,
                     db_path=self._db_path)
        row = get_watch_dir(p, db_path=self._db_path)
        if not row:
            raise RuntimeError(f"register_dir succeeded but row missing: {p}")
        return self._row_to_watch_dir(row, traj_count=0, indexed_count=0)

    def remove(self, path: str | Path) -> bool:
        p = Path(path).expanduser().resolve()
        return unregister_dir(p, db_path=self._db_path)

    def list(self) -> list[WatchDir]:
        rows = list_watch_dirs(db_path=self._db_path)
        return [self._row_to_watch_dir(r) for r in rows]

    def get(self, path: str | Path) -> Optional[WatchDir]:
        p = Path(path).expanduser().resolve()
        row = get_watch_dir(p, db_path=self._db_path)
        return self._row_to_watch_dir(row) if row else None

    @staticmethod
    def _row_to_watch_dir(row: dict, **overrides) -> WatchDir:
        return WatchDir(
            id=row["id"],
            path=Path(row["path"]),
            label=row.get("label", ""),
            auto_index=bool(row.get("auto_index", 1)),
            traj_count=overrides.get("traj_count", row.get("traj_count", 0)),
            indexed_count=overrides.get("indexed_count", row.get("indexed_count", 0)),
            ecosystem=row.get("ecosystem", "manual"),
        )

    # ─── trajectory 反查 ────────────────────────────────────────
    def trajectory_status(self, traj_path: str | Path) -> Optional[dict]:
        """返回某条 traj 在 trajectories 表里的全部字段（含 skill_used / canary_side / ux_score）。
        未找到返回 None。"""
        traj_path = Path(traj_path).resolve()
        wd_path = str(traj_path.parent)
        with pooled_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT t.* FROM trajectories t "
                "JOIN watch_dirs w ON t.watch_dir_id = w.id "
                "WHERE w.path = ? AND t.filename = ?",
                (wd_path, traj_path.name),
            ).fetchone()
            return dict(row) if row else None

    def trajectories_using(self, skill_name: str) -> list[Path]:
        """反查：曾用过某个 skill 的所有轨迹路径。
        skill_used 字段是逗号分隔，故 LIKE 匹配。"""
        with pooled_connection(self._db_path) as conn:
            rows = conn.execute(
                "SELECT w.path AS wd_path, t.filename "
                "FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id "
                "WHERE t.skill_used = ? OR t.skill_used LIKE ? "
                "   OR t.skill_used LIKE ? OR t.skill_used LIKE ?",
                (skill_name,
                 f"{skill_name},%",
                 f"%,{skill_name}",
                 f"%,{skill_name},%"),
            ).fetchall()
            return [Path(r["wd_path"]) / r["filename"] for r in rows]
