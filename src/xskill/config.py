"""
config.py — 全局路径与配置加载
═════════════════════════════════════
统一从 ~/.xskill/ 读取；无 cwd fallback、无环境变量 fallback、无 ~/.aikey fallback。
缺失即抛异常。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("xskill.config")

# ─── 默认根路径 ─────────────────────────────────────────────────
XSKILL_HOME = Path.home() / ".xskill"
CONFIG_PATH = XSKILL_HOME / "config.yaml"
LOGS_DIR = XSKILL_HOME / "logs"

_config: dict = {}
_overrides: dict = {}

DEFAULT_AGENT_WORKER_POOLS = {
    "split": {"workers": 24, "llm_weight": 6},
    "cluster": {"workers": 8, "batch_size": 8, "llm_weight": 3},
    "edit": {"workers": 4, "batch_size": 5, "llm_weight": 1},
    "embed": {"workers": 4},
}
DEFAULT_LLM_RATE_LIMIT = {
    "rpm": 240,
    "request_burst": 8,
    "max_inflight": 8,
}
DEFAULT_EMBEDDING_MAX_INFLIGHT = 4


def set_overrides(**kwargs):
    """CLI flag 覆盖。仅 debug / quiet 两个保留。"""
    for k, v in kwargs.items():
        if v is not None:
            _overrides[k] = v


# 首次运行 auto-init 写出的配置模板。这是配置格式的**唯一真源**——
# 不再单独维护 examples/config.yaml.example，避免两份漂移。
CONFIG_TEMPLATE = """\
# xskill config — fill in the api keys below, then run `xskill serve` again.
#
# xskill does NOT read environment variables or any key file. Missing required
# fields (llm.api_key / embedding.api_key) raise loudly — no silent fallback.

# ===== Skill repository =====
skill_dir: ~/.xskill/skill            # the single global skill repo
interests: []                         # optional top-level interest filter;
                                      # non-empty list enables TaskAgent filtering

# ===== Trajectory-to-Skill algorithm kernel =====
# XSkill only selects/discovers the kernel.  Every non-native kernel owns its
# own ~/.xskill/kernels/<id>/config.yaml and workspace; XSkill never parses or
# rewrites that private config.
kernel:
  kernel_id: native
  kernels_path: ~/.xskill/kernels     # <id>/kernel.py local bridge scripts

# ===== LLM (generation / scoring / chat) =====
# Any OpenAI-compatible chat-completions endpoint works (DeepSeek, OpenAI,
# Qwen/DashScope, OpenRouter, a local Ollama, ...).
llm:
  base_url: https://api.deepseek.com
  model:    deepseek-v4-flash
  api_key:  PUT_YOUR_LLM_API_KEY_HERE
  max_tokens: 10000      # optional; a "thinking" model needs enough budget for
                         # reasoning_tokens + content, or meta extraction
                         # returns empty/truncated and falls back to rules.
  # max_context: 200000  # optional; the model's CONTEXT-WINDOW size in tokens.
                         # The windowless single-pass splitter (TaskAgent) uses
                         # this as the denominator for context self-management:
                         # it proactively trims old `look` tool results at 85%
                         # of this budget. Leave commented to use the 200K
                         # default (a warning is logged). Uncomment and set it
                         # to YOUR model's real context limit (e.g. 128000 for
                         # gpt-4o, 64000 for deepseek-chat).
  # enable_spill: false   # optional; default false. When false, never trim/
                         # spill old tool results; rely on compact_token_limit.
                         # Set true to restore proactive spill@85% behavior.
  # compact_token_limit: 120000  # optional; if estimated history is still above
                         # this limit, ask the same chat model to compact working
                         # memory (Generate / SkillEdit). With enable_spill true,
                         # the effective limit is never below spill@ (85% of
                         # max_context). Leave commented to disable compact.
                         # Compact uses synchronous HTTP streaming (invoke_stream)
                         # so request_timeout is between chunks, not the whole
                         # summary. Failures retry; they do not continue the
                         # main request with still-over-limit history.
  # compact_keep_recent_messages: 6 # optional; recent complete message blocks
                         # kept verbatim after compact. Default 6.
  # temperature: 0.0     # optional; default 0 (deterministic)
  # extra_body:          # optional provider-specific OpenAI request fields;
  #   chat_template_kwargs:  # e.g. llama.cpp/Qwen chat-template controls.
  #     enable_thinking: false # changing reasoning can affect quality; replay
                         # representative trajectories before disabling it.
  # request_timeout: 60  # optional; per-request wall-clock cap in seconds
                         # (default 60). Explicit so an unreachable endpoint
                         # fails loud instead of hanging forever.
  # connect_timeout: 10  # optional; TCP-connect cap in seconds (default 10).
  # client_max_retries: 0 # optional; openai-SDK client retries (default 0 —
                         # transient-error retries are handled by xskill's own
                         # retry wrapper; client retries would multiply).
  rate_limit:
    rpm: 240             # requests per minute shared by split/cluster/edit
    request_burst: 8     # short request burst capacity
    max_inflight: 8      # simultaneous LLM HTTP requests across all three pools
    # tpm: 100000        # optional tokens per minute
    # token_burst: 20000 # optional token burst capacity (separate from requests)
  # See docs/adr/0001-rate-limit-diy-not-litellm.md for the design rationale.

# Optional. Give split, cluster, or edit their own model or endpoint if you want.
# Anything you leave out falls back to llm_skill, then llm. Leave this commented
# and your existing config keeps working as before.
# llm_agents:
#   split:
#     model: qwen-plus
#   cluster:
#     model: deepseek-v4-flash
#   edit:
#     base_url: http://localhost:8000/v1
#     model: local-skill-editor
#     api_key: local

# ===== Embedding (vector retrieval) =====
# Any OpenAI-compatible embeddings endpoint. dim: 0 auto-probes on first call.
#
# DeepSeek does NOT provide an embeddings API. Choose one of these:
#   • Alibaba DashScope:  base_url=https://dashscope.aliyuncs.com/compatible-mode/v1  model=text-embedding-v4
#   • OpenAI:             base_url=https://api.openai.com/v1                          model=text-embedding-3-small
#   • Ollama (local):     base_url=http://localhost:11434/v1                          model=nomic-embed-text
#   • Jina AI:            base_url=https://api.jina.ai/v1                             model=jina-embeddings-v3
embedding:
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  model:    text-embedding-v4
  api_key:  PUT_YOUR_EMBEDDING_API_KEY_HERE
  dim:      0
  rate_limit:
    max_inflight: 4      # embedding HTTP concurrency, independent from LLM
  # api: openai | multimodal   # optional; default openai. "multimodal" for
                               # vision-style embedding endpoints.
  # max_embed: 2               # optional; skill_hub/search 语义通道的全局并发上限
                               # （在飞的 query embed 数）。抢不到即降级纯 BM25；
                               # 设 0 关闭语义通道。默认 2。
  # search_timeout_s: 3.0      # optional; skill_hub/search query embed 的独立短
                               # 超时秒数（不复用 EmbedClient 的 60s），超时即降级。
                               # 默认 3.0。

# ===== Pricing (optional; for `xskill stats` cost estimation) =====
# Cost is ESTIMATED from response.usage tokens × price (USD per 1M tokens).
# Resolution order: this `pricing:` map  >  the vendored price table
# (src/xskill/data/model_prices.json, refreshed at build time)  >  `default`.
# Leave this out entirely to rely on the vendored table + default below.
# pricing:
#   default: { input_per_1m: 1.0, output_per_1m: 3.0, embed_per_1m: 0.05 }
#   deepseek-v4-flash: { input_per_1m: 0.14, output_per_1m: 0.28, cache_hit_per_1m: 0.014 }

# ===== Canary (gradual rollout) =====
canary:
  enabled:       true
  probability:   0.2            # on a retrieval hit, route to staging with prob p
  min_samples:   5              # need >= N UX scores on each side to decide
                                # (single-bucket / un-scoped path)
  max_days_hold: 14             # max staging lifetime; discarded on timeout
  rotate_interval: 300          # standalone canary time-window rotation (seconds)
  scope_top_n:   2              # model-scoped canary: only the top-N user models
                                # by usage take part (routing + scoring); unknown
                                # and non-top-N traffic stays on main
  total_samples: 20             # model-scoped path: total UX scores needed on
                                # each side before a weighted decision
  jam_threshold: 50             # traj-jam breaker: while staging is open, gate-1
                                # holds all SkillEdit; if pending candidates'
                                # weightscore sums to >= this, declare a jam
                                # (gray misaligned / no real traffic), bypass gray:
                                # merge main+staging+candidates into a new main and
                                # discard staging. Must exceed the graduation
                                # threshold (10) so normal increments aren't misread.

