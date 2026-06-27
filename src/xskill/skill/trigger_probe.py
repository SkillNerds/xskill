"""
skill/trigger_probe.py — 真跑代理的闭环触发探针（closed-loop trigger probe）
═══════════════════════════════════════════════════════════════════
取代 description_opt 里的 LLM-as-judge。判定"一个描述会不会触发"的方式从
"问 LLM 你会调哪个"（意见/元问题）换成 **真跑一个 agno 迷你代理**：把候选
skill + 一批语义相关的真实 skill 各注册成一个工具（工具说明=各自描述），喂
用户 query，代理一旦 call 了"本 skill 的工具"——记一笔、当场终止本轮
（``StopAgentRun``）。这是真实工具调用循环里的一次真实决策。

设计见 docs/plans/2026-06-11-trigger-probe-real-agent-and-dashboard.md。

保真度天花板：探针代理是 agno + 用户自配模型（DeepSeek/GLM），**不是 Claude
Code 本体**。它给的不是绝对真值，而是"A、B 两版描述哪个在真实竞争环境里更易被
真实代理选中"的相对信号——比元问题可信。

无副作用 → 无需沙箱（D5）：触发即终止，代理全程不执行任何真实动作；工具空间
只有 skill 触发工具（调用=拦截+终止）+ 几个只读空操作桩。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np

from xskill.canary import main_sha
from xskill.skill import frontmatter as fm
from xskill.skill.repo import SkillRepo

logger = logging.getLogger("xskill.skill_edit_agent")

# 探针单轮工具调用上限：给代理一两步决定够了，超了即视作"没触发本 skill"。
_PROBE_TOOL_CALL_LIMIT = 4

_PROBE_SYSTEM_PROMPT = (
    "You are a coding agent. Below (as your available tools) is the "
    "available_skills list: each `use_*` tool represents a skill, and its "
    "description tells you when that skill applies. Read the user's request "
    "and decide which single skill (if any) best fits. If one fits, invoke "
    "its `use_*` tool. If none fit, do NOT invoke any `use_*` tool — just "
    "say so briefly. Base the decision solely on the skill descriptions and "
    "the user's intent."
)


def _slug_to_tool(name: str) -> str:
    """skill name → 合法 python 标识符工具名 ``use_<slug>``。"""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower() or "skill"
    if slug[0].isdigit():
        slug = "s_" + slug
    return "use_" + slug


def _truncate(text: str, cap: int) -> str:
    text = (text or "").strip()
    if cap > 0 and len(text) > cap:
        return text[:cap]
    return text


# ═══════════════════════════════════════════════════════════════════
# 真实诱饵清单（D2/D3/D4/D6）：query 锚点 → main 分支 → cosine top-N → 截断
# ═══════════════════════════════════════════════════════════════════

def _main_descriptions(skill_root: Path, skill_name: str) -> dict[str, str]:
    main_desc: dict[str, str] = {}
    for sk in SkillRepo(skill_root):
        if sk.name == skill_name:
            continue
        if main_sha(sk.path):
            main_desc[sk.name] = sk.description
    return main_desc


def _ensure_probe_index(
    index_path: Path,
    skill_root: Path,
    main_desc: dict[str, str],
    embed_client: Any,
) -> bool:
    if index_path.is_file():
        return True
    if not main_desc:
        logger.warning(
            "build_probe_catalog: .skill_index.pkl 缺失且全库无 main 竞争"
            " skill (%s) → 降级无竞争模式 catalog_size=0：触发率只剩"
            "“有/没有触发”，无竞争区分度", skill_root,
        )
        return False
    logger.warning(
        "build_probe_catalog: .skill_index.pkl 缺失 (%s)，从 %d 个 main"
        " skill 现场重建索引", skill_root, len(main_desc),
    )
    try:
        from xskill.agents.skill_tools import rebuild_skill_index
        rebuild_skill_index(skill_dir=skill_root, embed_client=embed_client)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "build_probe_catalog: 索引重建失败（%s）→ 降级无竞争模式"
            " catalog_size=0", exc,
        )
        return False
    if index_path.is_file():
        return True
    logger.warning(
        "build_probe_catalog: 重建后索引仍缺失 (%s) → 降级无竞争模式"
        " catalog_size=0", skill_root,
    )
    return False


def _load_probe_index(index_path: Path) -> tuple[list[str], Any] | None:
    import pickle
    with open(index_path, "rb") as f:
        index = pickle.load(f)
    names: list[str] = list(index.get("skill_names") or [])
    embeddings = index.get("embeddings")
    if not names or embeddings is None or len(names) != len(embeddings):
        logger.warning("build_probe_catalog: skill_index 结构异常，诱饵清单为空"
                       "（无竞争模式 catalog_size=0）")
        return None
    return names, embeddings


def _rank_probe_index(query: str, embeddings: Any, embed_client: Any) -> np.ndarray:
    q = np.asarray(embed_client.encode(query), dtype=np.float32)
    norm = float(np.linalg.norm(q))
    if norm == 0.0:
        return np.asarray([], dtype=np.int64)
    q = q / norm
    sims = np.asarray(embeddings, dtype=np.float32) @ q
    return np.argsort(-sims)


def _catalog_from_order(
    order: np.ndarray,
    names: list[str],
    main_desc: dict[str, str],
    skill_name: str,
    *,
    max_skills: int,
    desc_cap: int,
) -> list[dict]:
    catalog: list[dict] = []
    for row in order:
        nm = names[int(row)]
        if nm == skill_name or nm not in main_desc:
            continue
        desc = _truncate(main_desc[nm], desc_cap)
        if not desc:
            continue
        catalog.append({"name": nm, "description": desc})
        if len(catalog) >= max_skills:
            break
    return catalog


def build_probe_catalog(
    query: str,
    skill_name: str,
    *,
    skill_root: Path,
    embed_client: Any,
    max_skills: int,
    desc_cap: int,
) -> list[dict]:
    """组装探针的诱饵清单：与 query 语义最近的若干 **main 分支** skill。

    - 只取已 graduate 到 main 的 skill（``main_sha`` 非空）——排除 baby（无正文
      stub）与 staging/canary 旁路候选。
    - 用 ``.skill_index.pkl``（L2 归一 embedding 矩阵）对 query 向量算 cosine，
      降序取前 ``max_skills``。
    - 每条 description 按 ``desc_cap`` 截断（镜像 Claude Code 的单条上限——代理
      看到的就是真实会看到的截断版）。
    - 排除本 skill 自身（它由 probe_trigger 注入候选描述）。

    索引缺失（``rebuild --force`` 清掉 index.pkl 后是**必现路径**）时不再静默
    返回空清单，而是：

    1. 先数 main 分支竞争者——全库只有本 skill 时重建也无意义，直接降级为
       **无竞争模式**（显式 WARNING + 调用方按 catalog_size=0 标记结果，
       不许悄悄当正常分），且不触 embedding。
    2. 有竞争者 → 用 ``rebuild_skill_index`` 从 main 技能现场重建索引再走
       正常检索；重建失败同样显式 WARNING 后降级。

    返回 ``[{"name", "description"}, ...]``；空清单 = 无竞争模式。
    """
    skill_root = Path(skill_root)

    # main 分支过滤 + name→description（竞争者画像，索引缺失判定也用它）
    main_desc = _main_descriptions(skill_root, skill_name)

    index_path = skill_root / ".skill_index.pkl"
    if not _ensure_probe_index(index_path, skill_root, main_desc, embed_client):
        return []

    # query 向量 L2 归一 → cosine = embeddings @ q
    loaded = _load_probe_index(index_path)
    if loaded is None:
        return []
    names, embeddings = loaded
    order = _rank_probe_index(query, embeddings, embed_client)
    return _catalog_from_order(
        order, names, main_desc, skill_name,
        max_skills=max_skills, desc_cap=desc_cap,
    )


# ═══════════════════════════════════════════════════════════════════
# 探针（D1/D5）：skill-as-tool + StopAgentRun
# ═══════════════════════════════════════════════════════════════════

def _make_skill_tool(
    tool_name: str, skill_real_name: str, description: str, record: dict,
) -> Callable:
    """造一个代表某 skill 的工具：调用即记 triggered + 抛 StopAgentRun 终止本轮。"""
    from agno.exceptions import StopAgentRun

    def _tool(reason: str = "") -> str:  # noqa: ARG001 (reason 给模型填，不用)
        record["triggered"] = skill_real_name
        raise StopAgentRun("skill triggered")

    _tool.__name__ = tool_name
    _tool.__doc__ = description or f"Use the {skill_real_name} skill."
    return _tool


def _stub_read_file(path: str = "") -> str:  # noqa: ARG001
    """Read a file's contents (read-only)."""
    return ""


