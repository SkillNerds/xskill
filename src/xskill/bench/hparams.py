"""官方超参数单一来源。

SkillOpt 的值逐条抄自 vendor 仓库 `configs/`（`_base_/default.yaml` 加各数据集
`default.yaml`），以及 vendor 代码里写死、配置改不动的值。xskill 的值是官方训练镜像
entrypoint 里的默认值。

三个来源分开放，不混成一个数：

- `SKILLOPT_OFFICIAL[bench]["train"]`：SkillOpt 官方训练超参，原样传给官方
  `scripts/train.py`。
- `SKILLOPT_OFFICIAL[bench]["env"]`：官方做题环境超参。其中 workers、
  max_tool_turns 是两边评测都要对齐的部分。
- `XSKILL_OFFICIAL[bench]`：xskill 官方训练镜像自己的超参，不跟 SkillOpt 对齐，
  也不许被命令行默认值改掉。

`shared_eval` 是本框架给两边评测用的同一套值：能抄官方就抄官方，官方没定的写清
出处。并发另有本机上限，见 `effective_workers`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

VENDOR_CONFIG = {
    "officeqa": "configs/officeqa/default.yaml",
    "spreadsheet": "configs/spreadsheetbench/default.yaml",
    "alfworld": "configs/alfworld/default.yaml",
}

# SkillOpt 官方 env 名（vendor 里的 env.name，和本框架的 benchmark 名不一样）。
SKILLOPT_ENV_NAME = {
    "officeqa": "officeqa",
    "spreadsheet": "spreadsheetbench",
    "alfworld": "alfworld",
}

# 每个键都能在 vendor 的 yaml 里找到对应项；哪个 section 见 SKILLOPT_YAML_SECTION。
_BASE_TRAIN = {
    "num_epochs": 4,
    "train_size": 0,
    "batch_size": 40,
    "accumulation": 1,
    "seed": 42,
    "minibatch_size": 8,
    "merge_batch_size": 8,
    "analyst_workers": 16,
    "max_analyst_rounds": 3,
    "failure_only": False,
    "learning_rate": 4,
    "min_learning_rate": 2,
    "lr_scheduler": "cosine",
    "lr_control_mode": "fixed",
    "skill_update_mode": "patch",
    "use_slow_update": True,
    "slow_update_samples": 20,
    "slow_update_gate_with_selection": False,
    "longitudinal_pair_policy": "mixed",
    "use_meta_skill": True,
    "use_gate": True,
    "sel_env_num": 0,
    "test_env_num": 0,
    "eval_test": True,
    "reasoning_effort": "medium",
}

# 官方 yaml 里这些键各自属于哪个 section，给对照测试用。
SKILLOPT_YAML_SECTION = {
    "num_epochs": "train",
    "train_size": "train",
    "batch_size": "train",
    "accumulation": "train",
    "seed": "train",
    "minibatch_size": "gradient",
    "merge_batch_size": "gradient",
    "analyst_workers": "gradient",
    "max_analyst_rounds": "gradient",
    "failure_only": "gradient",
    "learning_rate": "optimizer",
    "min_learning_rate": "optimizer",
    "lr_scheduler": "optimizer",
    "lr_control_mode": "optimizer",
    "skill_update_mode": "optimizer",
    "use_slow_update": "optimizer",
    "slow_update_samples": "optimizer",
    "slow_update_gate_with_selection": "optimizer",
    "longitudinal_pair_policy": "optimizer",
    "use_meta_skill": "optimizer",
    "use_gate": "evaluation",
    "sel_env_num": "evaluation",
    "test_env_num": "evaluation",
    "eval_test": "evaluation",
    "reasoning_effort": "model",
}

# 三个数据集共同的偏离：训练阶段不跑 SkillOpt 自带的 test 集评测。
# 官方 evaluation.eval_test 默认 true，但本框架的合同禁止训练碰 test split，
# 测试集只在评测阶段由本框架统一跑。打榜镜像的 entrypoint 也一样传 false。
EVAL_TEST_DEVIATION = {
    "evaluation.eval_test": "官方 true → false：训练阶段不许碰 test split，测试集只在评测阶段跑",
}

SKILLOPT_OFFICIAL: dict[str, dict[str, Any]] = {
    "officeqa": {
        "train": dict(_BASE_TRAIN),
        "env": {
            "workers": 4,
            "max_tool_turns": 24,
            "max_completion_tokens": 16384,
            "search_mode": "offline",
            "max_queries_per_turn": 4,
            "search_timeout_seconds": 20,
            "use_local_tools": True,
        },
        # officeqa 的 exec 做题在 vendor rollout.py 里每轮写死 180 秒，配置改不动，
        # 且 env.exec_timeout 在这个数据集上没被读。
        "vendor_exec_timeout_s": 180,
    },
    "spreadsheet": {
        "train": {**_BASE_TRAIN, "train_size": 80},
        "env": {
            "workers": 24,
            "max_turns": 30,
            "max_completion_tokens": 16384,
            "exec_timeout": 600,
            # 官方 default.yaml 写的是 mode: multi，但 vendor 的
            # SpreadsheetBenchAdapter.setup 明确禁止 exec target 配 multi，
            # 只允许 single。合同锁定 SkillOpt 答题走 claude_code_exec，故取 single。
            "mode": "single",
        },
        "vendor_exec_timeout_s": 600,
        "deviations": {
            "env.mode": "官方 multi → single：vendor 适配器禁止 exec target 用 multi",
        },
    },
    "alfworld": {
        "train": dict(_BASE_TRAIN),
        "env": {
            "workers": 8,
            "max_api_workers": 8,
            "max_steps": 50,
            "max_completion_tokens": 16384,
        },
        "vendor_exec_timeout_s": 120,
    },
}

# xskill 官方训练镜像 entrypoint 的默认值。评测侧不用这些，只训练用。
XSKILL_OFFICIAL: dict[str, dict[str, Any]] = {
    "officeqa": {
        "epochs": 4,
        "workers": 3,
        "canary_settle_s": 600,
        "item_timeout_s": 600,
        "max_tool_turns": 24,
        "merge_val_into_train": True,
    },
    "spreadsheet": {
        "epochs": 4,
        "workers": 3,
        "canary_settle_s": 600,
        "item_timeout_s": 420,
        "max_tool_turns": 24,
        "merge_val_into_train": True,
    },
    "alfworld": {
        "epochs": 4,
        "workers": 3,
        "canary_settle_s": 600,
        "item_timeout_s": 420,
        "max_tool_turns": 24,
        "merge_val_into_train": True,
    },
}


@dataclass(frozen=True)
class SharedEval:
    """两个算法评测时必须一致的那几项。"""

    workers: int
    max_tool_turns: int
    item_timeout_s: float
    max_steps: int | None
    source: str


# item_timeout_s 是单题墙上时钟预算。官方只有 spreadsheet 定了 600
# （env.exec_timeout）；officeqa 与 alfworld 官方配置没有单题墙上时钟，取打榜两边
# 一直在用的 600，两个算法同值。
_SHARED_EVAL = {
    "officeqa": SharedEval(
        workers=4,
        max_tool_turns=24,
        item_timeout_s=600.0,
        max_steps=None,
        source=(
            "workers、max_tool_turns 抄 configs/officeqa/default.yaml；"
            "单题 600 秒为打榜沿用值，官方该数据集未定单题墙上时钟"
        ),
    ),
    "spreadsheet": SharedEval(
        workers=24,
        max_tool_turns=30,
        item_timeout_s=600.0,
        max_steps=None,
        source=(
            "workers、max_turns、exec_timeout 抄 configs/spreadsheetbench/default.yaml"
        ),
    ),
    "alfworld": SharedEval(
        workers=8,
        max_tool_turns=16,
        item_timeout_s=600.0,
        max_steps=50,
        source=(
            "workers、max_steps 抄 configs/alfworld/default.yaml；"
            "单局 600 秒与每 step 16 轮为打榜沿用值，官方未定这两项"
        ),
    ),
}

MAX_WORKERS_ENV = "XSKILL_BENCH_MAX_WORKERS"


def shared_eval(benchmark: str) -> SharedEval:
    try:
        return _SHARED_EVAL[benchmark]
    except KeyError as exc:
        raise ValueError(f"no shared eval hyperparameters for {benchmark}") from exc


def host_worker_cap() -> int:
    """本机并发上限。两个算法同样受限，避免 24 并发把 4 核机器压垮。"""
    raw = os.environ.get(MAX_WORKERS_ENV)
    if raw:
        capped = int(raw)
        if capped < 1:
            raise ValueError(f"{MAX_WORKERS_ENV} must be >= 1")
        return capped
    return max(os.cpu_count() or 4, 1)


def effective_workers(benchmark: str) -> tuple[int, int]:
    """返回（官方 workers，本机实际 workers）。"""
    official = shared_eval(benchmark).workers
    return official, min(official, host_worker_cap())


def eval_notes(benchmark: str, algo: str, *, limit: int = 0) -> str:
    """写进 run_config.harness.notes 的对齐说明。schema 不许加新字段，只能写这里。"""
    from xskill.bench.constants import HARNESS_NOTES

    spec = shared_eval(benchmark)
    official, used = effective_workers(benchmark)
    harness_id = (
        "claude_code_native_skills" if algo == "xskill" else "skillopt_claude_code_exec"
    )
    parts = [
        HARNESS_NOTES[harness_id],
        f"评测超参两算法同值：workers={used}、max_tool_turns={spec.max_tool_turns}、"
        f"单题 {int(spec.item_timeout_s)} 秒",
    ]
    if spec.max_steps is not None:
        parts.append(f"max_steps={spec.max_steps}")
    parts.append(f"出处：{spec.source}")
    if used != official:
        parts.append(
            f"官方 workers={official}，本机 {host_worker_cap()} 核封顶到 {used}，两算法同样封顶"
        )
    if limit:
        parts.append(limit_notice(limit))
    return "；".join(parts)


def train_hparams(
    benchmark: str,
    algo: str,
    *,
    workers: int | None = None,
    timeout_s: float | None = None,
    max_tool_turns: int | None = None,
    limit: int = 0,
) -> tuple[int, float, int]:
    """训练阶段各走各家官方值。显式传参才覆盖。"""
    if algo == "xskill":
        official = xskill_official(benchmark)
        if limit:
            # 冒烟趟镜像会把这些改小，run_config 得写镜像真吃到的值。
            from xskill.bench.xskill_image import smoke_env

            shrunk = smoke_env(limit, n_train_items=limit)
            official = {
                **official,
                "workers": int(shrunk["XSKILL_WORKERS"]),
                "item_timeout_s": int(shrunk["ITEM_TIMEOUT"]),
            }
        return (
            int(workers) if workers else int(official["workers"]),
            float(timeout_s) if timeout_s else float(official["item_timeout_s"]),
            int(max_tool_turns) if max_tool_turns else int(official["max_tool_turns"]),
        )
    spec = skillopt_official(benchmark)
    _official_workers, capped = effective_workers(benchmark)
    return (
        int(workers) if workers else capped,
        float(timeout_s) if timeout_s else float(spec["vendor_exec_timeout_s"]),
        int(max_tool_turns) if max_tool_turns else shared_eval(benchmark).max_tool_turns,
    )


def limit_notice(limit: int) -> str:
    """--limit 是接线冒烟，派生超参会跟着缩水，不能当官方成绩。"""
    return f"接线冒烟：--limit {int(limit)}，题量与派生超参已缩水，不是官方口径的一趟"


def train_notes(benchmark: str, algo: str, *, limit: int = 0) -> str:
    from xskill.bench.constants import HARNESS_NOTES

    if algo == "xskill":
        official = xskill_official(benchmark)
        lead = "训练超参用 xskill 官方训练镜像默认值，不跟 SkillOpt 对齐、也不接命令行默认值："
        if limit:
            # 冒烟趟这些数已经被改小了，别把官方值说成这趟用的值。
            lead = "xskill 官方训练镜像默认值（这趟没按它跑，真吃到的值见 provenance 的 hyperparameters）："
        parts = [
            HARNESS_NOTES["claude_code_native_skills"],
            lead + f"epochs={official['epochs']}、simulated user workers={official['workers']}、"
            f"canary settle={official['canary_settle_s']} 秒、"
            f"单题 {official['item_timeout_s']} 秒",
        ]
        if limit:
            parts.append(limit_notice(limit))
        return "；".join(parts)
    spec = skillopt_official(benchmark)
    train = spec["train"]
    official_workers, used = effective_workers(benchmark)
    parts = [
        HARNESS_NOTES["skillopt_claude_code_exec"],
        f"训练走官方 scripts/train.py，超参来自 {spec['config']}："
        f"epochs={train['num_epochs']}、batch={train['batch_size']}、"
        f"minibatch={train['minibatch_size']}、改写模式={train['skill_update_mode']}、"
        f"gate={'开' if train['use_gate'] else '关'}、seed={train['seed']}",
        f"rollout workers={used}",
    ]
    if used != official_workers:
        parts.append(f"官方 workers={official_workers}，本机封顶到 {used}")
    parts.append("偏离：" + "；".join(f"{k} {v}" for k, v in spec["deviations"].items()))
    if limit:
        parts.append(limit_notice(limit))
    return "；".join(parts)


def xskill_official(benchmark: str) -> dict[str, Any]:
    try:
        return dict(XSKILL_OFFICIAL[benchmark])
    except KeyError as exc:
        raise ValueError(f"no official xskill train hyperparameters for {benchmark}") from exc


def skillopt_official(benchmark: str) -> dict[str, Any]:
    try:
        spec = SKILLOPT_OFFICIAL[benchmark]
    except KeyError as exc:
        raise ValueError(f"no official SkillOpt hyperparameters for {benchmark}") from exc
    out = {
        "config": VENDOR_CONFIG[benchmark],
        "env_name": SKILLOPT_ENV_NAME[benchmark],
        "train": dict(spec["train"]),
        "env": dict(spec["env"]),
        "vendor_exec_timeout_s": spec["vendor_exec_timeout_s"],
        "deviations": {**EVAL_TEST_DEVIATION, **dict(spec.get("deviations") or {})},
    }
    return out
