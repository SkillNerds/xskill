"""第七步：xskill bench 命令、假做题落盘、隔离旧合同与拆分器。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from xskill.cli import build_parser, main as xskill_main
from xskill.bench.cli import dispatch_bench
from xskill.bench.pipeline import BenchError, resolve_harness
from xskill.bench.split import resolve_train_uids
from xskill.bench.scorer import assert_no_reward_imported, officeqa_evaluate

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
UNTOUCHED = {
    "scripts/bench/README.md": "2f3fb4030e0a3eb32be655dd5f994f04db47a9b0041300942a885f9383b8387b",
    "scripts/bench/evaluate.py": "7fa0916005ec820cbeedc1ba6d7c8b5211c56990f8b14c4d3db1840feb14600d",
    "scripts/bench/run_baseline.py": "3f28cac87db835ccbc13202b5fc962fa5fd5e530f0907a89f265fbf4e2b730ba",
    "scripts/bench/officeqa/import_leaderboard_eval.py": "d3a3794daaf084ef5f89b315ddfad35f37ddbd5ecd4e462297be437028699836",
    "scripts/bench/spreadsheet/import_leaderboard_eval.py": "9402c9460d25749b40f33a1a908059354efa6e53e5115c12e6f1a1ee04052c27",
    "scripts/bench/alfworld/import_leaderboard_eval.py": "d8042ff67dca1cc8ad9a5565b3ea40b3207327c06cd941f8b8e5c36d1944474f",
    "scripts/bench/contract_import.py": "9991c0985a757d4e16481f158169d8eac94e613de3f809907e37812616d7bec1",
    "benchmarks/validate.py": "77fa03d47e62621ac9fa9f01b6410d18645142109383c5d8c40cac97c1155cb4",
    "benchmarks/schemas/run_config.schema.json": "38ff0c0dc8d9fece7483d4f9c21144ffa90bf4112bbf1c00434f451f4b3d027a",
    "benchmarks/schemas/summary.schema.json": "fc5a94373598edb973a3ba960847bdcdaa7f9a7541a5b750629aec61b3acf949",
    "benchmarks/schemas/result_record.schema.json": "53baf5a314cbf89023ca108438923977c9387b0b190d88cc4c778a3801b4fb78",
    "benchmarks/schemas/train_provenance.schema.json": "bd94571bad4b409b2150d498c66cc65b273ad9826259e0e45fbf87f9dec12f60",
}


def _sha(rel: str) -> str:
    data = (ROOT / rel).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def _skill_dir(tmp_path: Path) -> Path:
    pkg = tmp_path / "skills" / "officeqa-expert"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text("---\nname: officeqa-expert\n---\nLook up the number.\n", encoding="utf-8")
    return tmp_path / "skills"


def _skill_file(tmp_path: Path) -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text("# skillopt officeqa\nRead the docs.\n", encoding="utf-8")
    return path


def test_untouched_contract_and_splitter_files():
    for rel, digest in UNTOUCHED.items():
        assert _sha(rel) == digest, rel
    text = (ROOT / "scripts/bench/README.md").read_text(encoding="utf-8")
    assert "轨迹拆分" in text
    assert "officeqa" not in text.lower()
    assert not (ROOT / "scripts/bench/officeqa/vendor/reward.py").exists()
    assert not (ROOT / "src/xskill/bench/reward.py").exists()


def test_no_litellm_import():
    import xskill.bench  # noqa: F401
    assert "litellm" not in sys.modules


def test_help_lists_bench_and_old_commands(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "bench" in out
    assert "serve" in out
    assert "search" in out


def test_proposal_eval_xskill_parses(tmp_path):
    skill = _skill_dir(tmp_path)
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--split",
            "test",
            "--model",
            "deepseek-v4-flash",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert args.command == "bench"
    assert args.bench_action == "eval"
    assert args.harness == "native_cc"
    assert args.skill_dir == str(skill)


def test_proposal_eval_skillopt_parses(tmp_path):
    skill = _skill_file(tmp_path)
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--split",
            "test",
            "--model",
            "deepseek-v4-flash",
            "--algo",
            "skillopt",
            "--harness",
            "claude_code_exec",
            "--skill-file",
            str(skill),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert args.skill_file == str(skill)
    assert args.algo == "skillopt"


def test_proposal_train_and_run_parse(tmp_path):
    train = build_parser().parse_args(
        [
            "bench",
            "train",
            "--benchmark",
            "spreadsheet",
            "--split",
            "train",
            "--model",
            "deepseek-v4-flash",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(tmp_path / "train"),
        ]
    )
    run = build_parser().parse_args(
        [
            "bench",
            "run",
            "--benchmark",
            "alfworld",
            "--model",
            "deepseek-v4-flash",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(tmp_path / "pipe"),
        ]
    )
    assert train.bench_action == "train"
    assert run.bench_action == "run"


def test_reject_mismatched_harness():
    with pytest.raises(BenchError):
        resolve_harness("xskill", "claude_code_exec")
    with pytest.raises(BenchError):
        resolve_harness("skillopt", "native_cc")


def test_official_merge_counts_do_not_rewrite_manifest():
    manifest = BENCH / "officeqa/manifests/officeqa_skillopt_id_split.json"
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    path, uids, split, merge = resolve_train_uids("officeqa", "xskill")
    assert path == manifest
    assert len(uids) == 74
    assert split == "train_plus_val"
    assert merge is True
    _, ss, _, ss_merge = resolve_train_uids("spreadsheet", "xskill")
    _, alf, _, alf_merge = resolve_train_uids("alfworld", "xskill")
    _, so, so_split, so_merge = resolve_train_uids("officeqa", "skillopt")
    assert len(ss) == 120 and ss_merge is True
    assert len(alf) == 57 and alf_merge is True
    assert len(so) == 50 and so_split == "train" and so_merge is False
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before


def test_officeqa_scorer_is_skillopt_evaluate_not_reward():
    src = (ROOT / "src/xskill/bench/scorer.py").read_text(encoding="utf-8")
    assert "reward.py" in src
    assert "refused" in src or "must not" in src
    try:
        result = officeqa_evaluate("1,284,500,000", "1284500000")
    except RuntimeError as exc:
        if "not found" in str(exc):
            pytest.skip("SkillOpt OfficeQA evaluate() not installed")
        raise
    assert float(result["em"]) == 1.0
    assert_no_reward_imported()


def test_fake_eval_writes_four_files_and_card(tmp_path, capsys):
    skill = _skill_dir(tmp_path)
    out = tmp_path / "eval-officeqa-xskill-test"
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--split",
            "test",
            "--model",
            "deepseek-v4-flash",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(out),
            "--limit",
            "3",
            "--run-id",
            "eval-officeqa-xskill-test",
        ]
    )
    assert dispatch_bench(args) == 0
    printed = capsys.readouterr().out
    assert "Initializing benchmark: OfficeQA (Split: test, 3 items)" in printed
    assert "Harness: native_cc (Tools: Read, Bash, Skill)" in printed
    assert "counted as unpassed" in printed
    assert "Benchmark Results: OfficeQA (test)" in printed
    assert "Artifacts saved to:" in printed
    assert (out / "run_config.json").is_file()
    assert (out / "results.jsonl").is_file()
    assert (out / "summary.json").is_file()
    assert not (out / "train_provenance.json").is_file()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_total"] == 3
    assert summary["n_pass"] == 1
    assert summary["status_counts"]["timeout"] == 1
    assert summary["status_counts"]["fail"] == 1
    assert abs(summary["accuracy"] - 1 / 3) < 1e-9
    assert summary["usage_totals"]["usage_missing_n"] == 3
    cfg = json.loads((out / "run_config.json").read_text(encoding="utf-8"))
    assert cfg["harness"]["tools"] == ["Read", "Bash", "Skill"]
    assert cfg["scorer"]["id"] == "skillopt.envs.officeqa.evaluator.evaluate"
    assert "reward" not in cfg["scorer"]["id"]
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    timeout = next(r for r in rows if r["status"] == "timeout")
    assert timeout["is_correct"] is False
    manifest_before = hashlib.sha256(
        (BENCH / "officeqa/manifests/officeqa_skillopt_id_split.json").read_bytes()
    ).hexdigest()
    assert summary["refs"]["split_manifest_sha256"] == manifest_before


def test_fake_skillopt_eval_tools(tmp_path):
    out = tmp_path / "eval-skillopt"
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "skillopt",
            "--harness",
            "claude_code_exec",
            "--skill-file",
            str(_skill_file(tmp_path)),
            "--output-dir",
            str(out),
            "--limit",
            "2",
        ]
    )
    assert dispatch_bench(args) == 0
    cfg = json.loads((out / "run_config.json").read_text(encoding="utf-8"))
    assert cfg["harness"]["id"] == "skillopt_claude_code_exec"
    assert cfg["harness"]["tools"] == ["Read", "Bash"]
    assert "Skill" not in cfg["harness"]["tools"]


def test_fake_train_xskill_and_skillopt(tmp_path):
    x_out = tmp_path / "train-xskill"
    args = build_parser().parse_args(
        [
            "bench",
            "train",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(x_out),
        ]
    )
    assert dispatch_bench(args) == 0
    prov = json.loads((x_out / "train_provenance.json").read_text(encoding="utf-8"))
    assert prov["merge_val_into_train"] is True
    assert prov["selection_split"] is None
    manifest = json.loads((BENCH / "officeqa/manifests/officeqa_skillopt_id_split.json").read_text())
    expected = hashlib.sha256(
        ("\n".join(sorted(manifest["train"] + manifest["val"])) + "\n").encode()
    ).hexdigest()
    assert prov["train_uids_sha256"] == expected
    assert (x_out / "checkpoints/step_1").is_dir()
    assert (x_out / "skill").is_dir()

    s_out = tmp_path / "train-skillopt"
    args = build_parser().parse_args(
        [
            "bench",
            "train",
            "--benchmark",
            "officeqa",
            "--algo",
            "skillopt",
            "--harness",
            "claude_code_exec",
            "--output-dir",
            str(s_out),
        ]
    )
    assert dispatch_bench(args) == 0
    prov = json.loads((s_out / "train_provenance.json").read_text(encoding="utf-8"))
    assert prov["merge_val_into_train"] is False
    assert prov["selection_split"] == "val"
    assert prov["optimizer_backend"] == "openai_chat"


def test_fake_run_pipeline(tmp_path):
    out = tmp_path / "pipeline-alfworld"
    args = build_parser().parse_args(
        [
            "bench",
            "run",
            "--benchmark",
            "alfworld",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(out),
            "--limit",
            "3",
        ]
    )
    assert dispatch_bench(args) == 0
    assert (out / "train/train_provenance.json").is_file()
    assert (out / "eval/summary.json").is_file()
    assert (out / "console.log").is_file()
    summary = json.loads((out / "eval/summary.json").read_text(encoding="utf-8"))
    assert summary["benchmark"] == "alfworld"
    assert summary["n_total"] == 3


def test_safe_write_survives_cp1252_progress_bar():
    from xskill.bench.card import progress_line
    from xskill.bench.stdio import safe_write

    class Cp1252:
        encoding = "cp1252"

        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, text: str) -> int:
            text.encode("cp1252")
            self.chunks.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    handle = Cp1252()
    safe_write(progress_line(3, 3, 1.0), handle)
    assert handle.chunks
    assert "Evaluating" in "".join(handle.chunks)


def test_run_finishes_eval_when_stdout_pipe_breaks(tmp_path, monkeypatch):
    import os
    import sys

    out = tmp_path / "pipe-run"
    reader, writer = os.pipe()
    os.close(reader)
    monkeypatch.setattr(sys, "stdout", os.fdopen(writer, "w"))
    args = build_parser().parse_args(
        [
            "bench",
            "run",
            "--benchmark",
            "alfworld",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(out),
            "--limit",
            "3",
        ]
    )
    assert dispatch_bench(args) == 0
    assert (out / "eval/summary.json").is_file()
    log = (out / "console.log").read_text(encoding="utf-8")
    assert "Overall Accuracy" in log
    assert "Train Complete" in log


def test_eval_xskill_without_skill_dir_fails(tmp_path, capsys):
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert dispatch_bench(args) == 2
    assert "skill-dir" in capsys.readouterr().out


def test_xskill_main_bench_does_not_need_config(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    skill = _skill_dir(tmp_path)
    rc = xskill_main(
        [
            "bench",
            "eval",
            "--benchmark",
            "spreadsheet",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(tmp_path / "ss"),
            "--limit",
            "2",
        ]
    )
    assert rc == 0
    assert (tmp_path / "ss/summary.json").is_file()


def test_subprocess_xskill_bench_eval(tmp_path):
    skill = _skill_dir(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "xskill",
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(tmp_path / "sub"),
            "--limit",
            "3",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**dict(**{k: v for k, v in __import__("os").environ.items()}), "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Benchmark Results: OfficeQA" in proc.stdout
    assert (tmp_path / "sub/summary.json").is_file()


def test_officeqa_evaluate_script_replays_card(tmp_path):
    skill = _skill_dir(tmp_path)
    out = tmp_path / "replay"
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(out),
            "--limit",
            "3",
        ]
    )
    assert dispatch_bench(args) == 0
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "officeqa_evaluate_script", ROOT / "scripts/bench/officeqa/evaluate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--run-dir", str(out)]) == 0


def test_cli_has_no_executor_flag(tmp_path, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "bench",
                "train",
                "--benchmark",
                "officeqa",
                "--algo",
                "xskill",
                "--harness",
                "native_cc",
                "--output-dir",
                str(tmp_path / "out"),
                "--executor",
                "fake",
            ]
        )
    with pytest.raises(SystemExit) as exc:
        xskill_main(["bench", "train", "-h"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--executor" not in help_text
    assert "--fake-script" not in help_text


def test_resolve_executor_defaults_to_live_without_fake_env(monkeypatch):
    from xskill.bench.mode import resolve_executor

    monkeypatch.delenv("XSKILL_BENCH_FAKE", raising=False)
    assert resolve_executor() == "live"
    assert resolve_executor("fake") == "fake"
    monkeypatch.setenv("XSKILL_BENCH_FAKE", "1")
    assert resolve_executor() == "fake"


def test_live_train_mocked_writes_real_log(tmp_path, monkeypatch):
    from xskill.bench.pipeline import run_train
    from xskill.bench.skill_io import hash_skill
    from xskill.bench.xskill_image import ImageTrainResult

    def fake_image(**kwargs):
        skill_out = kwargs["skill_out"]
        skill_out.mkdir(parents=True, exist_ok=True)
        pkg = skill_out / "officeqa-expert"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "SKILL.md").write_text("---\nname: officeqa-expert\n---\nfrom image\n", encoding="utf-8")
        sha = hash_skill(skill_dir=skill_out)
        ckpt = kwargs["output_dir"] / "checkpoints" / "step_1"
        ckpt.mkdir(parents=True)
        log = kwargs["output_dir"] / "logs" / "step_1.log"
        log.parent.mkdir(parents=True)
        log.write_text(
            "xskill image train officeqa image=localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-20260903 items=1 limit=1\n"
            "algo=noforce-canary (not Chat rewrite)\n",
            encoding="utf-8",
        )
        return ImageTrainResult(
            frozen_sha=sha,
            checkpoints=[
                {
                    "step": 1,
                    "skill_sha256": sha,
                    "checkpoint_dir": str(ckpt),
                    "action": "merged_to_main",
                    "log_file": str(log),
                }
            ],
            log_text=log.read_text(encoding="utf-8"),
            action="merged_to_main",
            image="localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-20260903",
            n_skills=1,
            hyperparameters={
                "epochs": 1,
                "workers": 1,
                "source": "smoke_shortened",
                "official": {"epochs": 4, "workers": 3},
            },
        )

    monkeypatch.setattr("xskill.bench.pipeline.run_xskill_image_train", fake_image)

    def boom(**kwargs):
        raise AssertionError("xskill must not go through the SkillOpt official trainer")

    monkeypatch.setattr("xskill.bench.pipeline.run_official_train", boom)
    out = tmp_path / "train-live"
    run_train(
        benchmark="officeqa",
        algo="xskill",
        harness="native_cc",
        model="deepseek-v4-flash",
        output_dir=out,
        limit=1,
        executor="live",
    )
    log = (out / "logs" / "step_1.log").read_text(encoding="utf-8")
    assert "fake train" not in log
    assert "noforce-canary" in log
    assert "Chat rewrite" in log
    prov = json.loads((out / "train_provenance.json").read_text(encoding="utf-8"))
    assert prov["merge_val_into_train"] is True
    assert prov["intermediate_checkpoints"][0]["action"] == "merged_to_main"
    assert prov["algo_components"]["canary_router"] == "hard_canary_v2"
    # --limit 缩水了 epoch 与 worker，provenance 要写真吃到的值加官方值。
    assert prov["hyperparameters"]["source"] == "smoke_shortened"
    assert prov["hyperparameters"]["official"]["workers"] == 3
    assert prov["algo_components"]["simulated_user_workers"] == 1


def test_fake_train_log_still_used_when_executor_fake(tmp_path):
    from xskill.bench.pipeline import run_train

    out = tmp_path / "train-fake"
    run_train(
        benchmark="officeqa",
        algo="xskill",
        harness="native_cc",
        model="deepseek-v4-flash",
        output_dir=out,
        executor="fake",
    )
    text = (out / "logs" / "step_1.log").read_text(encoding="utf-8")
    assert text.startswith("fake train")


def test_skillopt_official_train_runs_vendor_script_through_litellm(tmp_path):
    """SkillOpt 训练必须是官方 scripts/train.py，超参来自 vendor config。"""
    from xskill.bench.skillopt_train import run_official_train

    seen: dict[str, object] = {}

    def fake_exec(argv, *, cwd, env, log_path, stream, timeout_s):
        del stream, timeout_s
        seen["argv"] = list(argv)
        seen["cwd"] = str(cwd)
        seen["env"] = dict(env)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("官方训练日志\n", encoding="utf-8")
        # 造出官方 run 目录：history.json、skills/、best_skill.md。
        run_dir = tmp_path / "out" / "skillopt_run"
        (run_dir / "skills").mkdir(parents=True, exist_ok=True)
        (run_dir / "steps" / "step_0001").mkdir(parents=True, exist_ok=True)
        (run_dir / "skills" / "skill_v0001.md").write_text("# v1\n", encoding="utf-8")
        (run_dir / "skills" / "skill_v0002.md").write_text("# v2\n", encoding="utf-8")
        (run_dir / "steps" / "step_0002").mkdir(parents=True, exist_ok=True)
        (run_dir / "history.json").write_text(
            json.dumps(
                [
                    # 跳过改写的步没有 selection_hard，只有 current_score。
                    {"step": 1, "epoch": 1, "action": "skip_no_patches", "current_score": 0.5, "best_score": 0.5},
                    {"step": 2, "epoch": 2, "action": "accept_new_best", "selection_hard": 0.8, "best_score": 0.8},
                ]
            ),
            encoding="utf-8",
        )
        (run_dir / "best_skill.md").write_text("# best skill\n", encoding="utf-8")
        return 0

    result = run_official_train(
        benchmark="officeqa",
        model="deepseek-v4-flash",
        run_id="t-official",
        skill_out=tmp_path / "skill",
        output_dir=tmp_path / "out",
        proxy_url="http://127.0.0.1:4000",
        proxy_key="sk-run-key",
        runner=fake_exec,
    )
    argv = seen["argv"]
    assert argv[1] == "scripts/train.py"
    assert argv[argv.index("--config") + 1] == "configs/officeqa/default.yaml"
    assert argv[argv.index("--target_backend") + 1] == "claude_code_exec"
    assert argv[argv.index("--optimizer_backend") + 1] == "openai_chat"
    # optimizer 与 target 的 endpoint 都必须是代理。
    assert argv[argv.index("--optimizer_azure_openai_endpoint") + 1] == "http://127.0.0.1:4000"
    assert argv[argv.index("--target_azure_openai_endpoint") + 1] == "http://127.0.0.1:4000"
    # key 只走环境变量：命令行整台机器 ps 都看得见。
    assert "sk-run-key" not in " ".join(argv)
    assert not any(part.endswith("_api_key") for part in argv)
    assert "--cfg-options" in argv
    # 没传种子就走官方 env.skill_init，不许伪造一个。
    assert "--skill_init" not in argv
    options = argv[argv.index("--cfg-options") + 1 :]
    # 训练超参留给官方 config，cfg-options 只放路径与 harness。
    assert not any(item.startswith(("train.", "gradient.")) for item in options)
    assert any(item.startswith("env.data_dirs=") for item in options)
    # 解题的 Claude 子进程继承这些变量，所以做题也走代理。
    env = seen["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-run-key"

    assert result.runtime == "host"
    assert (tmp_path / "skill" / "SKILL.md").read_text(encoding="utf-8") == "# best skill\n"
    # 每个官方 step 一条 checkpoint，动作与 val 分数照抄 history。
    assert [entry["round"] for entry in result.checkpoints] == [1, 2]
    assert [entry["action"] for entry in result.checkpoints] == [
        "skip_no_patches",
        "accept_new_best",
    ]
    assert result.checkpoints[0]["val_score"] == 0.5
    assert result.checkpoints[1]["val_score"] == 0.8
    assert result.action == "accept_new_best"
    assert result.val_score == 0.8
    hyper = result.hyperparameters
    assert (hyper["num_epochs"], hyper["batch_size"], hyper["minibatch_size"]) == (4, 40, 8)
    assert hyper["skill_update_mode"] == "patch"
    assert hyper["use_gate"] is True
    assert hyper["skill_init"] == "vendor_default"


def test_skillopt_official_train_starts_from_the_seed_we_recorded(tmp_path):
    """run_config 记了种子哈希，官方训练就得真从那份种子起步。"""
    from xskill.bench.skillopt_train import build_argv

    seed = tmp_path / "SKILL.md"
    seed.write_text("# seed\n", encoding="utf-8")
    argv = build_argv(
        benchmark="officeqa",
        model="deepseek-v4-flash",
        split_dir=tmp_path / "split",
        out_root=tmp_path / "run",
        workers=4,
        proxy_url="http://127.0.0.1:4000",
        skill_init=seed,
    )
    assert argv[argv.index("--skill_init") + 1] == str(seed)


def test_skillopt_official_train_spreadsheet_only_allowed_override(tmp_path):
    from xskill.bench.skillopt_train import build_argv, cfg_options

    options = cfg_options("spreadsheet")
    assert options == ["env.mode=single"]
    argv = build_argv(
        benchmark="spreadsheet",
        model="deepseek-v4-flash",
        split_dir=tmp_path / "split",
        out_root=tmp_path / "run",
        workers=4,
        proxy_url="http://127.0.0.1:4000",
    )
    assert "--data_root" in argv
    assert argv[argv.index("--config") + 1] == "configs/spreadsheetbench/default.yaml"


def test_skillopt_train_secrets_file_is_gone_when_the_run_ends(tmp_path):
    """key 落过盘就得收走：产物目录会被打包带走。"""
    from xskill.bench.skillopt_train import run_official_train

    out = tmp_path / "out"
    secrets = out / ".train_secrets.env"

    def fake_exec(argv, *, cwd, env, log_path, stream, timeout_s):
        del argv, cwd, env, stream, timeout_s
        assert secrets.is_file(), "跑的时候必须在，容器要读它"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("log\n", encoding="utf-8")
        run_dir = out / "skillopt_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "best_skill.md").write_text("# best\n", encoding="utf-8")
        return 0

    split = tmp_path / "split"
    for name in ("train", "val", "test"):
        folder = split / name
        folder.mkdir(parents=True)
        (folder / "items.json").write_text("[]\n", encoding="utf-8")
    run_official_train(
        benchmark="alfworld",
        model="deepseek-v4-flash",
        run_id="t-alf-secrets",
        skill_out=tmp_path / "skill",
        output_dir=out,
        proxy_url="http://127.0.0.1:4000",
        proxy_key="sk-run-key",
        split_dir=split,
        runner=fake_exec,
    )
    assert not secrets.exists()


def test_skillopt_official_train_alfworld_runs_in_image(tmp_path):
    from xskill.bench.skillopt_train import SKILLOPT_TRAIN_IMAGES, plan_run

    plan = plan_run(
        benchmark="alfworld",
        model="deepseek-v4-flash",
        run_id="t-alf",
        output_dir=tmp_path / "out",
        workers=4,
        proxy_url="http://127.0.0.1:4000",
        proxy_key="sk-run-key",
    )
    assert plan.kind == "image"
    assert plan.argv[:3] == ["docker", "run", "--rm"]
    assert SKILLOPT_TRAIN_IMAGES["alfworld"] in plan.argv
    assert "host.docker.internal:host-gateway" in plan.argv
    # key 走 --env-file，不上 docker -e，也不进容器里那段脚本。
    assert "sk-run-key" not in " ".join(plan.argv)
    env_file = Path(plan.argv[plan.argv.index("--env-file") + 1])
    assert "TARGET_AZURE_OPENAI_API_KEY=sk-run-key" in env_file.read_text(encoding="utf-8")
    if os.name != "nt":
        assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    script = plan.argv[-1]
    # 覆盖 entrypoint 直接跑官方 train.py，绕开打榜镜像里 batch_size=3 那套缩水值。
    assert "scripts/train.py" in script
    assert "batch_size=3" not in script
    assert "host.docker.internal:4000" in script