# ===== Skill description trigger optimization =====
# Before each promotion commit (baby→main / main→staging) the daemon runs a
# deterministic hill-climb to tune the SKILL.md frontmatter `description` for
# trigger accuracy: it generates ~N eval queries, asks the LLM-as-judge which
# skill it would invoke, iteratively rewrites the description, and keeps the
# variant that scores highest on a held-out TEST split (anti-overfit). All LLM
# calls reuse the `llm` above (rate_limit applies). Failure never blocks the
# commit. Archived under `<skill>/.description_optimization/` (NOT versioned).
skill_opt:
  enabled:            true   # set false to disable optimization entirely (no-op)
  n_cases:            20     # eval queries generated per skill (cached + reused)
  runs_per_case:      3      # probe runs per query; trigger = hit >= 0.5 of runs
  max_iters:          5      # max improve-description iterations (candidates)
  max_llm_calls:      400    # hard cap on total LLM/probe calls per run
  train_frac:         0.6    # stratified train fraction; rest is held-out test
  seed:               42     # fixed RNG seed → deterministic split
  catalog_max_skills: 12     # decoy-catalog size (mirrors CC listing budget)
  catalog_desc_cap:   256    # per-skill description truncation fed to the probe
  probe_case_timeout: 60     # per-probe-case wall-clock cap (seconds); a stuck
                             # probe counts as "not triggered" instead of
                             # hanging the optimization loop. 0 disables.
  rerun_enabled:      true    # dashboard "re-run case" action endpoint on/off

# ===== Server (uvicorn/FastAPI runtime knobs) =====
server:
  thread_pool_tokens: 80           # anyio 同步路由线程池容量（画像刷新使用独立 worker）
  team_sync_workers: 32            # team /sync 独立线程池，不占用 dashboard/普通同步路由
  profile_refresh_workers: 4       # 用户画像后台刷新固定并发数
  profile_refresh_queue_size: 1024 # 待刷新 client 的有界队列容量
  profile_refresh_settle_delay: 5  # sync 波次入队后再启动画像计算的秒数
  profile_refresh_shutdown_timeout: 5 # 停机等待画像 worker 的最长秒数
  profile_refresh_interval: 600    # 画像短命子进程调度周期(秒;画像变化慢,默认 10min,与 watcher 解耦)
  profile_refresh_timeout: 1800    # 单轮画像子进程硬上限(秒;冷启动大量 client 兜底)
  ux_scores_sync_interval: 30      # 盘上 .ux_scores.jsonl → registry.db 同步周期(秒)
  ux_scores_sync_timeout: 300      # 单轮 UX 同步子进程硬上限(秒)
  vector_sync_batch_limit: 256     # 向量对账单轮最多处理的 catalog_key 数(issue #328;万级目录靠这个拆成多轮)
  recommend_heavy_memory_budget_mb: 1024 # recommend-heavy 单轮峰值 RSS 软上限(MiB);超了本轮提前中止,留给下一轮

# ===== Watcher (scan scheduling only) =====
watcher:
  poll_interval: 5              # seconds between scans of every watch_dir
  full_reconcile_interval: 60   # idle polls only stat the directory; this
                                # periodic full scan catches in-place rewrites

# ===== Logical Task Graph =====
# Semantic branch above Session/Atom; it never changes Atom→Skill routing.
task_graph:
  enabled: true                  # default-on; set false to pause projection while retaining dirty fences
  top_k: 8                      # hard bound on classified candidates per Atom
  recent_k: 6                   # same-Session recent Task candidates
  posting_cap: 64               # bounded inverted-index posting list
  max_scopes_per_run: 4         # fairness bound for one background pass
  source_cache_size: 128        # bounded in-memory cache for unchanged source evidence
  llm_adjudication:
    enabled: false               # opt-in; raw Atom/Task text may be sent to llm
    auto_confirm: false          # false keeps same-task judgements as proposed
    max_judgements_per_build: 64 # hard cost bound; one failure opens a build-local circuit
    # llm:                       # optional partial override; otherwise uses top-level llm
    #   model: task-link-model
    #   max_tokens: 800          # hard-capped at 800 even when configured higher

# ===== Persistent agent worker =====
# Every pool has an automatic waiting capacity of workers * 2. Running plus
# waiting capacity is workers * 3; a full pool never blocks the watcher.
agent_worker:
  pools:
    split:
      workers: 24
      llm_weight: 6
    cluster:
      workers: 8
      batch_size: 8
      llm_weight: 3
    edit:
      workers: 4
      batch_size: 5
      llm_weight: 1
    embed:
      workers: 4

# ===== Ingest (bridging native agent sessions into traj_*.md) =====
# 各生态 session ingester（claude_code / codex / openclaw / cursor 的 JSONL
# 桥接）入库行为。
ingest:
  settle_seconds: 120   # 入库完成屏障：源 session 文件最后修改距今 < N 秒视为
                        # "还在写"，本轮不入库，等停笔满 N 秒后的下一轮 poll 再
                        # 转换——避免把刚开跑的 session 定格成只有题面的残骸。
                        # 已入库后源文件又增长的 session 会被重新转换覆盖
                        # （并重置该轨迹已拆出的 atom，等价 rebuild --traj）。
                        # 真实用户 session 动辄几十分钟，调太小会截断；
                        # 评测场景（脚本批量产 session、写完即定稿）建议 5~15。
  mask_patterns: []     # 去壳掩码：正则列表。入库转换写 md 之前，把命中的文本段
                        # 替换为 [MASKED_HARNESS_PROMPT] 占位符——用于剥掉评测
                        # harness 每题固定的 turn-0 提示词，防聚类被任务外壳吸住。
                        # 默认空列表 = 完全不替换（现网用户不受影响）。
                        # 跨行匹配用内联 flag，例：'(?s)HARNESS_BEGIN.*?HARNESS_END'

# ===== Team C/S mode (only read by `xskill serve --server`) =====
# 仅 server 端读这一段。客户端（`xskill connect <host:port> --token ...`）是瘦
# 进程，不读 config.yaml——连接信息落 ~/.xskill/team_client.json；每个 server 的
# 上传游标 / 去抖 / 安装历史独立落 ~/.xskill/clients/<server_id>/，换 server 互不
# 污染。server 启动打印的 join token 落 ~/.xskill/team_server.json，再发给客户端。
team:
  server:
    traj_root:    ~/.xskill/team_trajectories  # 收下的客户端上传轨迹根目录
    skill_slots:  100   # 每个客户端 manifest 的技能槽位上限（ranked + recommended）
    ranked_slots: 80    # 其中按 UX 分排名占的槽位；剩余（100-80=20）留给向量推荐
    allow_anonymous_user: true   # false 时拒绝不带 --name 的匿名 connect（403）；
                                 # true（缺省）允许匿名，沿用既有 uuid/hashid 逻辑
    allow_read_others: false     # false（缺省）时 traj/atom read 只能读自己工号目录；
                                 # true 时允许读他人已上传轨迹

# ===== Skill recommend engine =====
# 用户画像 + skill 特征 + 推荐引擎参数。仅 team server 端生效。
recommend:
  quality_ratio:   0.8   # 已废弃：引擎忽略；recommended 纯相关性轮询，不足时 UX 回填
  cluster_centers: 5     # 用户兴趣聚类中心上限（≤5）；atom 少时自动降 k
  last_n_atoms:    5     # skill.atom_feat 取最近 N 个被路由 atom 摘要的均值
  # staging_need:   5    # 可选；缺省 None = 复用 canary.min_samples（推荐侧达量阈值，
                          # 比 total_samples 更适合小团队；显式配置可覆盖）