def _stub_list_files(path: str = ".") -> str:  # noqa: ARG001
    """List files under a directory (read-only)."""
    return ""


def _skill_tools(
    skill_name: str,
    candidate_desc: str,
    catalog: list[dict],
    *,
    desc_cap: int,
    record: dict,
) -> list[Callable]:
    tools: list[Callable] = []
    self_tool = _slug_to_tool(skill_name)
    tools.append(_make_skill_tool(
        self_tool, skill_name, _truncate(candidate_desc, desc_cap), record,
    ))
    used_tool_names = {self_tool}
    for entry in catalog:
        nm = entry["name"]
        tname = _unique_tool_name(_slug_to_tool(nm), used_tool_names)
        tools.append(_make_skill_tool(tname, nm, entry["description"], record))
    return tools


def _unique_tool_name(base: str, used_tool_names: set[str]) -> str:
    tname = base
    i = 2
    while tname in used_tool_names:
        tname = f"{base}_{i}"
        i += 1
    used_tool_names.add(tname)
    return tname


def _run_probe_agent(agent: Any, query: str) -> None:
    try:
        agent.run(query)
    except Exception as exc:  # noqa: BLE001
        # StopAgentRun 已被 agno 内部吞掉；这里兜真实异常（网络等）——记日志，
        # 当作"未触发"，绝不让一条 case 崩掉整个优化。
        logger.warning("probe_trigger 代理异常（视作未触发）: %s", exc)


