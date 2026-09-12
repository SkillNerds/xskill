"""超参对齐：SkillOpt 逐项对着 vendor yaml，评测两边同值，xskill 训练不被改掉。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from xskill.bench.dataset import skillopt_vendor_root
from xskill.bench.hparams import (
    SKILLOPT_OFFICIAL,
    SKILLOPT_YAML_SECTION,
    VENDOR_CONFIG,
    effective_workers,
    eval_notes,
    shared_eval,
    skillopt_official,
    train_hparams,
    xskill_official,
)

BENCHMARKS = ("officeqa", "spreadsheet", "alfworld")


def _merged_config(benchmark: str) -> dict:
    """把 vendor 的 _base_ 与数据集 config 合并成官方生效值。"""
    root = skillopt_vendor_root()
    path = root / VENDOR_CONFIG[benchmark]
    if not path.is_file():
        pytest.skip(f"vendor config missing: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base_rel = doc.pop("_base_", None)
    merged: dict = {}
    if base_rel:
        base_path = (path.parent / base_rel).resolve()
        merged = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    for section, values in doc.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values
    return merged


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_skillopt_train_hparams_match_vendor_yaml(benchmark):
    merged = _merged_config(benchmark)
    table = SKILLOPT_OFFICIAL[benchmark]["train"]
    assert set(table) == set(SKILLOPT_YAML_SECTION), "表里每个键都要说清出自哪个 section"
    for key, want in table.items():
        section = SKILLOPT_YAML_SECTION[key]
        got = merged[section][key]
        assert got == want, f"{benchmark}.{section}.{key}: yaml={got!r} 表={want!r}"


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_skillopt_env_hparams_match_vendor_yaml(benchmark):
    merged = _merged_config(benchmark)
    env = dict(merged["env"])
    for key, want in SKILLOPT_OFFICIAL[benchmark]["env"].items():
        if key == "mode":
            # 唯一允许的覆盖：官方 multi，exec target 只能 single。
            assert env[key] == "multi"
            assert want == "single"
            continue
        if key == "use_local_tools":
            # officeqa 的 yaml 没写这项，官方 entrypoint 显式传 true。
            continue
        assert env[key] == want, f"{benchmark}.env.{key}: yaml={env[key]!r} 表={want!r}"


def test_skillopt_deviations_are_declared():
    for benchmark in BENCHMARKS:
        spec = skillopt_official(benchmark)
        assert "evaluation.eval_test" in spec["deviations"]
    assert "env.mode" in skillopt_official("spreadsheet")["deviations"]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_eval_hparams_identical_for_both_algos(benchmark):
    from xskill.bench.pipeline import resolve_eval_hparams

    xskill_side = resolve_eval_hparams(benchmark)
    skillopt_side = resolve_eval_hparams(benchmark)
    assert xskill_side == skillopt_side
    workers, timeout_s, turns = xskill_side
    spec = shared_eval(benchmark)
    assert timeout_s == spec.item_timeout_s
    assert turns == spec.max_tool_turns
    assert workers == effective_workers(benchmark)[1]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_eval_turns_come_from_skillopt_official(benchmark):
    merged = _merged_config(benchmark)
    spec = shared_eval(benchmark)
    env = merged["env"]
    assert spec.workers == env["workers"]
    if benchmark == "officeqa":
        assert spec.max_tool_turns == env["max_tool_turns"]
    elif benchmark == "spreadsheet":
        assert spec.max_tool_turns == env["max_turns"]
        assert spec.item_timeout_s == env["exec_timeout"]
    else:
        assert spec.max_steps == env["max_steps"]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_eval_notes_record_official_source_and_cap(benchmark):
    for algo in ("xskill", "skillopt"):
        note = eval_notes(benchmark, algo)
        assert "评测超参两算法同值" in note
        assert "出处" in note
    official, used = effective_workers(benchmark)
    if official != used:
        assert "封顶" in eval_notes(benchmark, "xskill")


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_limit_runs_are_labelled_as_smoke_not_official(benchmark):
    """--limit 的一趟不许在卡片里冒充官方口径。"""
    from xskill.bench.hparams import train_notes as notes

    for algo in ("xskill", "skillopt"):
        assert "接线冒烟" not in eval_notes(benchmark, algo)
        assert "接线冒烟" not in notes(benchmark, algo)
        smoke = eval_notes(benchmark, algo, limit=2)
        assert "接线冒烟：--limit 2" in smoke
        assert "不是官方口径" in smoke
        assert "接线冒烟：--limit 3" in notes(benchmark, algo, limit=3)
    # xskill 冒烟趟镜像真吃到的是缩水值，别把官方值写成这趟用的值。
    full = notes(benchmark, "xskill")
    smoke = notes(benchmark, "xskill", limit=3)
    assert "训练超参用 xskill 官方训练镜像默认值" in full
    assert "训练超参用 xskill 官方训练镜像默认值" not in smoke
    assert "这趟没按它跑" in smoke


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_xskill_train_hparams_follow_the_smoke_shrink(benchmark):
    """run_config 写的 workers/超时要跟镜像真吃到的一致，不能照抄官方值。"""
    from xskill.bench.xskill_image import build_train_env, effective_hparams

    official = xskill_official(benchmark)
    workers, timeout_s, turns = train_hparams(benchmark, "xskill", limit=1)
    used = effective_hparams(
        benchmark, build_train_env(benchmark=benchmark, model="m", limit=1, n_train_items=1)
    )
    assert workers == used["workers"] != int(official["workers"])
    assert int(timeout_s) == used["item_timeout_s"] != int(official["item_timeout_s"])
    assert turns == int(official["max_tool_turns"])


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_xskill_train_hparams_ignore_cli_defaults(benchmark):
    official = xskill_official(benchmark)
    workers, timeout_s, turns = train_hparams(benchmark, "xskill")
    assert workers == official["workers"]
    assert timeout_s == official["item_timeout_s"]
    assert turns == official["max_tool_turns"]
    # 命令行默认值是 4；xskill 训练不许被它改成 4。
    assert workers == 3
    # 显式传参才覆盖。
    assert train_hparams(benchmark, "xskill", workers=9)[0] == 9


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_xskill_provenance_hparams_tell_the_truth_about_smoke_shrink(benchmark):
    """provenance 里要写镜像真吃到的值，不是照抄官方表。"""
    from xskill.bench.xskill_image import build_train_env, effective_hparams

    official = xskill_official(benchmark)
    full_env = build_train_env(benchmark=benchmark, model="m", limit=0)
    full = effective_hparams(benchmark, full_env)
    assert full["source"] == "xskill_official_image_defaults"
    assert "official" not in full
    for name in ("epochs", "workers", "canary_settle_s", "item_timeout_s", "max_tool_turns"):
        assert full[name] == int(official[name]), name

    smoke_env_map = build_train_env(benchmark=benchmark, model="m", limit=1, n_train_items=2)
    smoke = effective_hparams(benchmark, smoke_env_map)
    assert smoke["source"] == "smoke_shortened"
    assert smoke["epochs"] == 1 and smoke["workers"] == 1
    assert smoke["official"]["epochs"] == int(official["epochs"])
    assert smoke["official"]["workers"] == int(official["workers"])


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_skillopt_train_workers_from_official_env(benchmark):
    workers, timeout_s, _turns = train_hparams(benchmark, "skillopt")
    assert workers == effective_workers(benchmark)[1]
    assert timeout_s == SKILLOPT_OFFICIAL[benchmark]["vendor_exec_timeout_s"]


def test_officeqa_exec_timeout_is_the_vendor_hardcoded_value():
    rollout = skillopt_vendor_root() / "skillopt/envs/officeqa/rollout.py"
    if not rollout.is_file():
        pytest.skip("vendor rollout missing")
    text = rollout.read_text(encoding="utf-8")
    # officeqa 的 exec 做题每轮超时在 vendor 里写死 180，配置改不动。
    assert "timeout=180," in text
    assert SKILLOPT_OFFICIAL["officeqa"]["vendor_exec_timeout_s"] == 180


def test_worker_cap_is_shared_and_overridable(monkeypatch):
    from xskill.bench import hparams

    monkeypatch.setenv(hparams.MAX_WORKERS_ENV, "2")
    for benchmark in BENCHMARKS:
        official, used = effective_workers(benchmark)
        assert used == min(official, 2)
    monkeypatch.setenv(hparams.MAX_WORKERS_ENV, "0")
    with pytest.raises(ValueError):
        hparams.host_worker_cap()


def test_untouched_vendor_configs_are_not_rewritten():
    root = skillopt_vendor_root()
    for rel in VENDOR_CONFIG.values():
        path = root / rel
        if not path.is_file():
            pytest.skip(f"vendor config missing: {path}")
        text = path.read_text(encoding="utf-8")
        assert "xskill bench" not in text, f"不许改 vendor 官方配置: {rel}"
    bench_src = Path(__file__).resolve().parents[1] / "src/xskill/bench"
    # 超参只能来自 hparams.py，别的模块不许再写死 epoch 与 batch。
    for name in ("pipeline.py", "xskill_image.py", "skillopt_train.py"):
        text = (bench_src / name).read_text(encoding="utf-8")
        assert "num_epochs=4" not in text
        assert "batch_size=40" not in text