# ===== SkillHub (optional third-party skill directory, CS mode) =====
# 启用后扫描该目录下的三方 SKILL.md，按 description 向量化纳入推荐检索池
# （仅进相关性位，不进质量位/灰度）。三方 skill 无 git 分支/灰度基础设施。
skillhub:
  enabled: false                      # 缺省关；true 才扫描
  dir:     ~/.xskill/skillhub_skills  # 三方 skill 目录；启用时缺失会抛错（不静默跳过）
  scan_ttl_seconds: 3600              # L1 快照 TTL；过期后下一次访问才惰性扫盘（非定时）

# ===== Dashboard (the built-in web console served by `xskill serve`) =====
dashboard:
  enabled:  false      # 设 true 才挂载控制台到 serve 的 /
  public:   false      # 默认仅本机可达；true 才放行公网（仅看板路由）
  password: ""         # 可选；非空时看板要求 HTTP Basic 登录（API 不受影响）
  # 历史轨迹没记 coding agent(harness) / 模型(source_model) 时，看板按什么归类。
  # 留空 → 'unknown'（保持原行为）。填了 → 这些缺失字段的轨迹归到该值的桶里。
  # 仅影响看板的“生态/模型”分组展示，不改库里的真实值，也不影响 canary 路由。
  default_harness: ""  # 例：claude_code（须是已知 harness 才会并入现有分组）
  default_model:   ""  # 例：deepseek-v4-flash（模型名无封闭集，自由填）
"""


def ensure_config_exists(path: Optional[Path] = None) -> bool:
    """首次运行 auto-init：config.yaml 不存在时写出 CONFIG_TEMPLATE。

    返回值：
        True  —— 配置已存在（什么都没做）
        False —— 刚刚创建了模板（调用方应提示用户填 key 后重跑）
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    if cfg_path.exists():
        return True
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return False