def _run_probe_with_timeout(
    agent: Any, query: str, skill_name: str, case_timeout: float | None,
) -> None:
    if not case_timeout or case_timeout <= 0:
        _run_probe_agent(agent, query)
        return

    import threading
    worker = threading.Thread(target=lambda: _run_probe_agent(agent, query),
                              daemon=True, name=f"probe-{skill_name}")
    worker.start()
    worker.join(case_timeout)
    if worker.is_alive():
        # 守护线程留它自生自灭（底层超时最终会让它退出）；本 case 按
        # "未触发"计——宁可保守扣分，不能挂死整个优化循环。
        logger.warning(
            "probe_trigger 超过单 case 墙钟上限 %.0fs（视作未触发）: "
            "skill=%s query=%.60s", case_timeout, skill_name, query,
        )


def probe_trigger(
    query: str,
    skill_name: str,
    candidate_desc: str,
    catalog: list[dict],
    *,
    agno_agent_factory: Callable[..., Any],
    desc_cap: int,
    case_timeout: float | None = 60.0,
) -> str:
    """真跑一轮代理，返回它触发的 skill name（或 "NONE"）。

    参数
    ----
    query: 评测查询。
    skill_name / candidate_desc: 被测 skill 及其候选描述（注入诱饵清单首位）。
    catalog: build_probe_catalog 产的真实诱饵清单 ``[{"name","description"}]``。
    agno_agent_factory: ``(*, instructions, tools, **kwargs) -> agno Agent``。
    desc_cap: 候选描述喂给代理前的截断上限（与诱饵同一上限）。
    case_timeout: 单 case 墙钟上限（秒），配置项 ``skill_opt.probe_case_timeout``
        （默认 60）。模型层超时（``llm.request_timeout``）是第一道防线，这里是
        总兜底：把 ``agent.run`` 放守护线程里跑，超时即视作"未触发"并告警——
        任何底层组件（网络/SDK/agno 内部）的意外阻塞都不能挂死优化循环。
        传 None/0 关闭兜底（仅限测试用）。
    """
    record: dict = {}

    # 候选 skill 在首位（描述同样按 cap 截断，保证与诱饵同一可见条件）
    tools = _skill_tools(
        skill_name, candidate_desc, catalog, desc_cap=desc_cap, record=record,
    )

    # 只读空操作桩：给代理合理动作空间，零副作用
    tools.append(_stub_read_file)
    tools.append(_stub_list_files)

    agent = agno_agent_factory(
        instructions=[_PROBE_SYSTEM_PROMPT],
        tools=tools,
        tool_call_limit=_PROBE_TOOL_CALL_LIMIT,
    )
    _run_probe_with_timeout(agent, query, skill_name, case_timeout)

    return record.get("triggered") or "NONE"


# ═══════════════════════════════════════════════════════════════════
# 看板"重跑单 case"（Phase 2 action 端点用，按需现建 factory/embed）
# ═══════════════════════════════════════════════════════════════════

def rerun_probe_case(
    skill_root: Path, skill_name: str, query: str, *, config: dict,
) -> dict:
    """对单条 query 用 skill 当前描述重跑探针（runs_per_case 轮），返回结果。

    给看板"重新触发"按钮用：现读 SKILL.md 当前 description、现建诱饵清单 +
    agno 工厂 + embed_client，跑完即弃。不改归档历史（归档是某次优化的记录），
    结果只回前端内联展示。
    """
    from xskill.agents.agno_factory import make_default_factory
    from xskill.utils.llm import create_embed_client

    opt = dict(config.get("skill_opt", {}) or {})
    runs = int(opt.get("runs_per_case", 3))
    max_skills = int(opt.get("catalog_max_skills", 12))
    desc_cap = int(opt.get("catalog_desc_cap", 256))
    case_timeout = float(opt.get("probe_case_timeout", 60.0) or 0.0)

    skill_md = Path(skill_root) / skill_name / "SKILL.md"
    fm_dict, _ = fm.parse(skill_md.read_text(encoding="utf-8"))
    desc = str(fm_dict.get("description") or "").strip()
    if not desc:
        raise ValueError(f"{skill_name} 无 description，无法重跑探针")

    embed = create_embed_client(config)
    catalog = build_probe_catalog(
        query, skill_name, skill_root=Path(skill_root), embed_client=embed,
        max_skills=max_skills, desc_cap=desc_cap,
    )
    factory = make_default_factory(config)
    n_hit = 0
    runs_rec: list[dict] = []
    for _ in range(runs):
        chosen = probe_trigger(
            query, skill_name, desc, catalog,
            agno_agent_factory=factory, desc_cap=desc_cap,
            case_timeout=case_timeout,
        )
        hit = chosen == skill_name
        if hit:
            n_hit += 1
        runs_rec.append({"triggered_skill": chosen, "hit": hit})
    return {
        "query": query,
        "did_trigger": (n_hit / runs) >= 0.5 if runs else False,
        "catalog": [e["name"] for e in catalog],
        "runs": runs_rec,
    }
