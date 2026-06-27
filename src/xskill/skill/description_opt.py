"""
skill/description_opt.py — description 触发优化（确定性 loop）
═══════════════════════════════════════════════════════════════════
把 Anthropic skill-creator 的"description 是唯一触发机制 + 触发准确率优化
+ held-out test 集选优防过拟合"那套，落进 xskill 的 commit workflow。

入口 :func:`optimize_description`，在 ``commit_baby_to_main`` /
``commit_to_staging`` 内部、git commit 之前调用（D1：硬编码进 workflow，
不做 agent tool——优化 loop 是确定性代码，agent 看返回值无意义）。

机制（同 skill-creator ``run_loop.py``）：
  1. case 生成：LLM 产 ~20 条 {query, should_trigger, topic}，缓存到
     ``.description_optimization/cases.json`` 复用。
  2. train/test split：按 should_trigger 分层，60/40，固定 seed。
  3. 触发判定 = LLM-as-judge（D2）：候选 desc + 其它 skill 拼伪
     available_skills catalog，问 LLM"会调哪个 skill"，跑 N 次算触发率。
  4. 进化（improve loop，≤max_iters）：把 train 失败拼进 improve prompt，
     产新候选 desc；<1024 字符硬闸 + 超长重写兜底。
  5. 筛选（D3/D4）：所有候选**按 TEST 集分**选 best（不看 train 分，防
     过拟合）。平手时偏好原始 desc（稳定性）。
  6. best 写回 SKILL.md frontmatter，全程 archive 到
     ``.description_optimization/{exp_id}_{ts}/``（D8）。

成本闸（D7）：max_iters / max_llm_calls 硬上限；走传入的 ``llm``（已含
rate_limit + retry），绝不另起进程/线程。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xskill.skill import frontmatter as fm
from xskill.skill.trigger_probe import build_probe_catalog, probe_trigger

logger = logging.getLogger("xskill.skill_edit_agent")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agents" / "prompts"

# description 硬限制（Claude 截断阈值）
DESC_HARD_LIMIT = 1024


# ═══════════════════════════════════════════════════════════════════
# prompt 装载
# ═══════════════════════════════════════════════════════════════════

def _load_prompt(name: str) -> str:
    p = _PROMPTS_DIR / name
    return p.read_text(encoding="utf-8")


def _skill_opt_settings(config: dict) -> dict:
    opt_cfg = dict(config.get("skill_opt", {}) or {})
    return {
        "enabled": opt_cfg.get("enabled", True),
        "n_cases": int(opt_cfg.get("n_cases", 20)),
        "runs_per_case": int(opt_cfg.get("runs_per_case", 3)),
        "max_iters": int(opt_cfg.get("max_iters", 5)),
        "max_llm_calls": int(opt_cfg.get("max_llm_calls", 400)),
        "train_frac": float(opt_cfg.get("train_frac", 0.6)),
        "seed": int(opt_cfg.get("seed", 42)),
        "catalog_max_skills": int(opt_cfg.get("catalog_max_skills", 12)),
        "catalog_desc_cap": int(opt_cfg.get("catalog_desc_cap", 256)),
        "probe_case_timeout": float(opt_cfg.get("probe_case_timeout", 60.0) or 0.0),
    }


def _read_skill_context(skill_dir: Path) -> tuple[Path, dict, str, str, str, str]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"SKILL.md not found: {skill_md}")

    skill_content = skill_md.read_text(encoding="utf-8")
    fm_dict, body = fm.parse(skill_content)
    skill_name = str(fm_dict.get("name") or skill_dir.name).strip()
    current_description = str(fm_dict.get("description") or "").strip()
    if not current_description:
        raise ValueError(f"SKILL.md 没有 description，无法优化: {skill_md}")
    return skill_md, fm_dict, body, skill_name, current_description, skill_content


def _build_catalogs(
    cases: list[dict],
    skill_name: str,
    *,
    skill_root: Path,
    embed_client: Any,
    max_skills: int,
    desc_cap: int,
) -> dict:
    return {
        c["query"]: build_probe_catalog(
            c["query"], skill_name, skill_root=skill_root,
            embed_client=embed_client, max_skills=max_skills,
            desc_cap=desc_cap,
        )
        for c in cases
    }


def _catalog_mode(cases: list[dict], catalog_by_query: dict, skill_name: str) -> tuple[int, bool]:
    avg_catalog = 0
    if cases:
        avg_catalog = round(
            sum(len(catalog_by_query.get(c["query"], [])) for c in cases)
            / len(cases)
        )
    no_competition = avg_catalog == 0
    if no_competition:
        logger.warning(
            "description_opt[%s]: 诱饵清单全空（catalog_size=0，无竞争模式）"
            "——触发率只反映“有/没有触发”，与有竞争场景的分数不可比",
            skill_name,
        )
    return avg_catalog, no_competition


def _create_exp_dir(opt_root: Path) -> tuple[str, Path]:
    exp_id = _next_exp_id(opt_root)
    ts = time.strftime("%Y%m%d-%H%M%S")
    exp_dir = opt_root / f"{exp_id}_{ts}"
    exp_dir.mkdir(parents=True, exist_ok=True)
    return exp_id, exp_dir


def _record_candidate(
    candidates: list[dict],
    attempts_path: Path,
    *,
    iteration: int,
    desc: str,
    train: list[dict],
    budget: Any,
    skill_name: str,
    catalog_by_query: dict,
    runs_per_case: int,
    exp_dir: Path,
    agno_agent_factory: Any,
    desc_cap: int,
    case_timeout: float,
) -> dict:
    """评 train，落 attempts.jsonl + per-case json，返回候选条目（test 分后填）。"""
    train_score, train_results = _score_description(
        desc, train, budget, skill_name, catalog_by_query,
        runs_per_case, exp_dir, agno_agent_factory=agno_agent_factory,
        desc_cap=desc_cap, tag=f"iter{iteration}_train",
        case_timeout=case_timeout,
    )
    entry = {
        "iter": iteration,
        "description": desc,
        "train_score": train_score,
        "test_score": None,
        "_train_results": train_results,
    }
    candidates.append(entry)
    _append_jsonl(attempts_path, {
        "iter": iteration, "description": desc,
        "train_score": train_score, "test_score": None,
    })
    logger.info(
        "description_opt[%s] iter %d: train_score=%.3f desc=%r",
        skill_name, iteration, train_score, desc[:80],
    )
    return entry


@dataclass(frozen=True)
class _EvolutionContext:
    max_iters: int
    max_llm_calls: int
    train: list[dict]
    budget: Any
    llm: Any
    skill_name: str
    skill_content: str
    catalog_by_query: dict
    runs_per_case: int
    exp_dir: Path
    attempts_path: Path
    agno_agent_factory: Any
    desc_cap: int
    case_timeout: float


def _evolve_candidates(
    current_description: str,
    *,
    ctx: _EvolutionContext,
) -> list[dict]:
    candidates: list[dict] = []
    try:
        # 原始 desc 作为 iter 0 候选
        _record_candidate(
            candidates, ctx.attempts_path, iteration=0, desc=current_description,
            train=ctx.train, budget=ctx.budget, skill_name=ctx.skill_name,
            catalog_by_query=ctx.catalog_by_query, runs_per_case=ctx.runs_per_case,
            exp_dir=ctx.exp_dir, agno_agent_factory=ctx.agno_agent_factory,
            desc_cap=ctx.desc_cap, case_timeout=ctx.case_timeout,
        )

        improve_tmpl = _load_prompt("improve_description.txt")
        for it in range(1, ctx.max_iters + 1):
            prev = candidates[-1]
            prompt = _improve_prompt(
                improve_tmpl, prev, ctx.train, candidates,
                ctx.skill_name, ctx.skill_content,
            )
            raw = ctx.budget.chat(ctx.llm, prompt)
            new_desc = _parse_new_description(raw)
            if not new_desc:
                logger.warning(
                    "description_opt[%s] iter %d: LLM 未返回 <new_description>，停止进化",
                    ctx.skill_name, it,
                )
                break
            _record_candidate(
                candidates, ctx.attempts_path, iteration=it,
                desc=_enforce_limit(new_desc, ctx.llm, ctx.budget),
                train=ctx.train, budget=ctx.budget, skill_name=ctx.skill_name,
                catalog_by_query=ctx.catalog_by_query,
                runs_per_case=ctx.runs_per_case,
                exp_dir=ctx.exp_dir, agno_agent_factory=ctx.agno_agent_factory,
                desc_cap=ctx.desc_cap, case_timeout=ctx.case_timeout,
            )
    except _LLMBudgetExhausted:
        logger.warning(
            "description_opt[%s]: 命中 max_llm_calls=%d，提前停止进化，"
            "用已有候选选优", ctx.skill_name, ctx.max_llm_calls,
        )
    return candidates


def _improve_prompt(
    improve_tmpl: str, prev: dict, train: list[dict], candidates: list[dict],
    skill_name: str, skill_content: str,
) -> str:
    scores_summary = (
        f"train_score={prev['train_score']:.3f}, "
        f"{len(train)} train cases"
    )
    scores_detail = _format_scores_detail(prev["_train_results"], candidates)
    return improve_tmpl.format(
        skill_name=skill_name,
        current_description=prev["description"],
        scores_summary=scores_summary,
        scores_detail=scores_detail,
        skill_content=skill_content,
    )


def _write_best_description(
    skill_md: Path, fm_dict: dict, body: str, skill_name: str,
    best: dict, current_description: str,
) -> str:
    best_desc = best["description"]
    if best_desc != current_description:
        fm_dict["description"] = best_desc
        skill_md.write_text(fm.serialize(fm_dict, body), encoding="utf-8")
        logger.info(
            "description_opt[%s]: 写回 best desc (test_score=%.3f): %r",
            skill_name, best["test_score"], best_desc[:80],
        )
    else:
        logger.info(
            "description_opt[%s]: 原始 desc 已是 test 最优 (test_score=%.3f)，不改",
            skill_name, best["test_score"],
        )
    return best_desc


def _record_trigger_eval_best(
    skill_dir: Path, skill_name: str, exp_id: str, best: dict,
    cases: list[dict], avg_catalog: int,
) -> None:
    try:
        from xskill.canary import main_sha as _main_sha
        from xskill.pipeline.registry import record_trigger_eval
        record_trigger_eval(
            skill=skill_name, version_sha=_main_sha(skill_dir),
            exp_id=exp_id, train_score=float(best["train_score"]),
            test_score=float(best["test_score"] or 0.0),
            n_cases=len(cases), catalog_size=avg_catalog,
        )
    except Exception:  # noqa: BLE001
        logger.exception("record_trigger_eval 失败（不阻断）: %s", skill_name)


def _optimization_result(
    best_desc: str, summary: dict, avg_catalog: int, no_competition: bool,
    candidates: list[dict], exp_dir: Path, budget: Any,
) -> dict:
    return {
        "enabled": True,
        "best_description": best_desc,
        "chosen_reason": summary["chosen_reason"],
        "catalog_size": avg_catalog,
        "no_competition": no_competition,
        "candidates": [
            {"iter": c["iter"], "description": c["description"],
             "train_score": c["train_score"], "test_score": c["test_score"]}
            for c in candidates
        ],
        "exp_dir": str(exp_dir),
        "n_llm_calls": budget.used,
    }


# ═══════════════════════════════════════════════════════════════════
# 公共入口
# ═══════════════════════════════════════════════════════════════════

def optimize_description(
    skill_dir: Path,
    *,
    llm: Any,
    config: dict,
    agno_agent_factory: Any,
    embed_client: Any,
    skill_root: Path,
) -> dict:
    """优化 skill_dir/SKILL.md 的 frontmatter.description 触发准确率。

    触发判定走真跑代理的闭环探针（trigger_probe），诱饵清单 = 与 query 语义
    最近的 main 分支 skill（见 docs/plans/2026-06-11-trigger-probe-...）。

    参数
    ----
    skill_dir:
        skill 子目录（含 SKILL.md）。
    llm:
        ``xskill.utils.llm.LLMClient``（已含 rate_limit + retry）。case 生成 /
        improve / shorten 走 ``llm.chat(prompt)``。
    config:
        全局 config dict；本函数只读 ``config["skill_opt"]``。
    agno_agent_factory:
        ``(*, instructions, tools, **kwargs) -> agno Agent``，探针真跑代理用。
    embed_client:
        embedding 客户端，给诱饵清单 query 锚点检索用。
    skill_root:
        skill 仓根（含各 skill 子目录 + ``.skill_index.pkl``），诱饵清单来源。

    返回
    ----
    dict 摘要 ``{"enabled", "best_description", "chosen_reason",
    "candidates", "exp_dir", ...}``；``enabled=False`` 时只返回
    ``{"enabled": False}``（no-op）。
    """
    skill_dir = Path(skill_dir)
    settings = _skill_opt_settings(config)

    if not settings["enabled"]:
        logger.info("skill_opt disabled — skip description optimization (%s)",
                    skill_dir.name)
        return {"enabled": False}

    skill_md, fm_dict, body, skill_name, current_description, skill_content = (
        _read_skill_context(skill_dir)
    )

    # LLM 调用计数器（闭包共享，命中 max_llm_calls 抛 _LLMBudgetExhausted）。
    # case 生成/improve/shorten 走 budget.chat；探针每跑一轮走 budget.consume。
    budget = _Budget(max_calls=settings["max_llm_calls"])

    opt_root = skill_dir / ".description_optimization"
    opt_root.mkdir(parents=True, exist_ok=True)

    # ── 1. case 生成（缓存复用）─────────────────────────────────
    cases = _load_or_generate_cases(
        opt_root, llm, budget, skill_name, current_description,
        skill_content, settings["n_cases"],
    )

    # ── 2. train/test split（分层 + 确定性）────────────────────
    train, test = stratified_split(
        cases, train_frac=settings["train_frac"], seed=settings["seed"])
    logger.info(
        "description_opt[%s]: %d cases → %d train / %d test",
        skill_name, len(cases), len(train), len(test),
    )

    # 诱饵清单按 query 预算一次（同 query 跨候选/跨轮复用，省 embedding）。
    # 每个 query 的竞争对手可不同——这正是"不同技能列表触发率不同"的来源。
    catalog_by_query = _build_catalogs(
        cases, skill_name, skill_root=skill_root, embed_client=embed_client,
        max_skills=settings["catalog_max_skills"],
        desc_cap=settings["catalog_desc_cap"],
    )

    # 无竞争模式显式标记：诱饵清单全空（索引缺失重建失败 / 全库只有本 skill）
    # 时分数没有竞争区分度——绝不许悄悄当正常分。catalog_size 随结果/registry/
    # summary 一路带出去，看板与复盘据此降权。
    avg_catalog, no_competition = _catalog_mode(cases, catalog_by_query, skill_name)

    # 实验目录
    exp_id, exp_dir = _create_exp_dir(opt_root)
    attempts_path = exp_dir / "attempts.jsonl"

    # ── 3+4. 进化（improve loop）─────────────────────────────────
    # 候选 = 原始 desc + 每轮 improve 产出。每个候选先在 train 上评（拿失败
    # 喂下一轮），最后统一在 test 上评选优。
    evolution_ctx = _EvolutionContext(
        max_iters=settings["max_iters"],
        max_llm_calls=settings["max_llm_calls"],
        train=train,
        budget=budget,
        llm=llm,
        skill_name=skill_name,
        skill_content=skill_content,
        catalog_by_query=catalog_by_query,
        runs_per_case=settings["runs_per_case"],
        exp_dir=exp_dir,
        attempts_path=attempts_path,
        agno_agent_factory=agno_agent_factory,
        desc_cap=settings["catalog_desc_cap"],
        case_timeout=settings["probe_case_timeout"],
    )
    candidates = _evolve_candidates(
        current_description, ctx=evolution_ctx,
    )

    # ── 5. 筛选（test 选优；D3/D4）──────────────────────────────
    best = _select_best_on_test(
        candidates, test, budget, skill_name, catalog_by_query,
        settings["runs_per_case"], exp_dir, current_description, attempts_path,
        agno_agent_factory=agno_agent_factory,
        desc_cap=settings["catalog_desc_cap"],
        case_timeout=settings["probe_case_timeout"],
    )

    # ── 6. 写回 frontmatter ─────────────────────────────────────
    best_desc = _write_best_description(
        skill_md, fm_dict, body, skill_name, best, current_description,
    )

    summary = _write_summary(
        exp_dir, skill_name, train, test, candidates, best,
        current_description, catalog_size=avg_catalog,
        no_competition=no_competition,
    )

    # 持久化离线探针触发率（看板用）。version_sha = 当前 main（本次写回将由随后
    # 的 commit 产新 sha，故此处记的是父版本/首版可空）。失败只 log 不阻断。
    _record_trigger_eval_best(skill_dir, skill_name, exp_id, best, cases, avg_catalog)

    return _optimization_result(
        best_desc, summary, avg_catalog, no_competition,
        candidates, exp_dir, budget,
    )


# ═══════════════════════════════════════════════════════════════════
# LLM 预算
# ═══════════════════════════════════════════════════════════════════

class _LLMBudgetExhausted(Exception):
    """命中 max_llm_calls 硬上限。"""


class _Budget:
    """计数每一次 LLM 消耗（llm.chat 或探针跑一轮），命中上限抛 _LLMBudgetExhausted。"""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.used = 0

    def consume(self) -> None:
        """计一次消耗（探针真跑一轮用），命中上限抛 _LLMBudgetExhausted。"""
        if self.used >= self.max_calls:
            raise _LLMBudgetExhausted()
        self.used += 1

    def chat(self, llm: Any, prompt: str) -> str:
        self.consume()
        return llm.chat(prompt)


# ═══════════════════════════════════════════════════════════════════
# case 生成 / 缓存
# ═══════════════════════════════════════════════════════════════════

def _load_or_generate_cases(
    opt_root: Path, llm: Any, budget: _Budget, skill_name: str,
    description: str, skill_content: str, n_cases: int,
) -> list[dict]:
    cache = opt_root / "cases.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                logger.info("description_opt[%s]: 复用缓存 cases.json (%d 条)",
                            skill_name, len(data))
                return _normalize_cases(data)
        except (json.JSONDecodeError, OSError):
            logger.warning("cases.json 损坏，重新生成: %s", cache)

    # case_gen.txt 含一段字面 JSON 示例（带花括号），不能用 str.format（会被
    # 当占位符解析）。这里用显式 replace 注入三个占位符，prompt 文本保持原文。
    tmpl = _load_prompt("case_gen.txt")
    prompt = (
        tmpl.replace("{skill_name}", skill_name)
        .replace("{description}", description)
        .replace("{skill_content}", skill_content)
    )
    raw = budget.chat(llm, prompt)
    cases = _parse_cases_json(raw)
    if not cases:
        raise ValueError(
            f"case 生成失败：LLM 未返回合法 JSON 数组（skill={skill_name}）"
        )
    cases = _normalize_cases(cases)[:n_cases]
    cache.write_text(json.dumps(cases, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    logger.info("description_opt[%s]: 生成 %d cases → cases.json",
                skill_name, len(cases))
    return cases


def _parse_cases_json(raw: str) -> list[dict]:
    """从 LLM 回复里抠出 JSON 数组。"""
    text = raw.strip()
    # 去掉 ```json ... ``` 围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 找第一个 [ 到最后一个 ]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


def _normalize_cases(data: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        query = str(c.get("query") or "").strip()
        if not query:
            continue
        out.append({
            "query": query,
            "should_trigger": bool(c.get("should_trigger", False)),
            "topic": str(c.get("topic") or "misc").strip() or "misc",
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# train/test split（分层 + 确定性）
# ═══════════════════════════════════════════════════════════════════

def stratified_split(
    cases: list[dict], *, train_frac: float, seed: int,
) -> tuple[list[dict], list[dict]]:
    """按 should_trigger 分层抽样，train_frac 进 train，其余进 test。

    确定性：同 seed + 同 cases 必出同样划分。
    """
    rng = random.Random(seed)
    pos = [c for c in cases if c.get("should_trigger")]
    neg = [c for c in cases if not c.get("should_trigger")]

    def _split_one(group: list[dict]) -> tuple[list[dict], list[dict]]:
        g = list(group)
        rng.shuffle(g)
        n_train = int(round(len(g) * train_frac))
        # 至少各留一个（只要 group ≥ 2）
        if len(g) >= 2:
            n_train = max(1, min(len(g) - 1, n_train))
        return g[:n_train], g[n_train:]

    pos_tr, pos_te = _split_one(pos)
    neg_tr, neg_te = _split_one(neg)
    return pos_tr + neg_tr, pos_te + neg_te


# ═══════════════════════════════════════════════════════════════════
# 触发判定（真跑代理闭环探针，见 trigger_probe.py）
# ═══════════════════════════════════════════════════════════════════

def _score_description(
    desc: str, split: list[dict], budget: _Budget,
    skill_name: str, catalog_by_query: dict, runs_per_case: int,
    exp_dir: Path, *, agno_agent_factory: Any, desc_cap: int, tag: str,
    case_timeout: float = 60.0,
) -> tuple[float, list[dict]]:
    """对一个 split 评 desc：每 case 真跑 runs_per_case 轮探针判触发，
    triggered = 命中本 skill ≥ 0.5；case pass = triggered == should_trigger。

    诱饵清单 = catalog_by_query[query]（query 锚点的真实 main 分支竞争对手）。
    返回 (pass_fraction, per_case_results)。per_case 同时落 {topic}/{job}.json，
    含 catalog 快照供看板逐 case 展示/复盘。
    """
    results: list[dict] = []
    if not split:
        return 0.0, results

    n_pass = 0
    for idx, case in enumerate(split):
        query = case["query"]
        should = bool(case["should_trigger"])
        topic = case.get("topic", "misc")
        catalog = catalog_by_query.get(query, [])
        runs: list[dict] = []
        n_hit = 0
        for _ in range(runs_per_case):
            budget.consume()  # 每跑一轮探针计一次预算（命中上限抛 _LLMBudgetExhausted）
            chosen = probe_trigger(
                query, skill_name, desc, catalog,
                agno_agent_factory=agno_agent_factory, desc_cap=desc_cap,
                case_timeout=case_timeout,
            )
            hit = chosen == skill_name
            if hit:
                n_hit += 1
            runs.append({"triggered_skill": chosen, "hit": hit})
        did_trigger = (n_hit / runs_per_case) >= 0.5
        passed = did_trigger == should
        if passed:
            n_pass += 1
        triggered_skill = skill_name if did_trigger else "NONE"
        rec = {
            "should_trigger": should,
            "did_trigger": did_trigger,
            "passed": passed,
            "query": query,
            "topic": topic,
            "triggered_skill": triggered_skill,
            "catalog": [e["name"] for e in catalog],
            "runs": runs,
        }
        results.append(rec)
        _write_case_json(exp_dir, topic, f"{tag}_{idx:02d}", rec)

    return n_pass / len(split), results


# ═══════════════════════════════════════════════════════════════════
# improve loop helpers
# ═══════════════════════════════════════════════════════════════════

def _format_scores_detail(
    train_results: list[dict], all_candidates: list[dict],
) -> str:
    """拼 FAILED-TO-TRIGGER / FALSE-TRIGGERS / PREVIOUS-ATTEMPTS 块。"""
    failed_to_trigger = [
        r for r in train_results
        if r["should_trigger"] and not r["did_trigger"]
    ]
    false_triggers = [
        r for r in train_results
        if not r["should_trigger"] and r["did_trigger"]
    ]

    lines: list[str] = []
    lines.append("FAILED-TO-TRIGGER (should have triggered but did not):")
    if failed_to_trigger:
        for r in failed_to_trigger:
            lines.append(f"  - [{r['topic']}] {r['query']}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("FALSE-TRIGGERS (triggered but should NOT have):")
    if false_triggers:
        for r in false_triggers:
            lines.append(f"  - [{r['topic']}] {r['query']}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("PREVIOUS-ATTEMPTS (description → train_score):")
    for c in all_candidates:
        lines.append(f"  - ({c['train_score']:.2f}) {c['description']}")

    return "\n".join(lines)


def _parse_new_description(raw: str) -> str:
    m = re.search(r"<new_description>(.*?)</new_description>", raw or "",
                  re.DOTALL)
    if m:
        return m.group(1).strip().strip('"').strip()
    return ""


def _enforce_limit(desc: str, llm: Any, budget: _Budget) -> str:
    """<1024 字符硬闸：超了调 shorten prompt 一次；仍超则硬截断。"""
    if len(desc) <= DESC_HARD_LIMIT:
        return desc
    tmpl = _load_prompt("shorten_description.txt")
    raw = budget.chat(llm, tmpl.format(description=desc))
    shortened = _parse_new_description(raw)
    if shortened and len(shortened) <= DESC_HARD_LIMIT:
        return shortened
    # 兜底硬截断（绝不放过 >1024 的 desc 进 frontmatter）
    return (shortened or desc)[:DESC_HARD_LIMIT]


# ═══════════════════════════════════════════════════════════════════
# 选优（test 集；D3/D4）
# ═══════════════════════════════════════════════════════════════════

def _select_best_on_test(
    candidates: list[dict], test: list[dict], budget: _Budget,
    skill_name: str, catalog_by_query: dict, runs_per_case: int,
    exp_dir: Path, current_description: str, attempts_path: Path,
    *, agno_agent_factory: Any, desc_cap: int, case_timeout: float = 60.0,
) -> dict:
    """每个候选在 TEST 上评分，选 test_score 最高；平手偏好原始 desc。"""
    for c in candidates:
        try:
            test_score, _ = _score_description(
                c["description"], test, budget, skill_name,
                catalog_by_query, runs_per_case, exp_dir,
                agno_agent_factory=agno_agent_factory, desc_cap=desc_cap,
                tag=f"iter{c['iter']}_test", case_timeout=case_timeout,
            )
        except _LLMBudgetExhausted:
            # 预算耗尽：未评的候选 test_score 留 None，不参与选优
            logger.warning(
                "description_opt[%s]: test 评估命中预算上限，候选 iter %d 起未评",
                skill_name, c["iter"],
            )
            break
        c["test_score"] = test_score
        _append_jsonl(attempts_path, {
            "iter": c["iter"], "description": c["description"],
            "train_score": c["train_score"], "test_score": test_score,
            "phase": "test",
        })

    evaluated = [c for c in candidates if c["test_score"] is not None]
    if not evaluated:
        # 一个都没评上（极端预算耗尽）→ 守住原始 desc
        original = next(
            (c for c in candidates if c["description"] == current_description),
            candidates[0],
        )
        original["test_score"] = original["test_score"] or 0.0
        return original

    best_score = max(c["test_score"] for c in evaluated)
    tied = [c for c in evaluated if c["test_score"] == best_score]
    # 平手偏好原始 desc（稳定性）
    for c in tied:
        if c["description"] == current_description:
            return c
    # 否则取最早产生的（iter 最小）
    return min(tied, key=lambda c: c["iter"])


# ═══════════════════════════════════════════════════════════════════
# archive
# ═══════════════════════════════════════════════════════════════════

def _next_exp_id(opt_root: Path) -> str:
    n = 0
    for d in opt_root.iterdir():
        if d.is_dir() and "_" in d.name:
            head = d.name.split("_", 1)[0]
            if head.isdigit():
                n = max(n, int(head))
    return f"{n + 1:03d}"


def _slug_topic(topic: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", topic.strip().lower()).strip("_")
    return s or "misc"


def _write_case_json(exp_dir: Path, topic: str, job_name: str,
                     rec: dict) -> None:
    tdir = exp_dir / _slug_topic(topic)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{job_name}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _append_jsonl(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _write_summary(
    exp_dir: Path, skill_name: str, train: list[dict], test: list[dict],
    candidates: list[dict], best: dict, current_description: str,
    *, catalog_size: int = 0, no_competition: bool = False,
) -> dict:
    if best["description"] == current_description:
        reason = (
            f"original description kept (test_score={best['test_score']:.3f}, "
            "highest or tied-highest on held-out test set)"
        )
    else:
        reason = (
            f"iter {best['iter']} chosen: highest test_score="
            f"{best['test_score']:.3f} on held-out test set (anti-overfit: "
            "selected by TEST not TRAIN)"
        )
    summary = {
        "skill_name": skill_name,
        "split": {
            "train": [{"query": c["query"],
                       "should_trigger": c["should_trigger"],
                       "topic": c.get("topic")} for c in train],
            "test": [{"query": c["query"],
                      "should_trigger": c["should_trigger"],
                      "topic": c.get("topic")} for c in test],
        },
        "candidates": [
            {"iter": c["iter"], "description": c["description"],
             "train_score": c["train_score"], "test_score": c["test_score"]}
            for c in candidates
        ],
        "best": {
            "iter": best["iter"],
            "description": best["description"],
            "train_score": best["train_score"],
            "test_score": best["test_score"],
        },
        "chosen_reason": reason,
        # 无竞争模式标记：catalog_size=0 时分数无竞争区分度，复盘/看板降权
        "catalog_size": int(catalog_size),
        "no_competition": bool(no_competition),
    }
    (exp_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return summary