def _positive_int(value, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{path} 必须是正整数")
    return value


def _positive_int_or_default(value, path: str, default: int) -> int:
    if value is None:
        return default
    return _positive_int(value, path)


def normalize_runtime_config(config_data: dict) -> dict:
    """Return a validated runtime copy compatible with v0.6.27 configs.

    Compatibility defaults are deliberately applied in memory.  The user's
    YAML remains untouched, while every runtime consumer sees the canonical
    four-pool and request-limit shape introduced in v0.6.28.
    """
    if not isinstance(config_data, dict):
        raise ValueError("config.yaml 顶层必须是 mapping")
    runtime_config = copy.deepcopy(config_data)

    watcher = runtime_config.get("watcher") or {}
    if not isinstance(watcher, dict):
        raise ValueError("watcher 必须是 mapping")
    legacy_max_concurrent = None
    if "max_concurrent" in watcher:
        legacy_max_concurrent = _positive_int(
            watcher["max_concurrent"], "watcher.max_concurrent",
        )
    legacy_batch_size = None
    if "cluster_batch_size" in watcher:
        legacy_batch_size = _positive_int(
            watcher["cluster_batch_size"], "watcher.cluster_batch_size",
        )
    watcher.pop("max_concurrent", None)
    watcher.pop("cluster_batch_size", None)
    runtime_config["watcher"] = watcher

    task_graph = runtime_config.get("task_graph")
    if task_graph is None:
        task_graph = {}
    if not isinstance(task_graph, dict):
        raise ValueError("task_graph 必须是 mapping")
    task_graph = dict(task_graph)
    enabled = task_graph.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("task_graph.enabled 必须是布尔")
    task_graph["enabled"] = enabled
    for field_name, default in (
        ("top_k", 8), ("recent_k", 6),
        ("posting_cap", 64), ("max_scopes_per_run", 4),
        ("source_cache_size", 128),
    ):
        task_graph[field_name] = _positive_int_or_default(
            task_graph.get(field_name), f"task_graph.{field_name}", default,
        )
    llm_adjudication = task_graph.get("llm_adjudication")
    if llm_adjudication is None:
        llm_adjudication = {}
    if not isinstance(llm_adjudication, dict):
        raise ValueError("task_graph.llm_adjudication 必须是 mapping")
    llm_adjudication = dict(llm_adjudication)
    for field_name, default in (("enabled", False), ("auto_confirm", False)):
        value = llm_adjudication.get(field_name, default)
        if not isinstance(value, bool):
            raise ValueError(
                f"task_graph.llm_adjudication.{field_name} 必须是布尔"
            )
        llm_adjudication[field_name] = value
    llm_adjudication["max_judgements_per_build"] = _positive_int_or_default(
        llm_adjudication.get("max_judgements_per_build"),
        "task_graph.llm_adjudication.max_judgements_per_build",
        64,
    )
    adjudication_llm = llm_adjudication.get("llm")
    if adjudication_llm is not None and not isinstance(adjudication_llm, dict):
        raise ValueError("task_graph.llm_adjudication.llm 必须是 mapping")
    task_graph["llm_adjudication"] = llm_adjudication
    runtime_config["task_graph"] = task_graph

    worker = runtime_config.get("agent_worker")
    if worker is None:
        worker = {}
    if not isinstance(worker, dict):
        raise ValueError("agent_worker 必须是 mapping")
    pools = worker.get("pools")
    if pools is None:
        pools = {}
    if not isinstance(pools, dict):
        raise ValueError("agent_worker.pools 必须是 mapping")
    normalized_pools: dict[str, dict] = {}
    for name, defaults in DEFAULT_AGENT_WORKER_POOLS.items():
        pool = pools.get(name)
        if pool is None:
            pool = {}
        if not isinstance(pool, dict):
            raise ValueError(f"agent_worker.pools.{name} 必须是 mapping")
        if "queue_size" in pool:
            raise ValueError(
                f"不接受 agent_worker.pools.{name}.queue_size；等待容量自动为 workers × 2"
            )
        normalized_pool = {**defaults, **pool}
        normalized_pool["workers"] = _positive_int(
            normalized_pool["workers"], f"agent_worker.pools.{name}.workers"
        )
        if name in ("split", "cluster", "edit"):
            normalized_pool["llm_weight"] = _positive_int(
                normalized_pool["llm_weight"],
                f"agent_worker.pools.{name}.llm_weight",
            )
        normalized_pools[name] = normalized_pool
    cluster_batch_size = normalized_pools["cluster"].get("batch_size")
    if "cluster" not in pools or "batch_size" not in pools["cluster"]:
        cluster_batch_size = (
            legacy_batch_size
            if legacy_batch_size is not None
            else DEFAULT_AGENT_WORKER_POOLS["cluster"]["batch_size"]
        )
    normalized_pools["cluster"]["batch_size"] = _positive_int(
        cluster_batch_size,
        "agent_worker.pools.cluster.batch_size",
    )
    normalized_pools["edit"]["batch_size"] = _positive_int(
        normalized_pools["edit"].get("batch_size"),
        "agent_worker.pools.edit.batch_size",
    )
    worker = dict(worker)
    worker["pools"] = normalized_pools
    runtime_config["agent_worker"] = worker

    llm = runtime_config.get("llm") or {}
    if not isinstance(llm, dict):
        raise ValueError("llm 必须是 mapping")
    llm = dict(llm)
    llm_rate = llm.get("rate_limit") or {}
    if not isinstance(llm_rate, dict):
        raise ValueError("llm.rate_limit 必须是 mapping")
    llm_rate = dict(llm_rate)
    legacy_burst = llm_rate.pop("burst", None)
    if legacy_burst is not None:
        legacy_burst = _positive_int(legacy_burst, "llm.rate_limit.burst")
    llm_rate["rpm"] = _positive_int_or_default(
        llm_rate.get("rpm"), "llm.rate_limit.rpm",
        DEFAULT_LLM_RATE_LIMIT["rpm"],
    )
    llm_rate["request_burst"] = _positive_int_or_default(
        llm_rate.get("request_burst"), "llm.rate_limit.request_burst",
        legacy_burst or DEFAULT_LLM_RATE_LIMIT["request_burst"],
    )
    llm_rate["max_inflight"] = _positive_int_or_default(
        llm_rate.get("max_inflight"), "llm.rate_limit.max_inflight",
        legacy_max_concurrent or DEFAULT_LLM_RATE_LIMIT["max_inflight"],
    )
    if "tpm" in llm_rate:
        tpm = _positive_int(llm_rate["tpm"], "llm.rate_limit.tpm")
        llm_rate["token_burst"] = _positive_int_or_default(
            llm_rate.get("token_burst"), "llm.rate_limit.token_burst",
            legacy_burst or max(1, tpm // 6),
        )
    if "token_burst" in llm_rate:
        _positive_int(llm_rate["token_burst"], "llm.rate_limit.token_burst")
    llm["rate_limit"] = llm_rate
    runtime_config["llm"] = llm

    llm_skill = runtime_config.get("llm_skill")
    if llm_skill is not None:
        if not isinstance(llm_skill, dict):
            raise ValueError("llm_skill 必须是 mapping")
        llm_skill = dict(llm_skill)
        if "rate_limit" in llm_skill:
            skill_rate = llm_skill.get("rate_limit") or {}
            if not isinstance(skill_rate, dict):
                raise ValueError("llm_skill.rate_limit 必须是 mapping")
            skill_rate = dict(skill_rate)
            skill_burst = skill_rate.pop("burst", None)
            if skill_burst is not None:
                skill_burst = _positive_int(
                    skill_burst, "llm_skill.rate_limit.burst",
                )
                skill_rate.setdefault("request_burst", skill_burst)
                if "tpm" in skill_rate:
                    skill_rate.setdefault("token_burst", skill_burst)
            llm_skill["rate_limit"] = skill_rate
        runtime_config["llm_skill"] = llm_skill

    llm_agents = runtime_config.get("llm_agents")
    if llm_agents is not None:
        if not isinstance(llm_agents, dict):
            raise ValueError("llm_agents 必须是 mapping")
        unknown_stages = set(llm_agents) - {"split", "cluster", "edit"}
        if unknown_stages:
            raise ValueError(
                f"llm_agents 包含未知阶段: {sorted(unknown_stages)!r}"
            )
        normalized_agents: dict[str, dict] = {}
        for stage, stage_value in llm_agents.items():
            if not isinstance(stage_value, dict):
                raise ValueError(f"llm_agents.{stage} 必须是 mapping")
            stage_cfg = dict(stage_value)
            if "rate_limit" in stage_cfg:
                stage_rate = stage_cfg.get("rate_limit")
                if stage_rate is None:
                    stage_rate = {}
                if not isinstance(stage_rate, dict):
                    raise ValueError(
                        f"llm_agents.{stage}.rate_limit 必须是 mapping"
                    )
                stage_rate = dict(stage_rate)
                stage_burst = stage_rate.pop("burst", None)
                if stage_burst is not None:
                    stage_burst = _positive_int(
                        stage_burst,
                        f"llm_agents.{stage}.rate_limit.burst",
                    )
                    stage_rate.setdefault("request_burst", stage_burst)
                    if "tpm" in stage_rate:
                        stage_rate.setdefault("token_burst", stage_burst)
                stage_cfg["rate_limit"] = stage_rate
            normalized_agents[stage] = stage_cfg
        runtime_config["llm_agents"] = normalized_agents

    embedding = runtime_config.get("embedding") or {}
    if not isinstance(embedding, dict):
        raise ValueError("embedding 必须是 mapping")
    embedding = dict(embedding)
    embedding_rate = embedding.get("rate_limit") or {}
    if not isinstance(embedding_rate, dict):
        raise ValueError("embedding.rate_limit 必须是 mapping")
    embedding_rate = dict(embedding_rate)
    embedding_rate["max_inflight"] = _positive_int_or_default(
        embedding_rate.get("max_inflight"),
        "embedding.rate_limit.max_inflight",
        DEFAULT_EMBEDDING_MAX_INFLIGHT,
    )
    embedding["rate_limit"] = embedding_rate
    runtime_config["embedding"] = embedding
    return runtime_config


def agent_worker_config(config_data: dict) -> dict:
    """Return the validated four-pool config, including legacy defaults."""
    return normalize_runtime_config(config_data)["agent_worker"]


def read_agent_worker_pools(path: Optional[Path] = None) -> dict:
    """Read ``agent_worker.pools`` from config.yaml (validated, with defaults)."""
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"xskill config not found: {config_path}")
    with open(config_path, encoding="utf-8") as config_file:
        config_data = yaml.safe_load(config_file) or {}
    if not isinstance(config_data, dict):
        raise ValueError("config.yaml 顶层必须是 mapping")
    return agent_worker_config(config_data)["pools"]


def patch_agent_worker_pool_yaml(
    raw: str,
    pool: str,
    *,
    workers: Optional[int] = None,
    llm_weight: Optional[int] = None,
) -> str:
    """Update one pool's hot fields in a config.yaml body, keeping comments
    when the keys already exist. Missing structure falls back to a dump of
    the parsed mapping (comments in that case are not preserved).
    """
    if pool not in ("split", "cluster", "edit"):
        raise ValueError("pool 必须是 split、cluster 或 edit")
    if workers is None and llm_weight is None:
        raise ValueError("workers 与 llm_weight 至少提供一个")
    if workers is not None:
        workers = _positive_int(workers, f"agent_worker.pools.{pool}.workers")
    if llm_weight is not None:
        llm_weight = _positive_int(
            llm_weight, f"agent_worker.pools.{pool}.llm_weight",
        )
    try:
        cfg = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml 顶层必须是 mapping")
    pools = ((cfg.get("agent_worker") or {}).get("pools") or {})
    current = dict(pools.get(pool) or {})
    if workers is not None:
        current["workers"] = workers
    if llm_weight is not None:
        current["llm_weight"] = llm_weight
    agent_worker = dict(cfg.get("agent_worker") or {})
    all_pools = dict(agent_worker.get("pools") or {})
    all_pools[pool] = current
    agent_worker["pools"] = all_pools
    cfg["agent_worker"] = agent_worker
    agent_worker_config(cfg)

    new_raw = raw
    ok = True
    if workers is not None:
        new_raw, ok = _replace_or_insert_pool_key(new_raw, pool, "workers", workers)
    if ok and llm_weight is not None:
        new_raw, ok = _replace_or_insert_pool_key(
            new_raw, pool, "llm_weight", llm_weight,
        )
    if ok:
        return new_raw
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def _replace_or_insert_pool_key(
    raw: str, pool: str, field: str, value: int,
) -> tuple[str, bool]:
    """Replace ``field: N`` inside ``agent_worker.pools.<pool>``. Return
    ``(text, False)`` if that block cannot be found.
    """
    lines = raw.splitlines(keepends=True)
    if not lines:
        return raw, False
    agent_i = pools_i = pool_i = None
    agent_indent = pools_indent = pool_indent = None
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        key = stripped.split(":", 1)[0].strip()
        if key == "agent_worker" and agent_i is None:
            agent_i, agent_indent = index, indent
            continue
        if agent_i is not None and indent <= agent_indent and index > agent_i:
            break
        if agent_i is None:
            continue
        if key == "pools" and indent > agent_indent and pools_i is None:
            pools_i, pools_indent = index, indent
            continue
        if pools_i is None:
            continue
        if indent <= pools_indent and index > pools_i:
            break
        if key == pool and indent > pools_indent:
            pool_i, pool_indent = index, indent
            break
    if pool_i is None or pool_indent is None:
        return raw, False
    field_line = None
    insert_at = pool_i + 1
    for index in range(pool_i + 1, len(lines)):
        line = lines[index]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent <= pool_indent:
            break
        key = stripped.split(":", 1)[0].strip()
        insert_at = index + 1
        if key == field:
            field_line = index
            break
    replacement = f"{field}: {value}"
    if field_line is not None:
        line = lines[field_line]
        indent_txt = line[: len(line) - len(line.lstrip(" \t"))]
        comment = ""
        rest = line.lstrip(" \t").split(":", 1)[1]
        if "#" in rest:
            comment = "  #" + rest.split("#", 1)[1].rstrip("\r\n")
        newline = "\n" if line.endswith("\n") else ""
        if line.endswith("\r\n"):
            newline = "\r\n"
        lines[field_line] = f"{indent_txt}{replacement}{comment}{newline}"
        return "".join(lines), True
    indent_txt = " " * (pool_indent + 2)
    newline = "\n"
    if lines[pool_i].endswith("\r\n"):
        newline = "\r\n"
    lines.insert(insert_at, f"{indent_txt}{replacement}{newline}")
    return "".join(lines), True


def load_config(path: Optional[Path] = None) -> dict:
    """加载 ~/.xskill/config.yaml；不存在直接抛 FileNotFoundError。

    正常路径下 CLI 会先调 ``ensure_config_exists`` auto-init，不会走到这个
    FileNotFoundError；保留它作为 SDK 直接调用时的 fail-loud 兜底。
    """
    global _config
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"xskill config not found: {cfg_path}\n"
            f"Run `xskill serve` once to auto-create a template, "
            f"or call config.ensure_config_exists()."
        )
    with open(cfg_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f) or {}
    _config = normalize_runtime_config(raw_config)
    if not _config.get("llm", {}).get("api_key"):
        raise KeyError(f"llm.api_key missing in {cfg_path}")
    if not _config.get("embedding", {}).get("api_key"):
        raise KeyError(f"embedding.api_key missing in {cfg_path}")
    return _config


