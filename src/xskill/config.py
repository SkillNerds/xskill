"""
config.py — 全局路径与配置加载
═════════════════════════════════════
统一从 ~/.xskill/ 读取；无 cwd fallback、无环境变量 fallback、无 ~/.aikey fallback。
缺失即抛异常。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("xskill.config")

# ─── 默认根路径 ─────────────────────────────────────────────────
XSKILL_HOME = Path.home() / ".xskill"
CONFIG_PATH = XSKILL_HOME / "config.yaml"
REGISTRY_DB = XSKILL_HOME / "registry.db"
CHAT_DB = XSKILL_HOME / "chat_sessions.db"
LOGS_DIR = XSKILL_HOME / "logs"

_config: dict = {}
_overrides: dict = {}


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
  # temperature: 0.0     # optional; default 0 (deterministic)
  # request_timeout: 60  # optional; per-request wall-clock cap in seconds
                         # (default 60). Explicit so an unreachable endpoint
                         # fails loud instead of hanging forever.
  # connect_timeout: 10  # optional; TCP-connect cap in seconds (default 10).
  # client_max_retries: 0 # optional; openai-SDK client retries (default 0 —
                         # transient-error retries are handled by xskill's own
                         # retry wrapper; client retries would multiply).
  # rate_limit:          # optional; absent = unlimited (good for self-hosted)
  #   rpm: 60            # requests per minute; match your provider plan
  #   tpm: 100000        # tokens per minute (optional within rate_limit)
  #   burst: 10          # optional; default = ceil(rate/6)
  # See docs/adr/0001-rate-limit-diy-not-litellm.md for the design rationale.

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
  # api: openai | multimodal   # optional; default openai. "multimodal" for
                               # vision-style embedding endpoints.

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
  val_weight:    0.5            # promotion compares a composite score:
                                # (1-val_weight)*ux_avg + val_weight*(val_acc*10).
                                # val_acc is the side's solve-correctness on the
                                # val set, read from <skill_dir>/.val_scores.json
                                # by commit sha (written externally during canary
                                # settle). 0.5 = ux and val each weigh half; a
                                # side missing its val entry falls back to pure ux.

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

# ===== Watcher (the directory poller inside `serve`) =====
watcher:
  poll_interval:  30            # seconds between scans of every watch_dir
  max_concurrent: 4             # parallel LLM calls per scan. Conservative
                                # placeholder that pairs with llm.rate_limit
                                # above. Raise to 20-30 for self-hosted vLLM
                                # or accounts with no concurrency cap. See
                                # docs/adr/0001-rate-limit-diy-not-litellm.md
  # cluster_batch_size: 8       # atoms consumed per ClusterAgent call. The
                                # watcher pools un-clustered atoms ACROSS all
                                # indexed trajectories, drops those already in a
                                # skill's .candidates.yml, then feeds up to N at
                                # a time to ONE ClusterAgent (one LLM round-trip
                                # handles N atom positions instead of one). The
                                # agent still reads each atom's content on demand
                                # via tools — only the positions are batched.
                                # Clustering stays serial (one batch in flight per
                                # watch dir). Default 8; set 1 for the old
                                # one-atom-per-call behavior.

# ===== Cold-start epoch barrier (traj-jam fix) =====
# 默认关闭 → 走正常在线增量 SkillEdit。开启后：冷启动阶段 hold 住所有增量
# SkillEdit，让整个 epoch 的全部子轨迹 atom 攒进各 skill 的 candidates；算法在
# epoch 训练结束后 touch barrier_path 这个 sentinel，watcher 检出即用极低
# flush_threshold 把每个有候选的 baby 技能一次性批量毕业到 main（引用该 epoch
# 累积的全部 atom）。跑满 epochs 个 epoch 后自动转入在线增量 + 灰度。
# 适用于小数据冷启动——atom 稀少时正常阈值永远到不了、没有技能能毕业的堰塞。
# cold_start:
#   enabled: false            # 启用冷启动屏障（默认 false = 零行为变化）
#   flush_threshold: 1        # 屏障 flush 的 weightscore 门槛（≥1；1=任何有候选的都毕业）
#   epochs: 1                 # hold+flush 的冷启动 epoch 数（≥1），之后转在线
#   barrier_path: ""          # sentinel 绝对路径；空=<home>/EPOCH_FLUSH

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
        _config = yaml.safe_load(f) or {}
    if not _config.get("llm", {}).get("api_key"):
        raise KeyError(f"llm.api_key missing in {cfg_path}")
    if not _config.get("embedding", {}).get("api_key"):
        raise KeyError(f"embedding.api_key missing in {cfg_path}")
    return _config


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
    return {
        "enabled": bool(d.get("enabled", False)),
        "public": bool(d.get("public", False)),
        "password": str(d.get("password", "") or ""),
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


def get_skill_dir() -> Path:
    """skill_dir: config.yaml 字段；默认 ~/.xskill/skill/"""
    cfg = get_config()
    raw = cfg.get("skill_dir") or str(XSKILL_HOME / "skill")
    return Path(raw).expanduser()


def get_logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


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


def get_registry_db_path() -> Path:
    return REGISTRY_DB


def get_chat_db_path() -> Path:
    return CHAT_DB


# ─── team (C/S 模式) 路径 ───────────────────────────────────────
# 纯路径运算，不读 config.yaml——client 瘦客户端无 llm.api_key，
# get_config() 会抛 KeyError。get_team_trajectories_dir() 是唯一例外
# （只 server 调，server 一定有 key）。

def get_team_server_state_path() -> Path:
    """server join token 落盘位置（~/.xskill/team_server.json，0600）。"""
    return XSKILL_HOME / "team_server.json"


def get_team_clients_db_path() -> Path:
    """server 端 client 注册表 SQLite。"""
    return XSKILL_HOME / "team_clients.db"


def get_team_client_state_path() -> Path:
    """client 端连接信息（server_url / client_id / join_token）。"""
    return XSKILL_HOME / "team_client.json"


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
    """client 端上传游标（traj_id -> 已上传内容 sha256），按 server 分目录。

    去抖 sidecar 由 collector 从本路径派生（``cursor.debounce.json``），自动
    同目录隔离。
    """
    return get_team_client_dir(server_url) / "cursor.json"


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