def interests_config(config_data: Optional[dict] = None) -> list[str]:
    """Return normalized top-level interests.

    Missing or empty interests disable filtering. Values must be a list of
    strings; each string is stripped and blank entries are removed.
    """
    source_config = config_data or {}
    raw_interests = source_config.get("interests") or []
    if not isinstance(raw_interests, list):
        raise ValueError(
            f"interests 必须是字符串列表，got {type(raw_interests).__name__}"
        )
    normalized_interests: list[str] = []
    for interest_index, interest_value in enumerate(raw_interests):
        if not isinstance(interest_value, str):
            raise ValueError(
                "interests"
                f"[{interest_index}] 必须是字符串，got {type(interest_value).__name__}"
            )
        normalized_interest = interest_value.strip()
        if normalized_interest:
            normalized_interests.append(normalized_interest)
    return normalized_interests


def interests_fingerprint(interests: list[str]) -> str:
    """Return an order-sensitive fingerprint for normalized interests."""
    normalized_interests = interests_config({"interests": interests})
    serialized_interests = json.dumps(
        normalized_interests, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized_interests.encode("utf-8")).hexdigest()


def read_interests_config(path: Optional[Path] = None) -> list[str]:
    """Read only top-level interests from config.yaml without client validation."""
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        return []
    with open(config_path, encoding="utf-8") as config_file:
        config_data = yaml.safe_load(config_file) or {}
    if not isinstance(config_data, dict):
        raise ValueError("config.yaml 顶层必须是 mapping")
    return interests_config(config_data)


def get_config() -> dict:
    if not _config:
        load_config()
    return _config


def _resolve_attribution(dashboard_section: dict) -> dict:
    """把 dashboard 段的 default_harness / default_model 解析成看板用的归类标签。

    留空（缺省 / 空串 / 全空白）→ 'unknown'，即保持历史行为；非空则去首尾空白后
    原样用作缺失字段的归类桶。harness 不在此做白名单校验——按设计取自由字符串。
    """
    return {
        "harness": str(dashboard_section.get("default_harness") or "").strip() or "unknown",
        "model": str(dashboard_section.get("default_model") or "").strip() or "unknown",
    }


def dashboard_config(cfg: dict) -> dict:
    """从已加载 config 取 dashboard 段，缺字段用显式默认（非 fallback 兼容）。"""
    d = cfg.get("dashboard") or {}
    attr = _resolve_attribution(d)
    admins = d.get("admins") or []
    if not isinstance(admins, list):
        raise ValueError("dashboard.admins 必须是 user_name 列表")
    return {
        "enabled": bool(d.get("enabled", False)),
        "public": bool(d.get("public", False)),
        "password": str(d.get("password", "") or ""),
        # P2-2.2(D2/Q2a):admin 名单(user_name) + admin 单独强口令。
        # admin_password 为空 = admin 登录关闭(显式缺省,非默认开)。
        "admins": [str(a).strip() for a in admins if str(a).strip()],
        "admin_password": str(d.get("admin_password", "") or ""),
        "default_harness": attr["harness"],
        "default_model": attr["model"],
    }


def dashboard_attribution_defaults(path: Optional[Path] = None) -> dict:
    """看板归类默认值（default_harness / default_model），直接读 config.yaml 的
    dashboard 段，**不校验 llm/embedding api_key**——独立只读看板实例（瘦进程，
    可能没配 key）也要能用。返回 ``{"harness": <label>, "model": <label>}``，
    留空均退 'unknown'。

    config.yaml 不存在时同样返回 'unknown' 这组默认——这是看板展示偏好的显式
    缺省（与 ``dashboard_config`` 给 enabled/password 显式默认同性质），不是吞错。
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    section: dict = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            section = (yaml.safe_load(f) or {}).get("dashboard") or {}
    return _resolve_attribution(section)


# ingest.settle_seconds 缺省值。区间权衡：真实用户 session 动辄几十分钟，
# 过短（<60s）在长思考/长工具调用间隙就会误判"已写完"提前定格；过长则新
# session 入库延迟无谓变大。120s 落在建议区间（90~180s）中段。
INGEST_SETTLE_SECONDS_DEFAULT = 120.0


def ingest_config(path: Optional[Path] = None) -> dict:
    """读 config.yaml 的 ingest 段，缺字段用显式默认（非 fallback 兼容）。

    与 ``dashboard_attribution_defaults`` 同性质：**不校验 llm/embedding
    api_key**——team 瘦客户端 / 一次性 CLI 桥接（没配 key 的环境）也要能
    桥轨迹；config.yaml 不存在时返回全默认。

    返回 ``{"settle_seconds": float, "mask_patterns": list[str]}``。
    ``mask_patterns`` 在此即编译校验——坏正则 / 非列表直接抛 ValueError
    （CLAUDE.md：遇到问题 throw error，不静默吞掉让掩码失效）。
    """
    import re as _re

    cfg_path = Path(path) if path else CONFIG_PATH
    section: dict = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            section = (yaml.safe_load(f) or {}).get("ingest") or {}

    settle = section.get("settle_seconds", INGEST_SETTLE_SECONDS_DEFAULT)
    raw_patterns = section.get("mask_patterns") or []
    if not isinstance(raw_patterns, list):
        raise ValueError(
            f"ingest.mask_patterns 必须是正则列表，got {type(raw_patterns).__name__}"
        )
    patterns: list[str] = []
    for i, p in enumerate(raw_patterns):
        if not isinstance(p, str):
            raise ValueError(
                f"ingest.mask_patterns[{i}] 必须是字符串正则，got {type(p).__name__}"
            )
        try:
            _re.compile(p)
        except _re.error as e:
            raise ValueError(
                f"ingest.mask_patterns[{i}] 不是合法正则 {p!r}: {e}"
            ) from e
        patterns.append(p)
    return {"settle_seconds": float(settle), "mask_patterns": patterns}


# ─── recommend / skillhub / allow_anonymous ──────────────────────
# dict-based 读取（运行时拿已加载的 config dict），与 dashboard_config 同风格：
# 显式默认、坏类型抛 ValueError（fail-loud，不静默兜底）。


def recommend_config(cfg: Optional[dict] = None) -> dict:
    """读 config 的 ``recommend`` 段，显式默认。

    返回 ``{quality_ratio, cluster_centers, last_n_atoms, staging_need}``。
    ``staging_need`` 缺省 None = 复用 ``canary.min_samples``（推荐侧达量阈值，
    比 total_samples 更适合小团队；引擎构造时解析）。
    ``quality_ratio`` 仍校验/返回以兼容旧 yaml，推荐引擎已忽略。
    """
    section = (cfg or {}).get("recommend") or {}
    quality_ratio = section.get("quality_ratio", 0.8)
    if not isinstance(quality_ratio, (int, float)) or isinstance(quality_ratio, bool):
        raise ValueError(
            f"recommend.quality_ratio 必须是数值，got {type(quality_ratio).__name__}"
        )
    quality_ratio = float(quality_ratio)
    if not 0.0 <= quality_ratio <= 1.0:
        raise ValueError(
            f"recommend.quality_ratio 必须在 [0,1]，got {quality_ratio}"
        )
    cluster_centers = section.get("cluster_centers", 5)
    if not isinstance(cluster_centers, int) or isinstance(cluster_centers, bool):
        raise ValueError(
            f"recommend.cluster_centers 必须是整数，got {type(cluster_centers).__name__}"
        )
    if cluster_centers < 1:
        raise ValueError(
            f"recommend.cluster_centers 必须 >= 1，got {cluster_centers}"
        )
    last_n_atoms = section.get("last_n_atoms", 5)
    if not isinstance(last_n_atoms, int) or isinstance(last_n_atoms, bool):
        raise ValueError(
            f"recommend.last_n_atoms 必须是整数，got {type(last_n_atoms).__name__}"
        )
    if last_n_atoms < 1:
        raise ValueError(
            f"recommend.last_n_atoms 必须 >= 1，got {last_n_atoms}"
        )
    staging_need = section.get("staging_need")
    if staging_need is not None:
        if not isinstance(staging_need, int) or isinstance(staging_need, bool):
            raise ValueError(
                f"recommend.staging_need 必须是整数或 null，got {type(staging_need).__name__}"
            )
        if staging_need < 1:
            raise ValueError(
                f"recommend.staging_need 必须 >= 1，got {staging_need}"
            )
    return {
        "quality_ratio": quality_ratio,
        "cluster_centers": int(cluster_centers),
        "last_n_atoms": int(last_n_atoms),
        "staging_need": staging_need,
    }


def skillhub_config(cfg: Optional[dict] = None) -> dict:
    """读 config 的 ``skillhub`` 段，显式默认。

    返回 ``{enabled: bool, dir: Path, scan_ttl_seconds: float}``。
    ``dir`` 缺省 ``~/.xskill/skillhub_skills``；``scan_ttl_seconds`` 缺省 3600。
    目录是否存在不在此校验——由 ``SkillHub`` 初始化时按 no-fallback 抛错。
    """
    section = (cfg or {}).get("skillhub") or {}
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"skillhub.enabled 必须是布尔，got {type(enabled).__name__}"
        )
    raw_dir = section.get("dir") or str(XSKILL_HOME / "skillhub_skills")
    if not isinstance(raw_dir, str):
        raise ValueError(
            f"skillhub.dir 必须是字符串路径，got {type(raw_dir).__name__}"
        )
    scan_ttl_seconds = section.get("scan_ttl_seconds", 3600.0)
    if (
        not isinstance(scan_ttl_seconds, (int, float))
        or isinstance(scan_ttl_seconds, bool)
        or not math.isfinite(scan_ttl_seconds)
        or scan_ttl_seconds < 0
    ):
        raise ValueError(
            f"skillhub.scan_ttl_seconds 必须是非负有限数，got {scan_ttl_seconds!r}"
        )
    return {
        "enabled": enabled,
        "dir": Path(raw_dir).expanduser(),
        "scan_ttl_seconds": float(scan_ttl_seconds),
    }


def kernel_config(
    cfg: Optional[dict] = None,
    *,
    xskill_home: Optional[Path] = None,
) -> dict:
    """Read the XSkill-owned kernel selector and discovery directory.

    Kernel-private settings deliberately do not appear here.  They live at
    ``<plugin_dir>/<active>/config.yaml`` and are read only by that kernel.
    """
    from xskill.kernels.base import validate_kernel_id

    section = (cfg or {}).get("kernel") or {}
    if not isinstance(section, dict):
        raise ValueError(
            f"kernel 必须是 mapping，got {type(section).__name__}"
        )
    configured_id = section.get("kernel_id")
    legacy_active = section.get("active")
    if (
        configured_id is not None
        and legacy_active is not None
        and str(configured_id).strip() != str(legacy_active).strip()
    ):
        raise ValueError(
            "kernel.kernel_id 与兼容字段 kernel.active 不能冲突"
        )
    active = validate_kernel_id(
        configured_id if configured_id is not None else legacy_active or "native"
    )
    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    configured_path = section.get("kernels_path")
    legacy_plugin_dir = section.get("plugin_dir")
    if (
        configured_path is not None
        and legacy_plugin_dir is not None
        and str(configured_path).strip() != str(legacy_plugin_dir).strip()
    ):
        raise ValueError(
            "kernel.kernels_path 与兼容字段 kernel.plugin_dir 不能冲突"
        )
    raw_plugin_dir = (
        configured_path
        if configured_path is not None
        else legacy_plugin_dir or str(state_root / "kernels")
    )
    if not isinstance(raw_plugin_dir, str) or not raw_plugin_dir.strip():
        raise ValueError("kernel.kernels_path 必须是非空字符串路径")
    plugin_dir = Path(raw_plugin_dir).expanduser()
    if not plugin_dir.is_absolute():
        plugin_dir = state_root / plugin_dir
    return {
        "active": active,
        "plugin_dir": plugin_dir.resolve(),
    }


def embedding_search_config(cfg: Optional[dict] = None) -> dict:
    """读 config 的 ``embedding`` 段中 skill_hub/search 语义通道的护栏参数。

    返回 ``{max_embed: int, search_timeout_s: float}``。``max_embed`` 缺省 2
    （0 = 关闭语义通道纯 BM25），``search_timeout_s`` 缺省 3.0。瘦 server 缺段用缺省。
    """
    section = (cfg or {}).get("embedding") or {}
    max_embed = section.get("max_embed", 2)
    if not isinstance(max_embed, int) or isinstance(max_embed, bool) or max_embed < 0:
        raise ValueError(
            f"embedding.max_embed 必须是非负整数，got {max_embed!r}"
        )
    search_timeout_s = section.get("search_timeout_s", 3.0)
    if (not isinstance(search_timeout_s, (int, float))
            or isinstance(search_timeout_s, bool)
            or not math.isfinite(search_timeout_s)
            or search_timeout_s <= 0):
        raise ValueError(
            f"embedding.search_timeout_s 必须是正数，got {search_timeout_s!r}"
        )
    return {"max_embed": int(max_embed), "search_timeout_s": float(search_timeout_s)}


def profile_refresh_config(cfg: Optional[dict] = None) -> dict:
    """读取 team server 的后台画像刷新配置并严格校验。

    worker 数和队列容量必须是正整数，停机超时必须是正数。
    """
    section = (cfg or {}).get("server") or {}
    workers = section.get("profile_refresh_workers", 4)
    queue_size = section.get("profile_refresh_queue_size", 1024)
    settle_delay = section.get("profile_refresh_settle_delay", 5)
    shutdown_timeout = section.get("profile_refresh_shutdown_timeout", 5)
    # 画像已改为定时短命子进程(python -m xskill._workers profile-refresh):interval=
    # 调度周期,timeout=单轮子进程硬上限(冷启动大量 client 兜底)。画像变化慢、批量重,
    # 默认 600s(10min),不必频繁——与 30s 的 watcher poll 解耦,各自节奏。
    interval = section.get("profile_refresh_interval", 600)
    timeout = section.get("profile_refresh_timeout", 1800)

    for key, value in (
        ("profile_refresh_workers", workers),
        ("profile_refresh_queue_size", queue_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"server.{key} 必须是正整数，got {value!r}")
    for key, value, allow_zero in (
        ("profile_refresh_settle_delay", settle_delay, True),
        ("profile_refresh_shutdown_timeout", shutdown_timeout, False),
        ("profile_refresh_interval", interval, False),
        ("profile_refresh_timeout", timeout, False),
    ):
        if (not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)):
            qualifier = "非负数" if allow_zero else "正数"
            raise ValueError(f"server.{key} 必须是{qualifier}，got {value!r}")
    return {
        "workers": workers,
        "queue_size": queue_size,
        "settle_delay": float(settle_delay),
        "shutdown_timeout": float(shutdown_timeout),
        "interval": float(interval),
        "timeout": float(timeout),
    }


def recommend_heavy_config(cfg: Optional[dict] = None) -> dict:
    """recommend-heavy 重活单轮的批大小与内存预算（issue #328）。

    ``vector_sync_batch_limit``：全量/增量向量对账单次调用最多处理多少
    ``catalog_key``——万级目录时全量重建会被这个上限拆成多轮，而不是
    一轮吃掉整份 catalog。``memory_budget_mb``：单轮峰值 RSS 软上限，
    超过就中止本轮剩余批次（已处理的部分正常生效），留给下一轮继续；
    默认给一个对 16 GiB 主机安全、能跟 Docker/看板/客户端共存的值。
    """
    section = (cfg or {}).get("server") or {}
    batch_limit = section.get("vector_sync_batch_limit", 256)
    memory_budget_mb = section.get("recommend_heavy_memory_budget_mb", 1024)

    if not isinstance(batch_limit, int) or isinstance(batch_limit, bool) or batch_limit < 1:
        raise ValueError(
            f"server.vector_sync_batch_limit 必须是正整数，got {batch_limit!r}"
        )
    if (
        not isinstance(memory_budget_mb, (int, float))
        or isinstance(memory_budget_mb, bool)
        or not math.isfinite(memory_budget_mb)
        or memory_budget_mb <= 0
    ):
        raise ValueError(
            "server.recommend_heavy_memory_budget_mb 必须是正数，"
            f"got {memory_budget_mb!r}"
        )
    return {
        "batch_limit": batch_limit,
        "memory_budget_mb": float(memory_budget_mb),
    }


def ux_scores_sync_config(cfg: Optional[dict] = None) -> dict:
    """盘上 UX jsonl → registry.db 定时同步配置。"""
    section = (cfg or {}).get("server") or {}
    interval = section.get("ux_scores_sync_interval", 30)
    timeout = section.get("ux_scores_sync_timeout", 300)
    for key, value in (
        ("ux_scores_sync_interval", interval),
        ("ux_scores_sync_timeout", timeout),
    ):
        if (not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0):
            raise ValueError(f"server.{key} 必须是正数，got {value!r}")
    return {"interval": float(interval), "timeout": float(timeout)}


def team_sync_config(cfg: Optional[dict] = None) -> dict:
    """读取 team ``/sync`` 专用线程池配置并严格校验。"""
    section = (cfg or {}).get("server") or {}
    workers = section.get("team_sync_workers", 32)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError(
            "server.team_sync_workers 必须是正整数，"
            f"got {workers!r}"
        )
    return {"workers": workers}


def _team_server_section(cfg: Optional[dict]) -> dict:
    """取 ``team.server`` 段，缺省/为空一律 {}；畸形则抛带原因的 ValueError。

    注意不能写 ``.get("team", {})``——YAML 里一个光杆 ``team:`` 会解析成
    **键存在但值为 None**，默认值不生效，``None.get`` 直接 AttributeError
    （会穿透调用方的 except ValueError，把 400 变成 500）。故用 ``or {}``。
    """
    team = (cfg or {}).get("team") or {}
    if not isinstance(team, dict):
        raise ValueError(f"team 必须是 mapping，got {type(team).__name__}")
    section = team.get("server") or {}
    if not isinstance(section, dict):
        raise ValueError(f"team.server 必须是 mapping，got {type(section).__name__}")
    return section


def team_server_slots_config(cfg: Optional[dict] = None) -> dict:
    """读 ``team.server.skill_slots`` / ``ranked_slots``，缺省 100 / 80。

    这两个是**纯调优数字**，读方每次现取即热生效（不进 ``_ctx`` 启动快照），
    故此处必须 fail-loud 校验：非法值直接 raise，绝不静默取默认。
    """
    section = _team_server_section(cfg)
    skill_slots = section.get("skill_slots", 100)
    ranked_slots = section.get("ranked_slots", 80)
    for name, val in (("skill_slots", skill_slots), ("ranked_slots", ranked_slots)):
        if isinstance(val, bool) or not isinstance(val, int):
            raise ValueError(
                f"team.server.{name} 必须是整数，got {type(val).__name__}")
        if val < 0:
            raise ValueError(f"team.server.{name} 不能为负，got {val}")
    # 不校验 ranked_slots <= skill_slots：skill_slots=0 是合法的"停止分发"配置
    # （api.team_sync 直接短路），此时 ranked_slots 仍是常规 80；且 build_manifest
    # 本就用 min(ranked_slots, remaining) 夹取，多出来的部分无害。
    return {"skill_slots": skill_slots, "ranked_slots": ranked_slots}


def allow_anonymous_user(cfg: Optional[dict] = None) -> bool:
    """读 ``team.server.allow_anonymous_user``，缺省 True（向后兼容）。

    false 时 team server 拒绝不带 ``--name`` 的匿名注册。
    """
    section = _team_server_section(cfg)
    val = section.get("allow_anonymous_user", True)
    if not isinstance(val, bool):
        raise ValueError(
            "team.server.allow_anonymous_user 必须是布尔，"
            f"got {type(val).__name__}"
        )
    return val


def allow_read_others(cfg: Optional[dict] = None) -> bool:
    """读 ``team.server.allow_read_others``，缺省 False。

    false 时 team 的 traj/atom read 只能读调用者自己工号目录。
    检索卡片不受此开关限制。
    """
    section = _team_server_section(cfg)
    val = section.get("allow_read_others", False)
    if not isinstance(val, bool):
        raise ValueError(
            "team.server.allow_read_others 必须是布尔，"
            f"got {type(val).__name__}"
        )
    return val


def get_skill_dir(
    config_data: Optional[dict] = None,
    *,
    xskill_home: Optional[Path] = None,
) -> Path:
    """skill_dir: config.yaml 字段；默认 ~/.xskill/skill/"""
    config_source = config_data if config_data is not None else get_config()
    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    raw_skill_dir = config_source.get("skill_dir")
    if raw_skill_dir is not None and (
        not isinstance(raw_skill_dir, str) or not raw_skill_dir.strip()
    ):
        raise ValueError("skill_dir 必须是非空字符串路径")
    skill_dir = Path(raw_skill_dir or "skill").expanduser()
    return skill_dir if skill_dir.is_absolute() else state_root / skill_dir


def get_logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def get_kernel_console_log_path() -> Path:
    """kernel-host 子进程 stdout/stderr 落盘路径，供算法内核页实时串流。"""
    return get_logs_dir() / "xskill.kernel.log"


def get_traj_dir() -> Path:
    """默认轨迹目录 = 第一个已注册的 watch dir。

    轨迹来源的真源是 Registry——dataset 通过 ``xskill registry add <abs-path>``
    注册，daemon 启动时也会自动探测并注册各生态的 session 目录。本函数仅给
    "无显式路径" 的内部调用取一个默认目录用；新代码优先走 Registry / 显式 path。

    一个 watch dir 都没注册时直接抛错——不兜底到某个魔术目录（CLAUDE.md：
    遇到问题 throw error，不写 fallback）。
    """
    # 函数内 import：registry 反过来依赖 config，模块级 import 会成环。
    from xskill.pipeline.registry import list_watch_dirs
    dirs = list_watch_dirs()
    if not dirs:
        raise RuntimeError(
            "没有已注册的 watch dir——先 `xskill registry add <abs-path>`，"
            "或让 daemon 启动时自动探测生态目录后再调用 get_traj_dir()。"
        )
    return Path(dirs[0]["path"])


def get_uploads_dir() -> Path:
    """上传 db 文件的落盘根目录（``~/.xskill/uploads``）。

    HTTP 上传端口把收到的 db 存到 ``uploads/<eco>/<client_id>/`` 下，再由
    ``xskill read`` 入库。按 client 分子目录隔离多用户同名 ``ngagent.db``。
    """
    d = XSKILL_HOME / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_registry_db_path(
    *, xskill_home: Optional[Path] = None,
) -> Path:
    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    return state_root / "registry.db"


def get_kernel_evaluation_db_path(
    *, xskill_home: Optional[Path] = None,
) -> Path:
    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    return state_root / "kernel_runs.db"


# ─── team (C/S 模式) 路径 ───────────────────────────────────────
# 纯路径运算，不读 config.yaml——client 瘦客户端无 llm.api_key，
# get_config() 会抛 KeyError。get_team_trajectories_dir() 是唯一例外
# （只 server 调，server 一定有 key）。

def get_team_server_state_path(*, xskill_home: Optional[Path] = None) -> Path:
    """server join token 落盘位置（~/.xskill/team_server.json，0600）。

    无 ``xskill_home`` 时保持历史行为：直接拼 ``XSKILL_HOME / team_server.json``，
    不 ``resolve``，避免和现有路径断言漂移。
    """
    if xskill_home is None:
        return XSKILL_HOME / "team_server.json"
    return Path(xskill_home).expanduser().resolve() / "team_server.json"


def _peek_state_config(
    xskill_home: Optional[Path] = None,
    *,
    strict: bool = False,
) -> dict:
    """只读 state_root/config.yaml，不经 ``get_config()``。

    瘦客户端没有 llm.api_key，``get_config()`` 会抛 KeyError。同机隔离只需要
    ``skill_dir`` 字段，所以这里只做 YAML 窥视，再交给 ``get_skill_dir``。
    """
    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    cfg_file = state_root / "config.yaml"
    if not cfg_file.is_file():
        return {}
    try:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except Exception as config_error:
        if strict:
            raise ValueError(
                "state config cannot be read safely"
            ) from config_error
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise ValueError("state config must be a mapping")
    return {}


def resolve_local_skill_dir(
    *,
    xskill_home: Optional[Path] = None,
    strict_config: bool = False,
) -> Path:
    """本机 skill 仓路径：走 ``get_skill_dir``，不要求完整 config。"""
    return get_skill_dir(
        _peek_state_config(xskill_home, strict=strict_config),
        xskill_home=xskill_home,
    )


def get_team_client_working_dir(*, xskill_home: Optional[Path] = None) -> Path:
    """同机隔离后的 client 工作副本根（``<xskill_home>/client_skill``）。"""
    root = Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    return root.expanduser() / "client_skill"


def _norm_path_key(p: Path | str) -> str:
    """内部路径规范化键（跨平台绝对路径 + Windows 大小写折叠 + UNC 剥离）。"""
    resolved = str(Path(p).expanduser().resolve(strict=False))
    if os.name == "nt":
        if resolved.startswith("\\\\?\\UNC\\"):
            resolved = "\\\\" + resolved[8:]
        elif resolved.startswith("\\\\?\\"):
            resolved = resolved[4:]
    return os.path.normcase(os.path.abspath(resolved))


def _canonical_server_skill_dir(xskill_home: Optional[Path] = None) -> Path:
    """Server 端的标准自有仓目录。委托 ``get_skill_dir``，不手写 ``skill/``。"""
    return resolve_local_skill_dir(xskill_home=xskill_home)


def is_team_server_canonical_skill_dir(
    skill_dir: Path | str,
    *,
    xskill_home: Optional[Path] = None,
) -> bool:
    """判断给定目录是否等于本机 team server 的标准自有仓。

    本机既 ``serve --server`` 又 ``connect`` 时，client 默认也会指向
    ``get_skill_dir()``。cleanup 会按派发清单删仓，把自有仓收成只剩
    分给这个 client 的那几十上百个。

    「这台机器是不是 team server」沿用仓库已有约定：
    ``get_team_server_state_path()`` 对应的 join token 落盘文件。
    """
    if not get_team_server_state_path(xskill_home=xskill_home).is_file():
        return False
    try:
        req_key = _norm_path_key(skill_dir)
        canon_key = _norm_path_key(resolve_local_skill_dir(
            xskill_home=xskill_home,
            strict_config=True,
        ))
        return req_key == canon_key
    except (OSError, RuntimeError, ValueError) as config_error:
        logger.error(
            "team server canonical skill dir is uncertain; failing closed "
            "error_type=%s",
            type(config_error).__name__,
        )
        return True


def resolve_team_client_skill_dir(
    skill_dir: Path | str,
    *,
    xskill_home: Optional[Path] = None,
) -> Path:
    """client 工作副本目录。与 server 自有仓撞车时改放到 ``client_skill/``。"""
    requested = Path(skill_dir)
    if not is_team_server_canonical_skill_dir(requested, xskill_home=xskill_home):
        return requested
    relocated = get_team_client_working_dir(xskill_home=xskill_home)
    logger.warning(
        "team client colocated with team server; using %s instead of %s",
        relocated, requested,
    )
    return relocated


def get_team_clients_db_path() -> Path:
    """server 端 client 注册表 SQLite。"""
    return XSKILL_HOME / "team_clients.db"


def get_team_server_whl_dir() -> Path:
    """server 端静默更新回退 wheel 目录（~/.xskill/whls）。"""
    d = XSKILL_HOME / "whls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_team_client_state_path() -> Path:
    """client 端连接信息（server_url / client_id / join_token）。"""
    return XSKILL_HOME / "team_client.json"


def get_connect_daemon_state_path() -> Path:
    """``xskill connect`` 常驻进程的运行态（pid / server_url / task 名）。

    与 ``team_client.json``（连接**身份**，跨重启不变）分开：本文件记的是
    “现在有没有一个后台 connect 进程在跑、它的 pid/宿主任务是谁”，供
    ``xskill start/stop/status`` 管理。进程退出/机器重启后 pid 可能失效，
    读取方须自行校验存活（见 team.client.service）。
    """
    return XSKILL_HOME / "connect_daemon.json"


def _server_scope_id(server_url: str) -> str:
    """把 server_url 映射成文件系统安全、且按 server 唯一的作用域 id。

    形如 ``7.220.144.233_9961-1a2b3c4d``：前半是可读的 host_port（排错时
    一眼能认出连的是哪台），后半是规范化 url 的短哈希消歧（不同 url 规范化
    后撞到同一可读前缀时仍能区分）。规范化会去掉首尾空白与末尾斜杠，所以
    ``http://h:p`` 与 ``http://h:p/`` 视为同一 server。
    """
    import hashlib
    import re
    norm = server_url.strip().rstrip("/")
    netloc = norm.split("://", 1)[-1]
    safe = re.sub(r"[^A-Za-z0-9.]+", "_", netloc).strip("_") or "server"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def get_team_client_dir(server_url: str) -> Path:
    """client 端按 server 隔离的可变状态目录：~/.xskill/clients/<server_id>/。

    上传游标 / 去抖 / 安装历史都落这里。换 server 时天然落到不同目录——不会
    再被上一个 server 的"已上传"游标静默压制对新 server 的上传（方案 A）。
    """
    d = XSKILL_HOME / "clients" / _server_scope_id(server_url)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_team_client_cursor_path(server_url: str) -> Path:
    """旧版 client 上传游标 JSON 路径，按 server 分目录。

    新版运行时状态落 ``client_state.db``；本路径保留为旧
    ``cursor.json`` / ``cursor.debounce.json`` 的一次性迁移来源。
    """
    return get_team_client_dir(server_url) / "cursor.json"


def get_team_client_state_db_path(server_url: str) -> Path:
    """client 端上传状态 SQLite，按 server 分目录。"""
    return get_team_client_dir(server_url) / "client_state.db"


def get_team_client_history_path(server_url: str) -> Path:
    """client 端安装历史（reconcile 落的 side 时间序列），按 server 分目录。

    注意这与 server/standalone 模式的 ``XSKILL_HOME/install_history.jsonl``
    是不同文件：那条是本机自身 canary 归因用的，与"连了哪个 server"无关。
    """
    return get_team_client_dir(server_url) / "install_history.jsonl"


# 注：team client 不另开 team_skills/ / team_outbox/ 目录——
#  - skill working copies 复用标准 skill_dir（~/.xskill/skill/），与
#    standalone 模式同位置；
#  - 采集的轨迹复用标准 bridge 目录（~/.xskill/<eco>_sessions/），即
#    detect_known_ecosystems 返回的 bridge 路径。


def get_team_trajectories_dir() -> Path:
    """server 端收下的 client 上传轨迹根目录。

    读 config.yaml ``team.server.traj_root``，缺省 ~/.xskill/team_trajectories。
    仅 server 调用。
    """
    cfg = get_config()
    raw = (cfg.get("team", {}).get("server", {}).get("traj_root")
           or str(XSKILL_HOME / "team_trajectories"))
    p = Path(raw).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─── 调试 flag ──────────────────────────────────────────────────
def is_debug() -> bool:
    return _overrides.get("debug", False)


def is_quiet() -> bool:
    return _overrides.get("quiet", False)


# ─── 兼容旧 API（仅为过渡期保留，下期清掉）───────────────────────
def get_registry_dir() -> Path:
    """旧 API。返回 XSKILL_HOME。新代码请用 get_registry_db_path。"""
    return XSKILL_HOME




def get_output_dir() -> Path:
    """旧 API → 转 logs_dir"""
    return get_logs_dir()


def resolve_traj_path(path_or_dataset: str) -> Path:
    """旧 API。新代码：直接用绝对路径。"""
    p = Path(path_or_dataset).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"trajectory path not found: {p}")
    return p
