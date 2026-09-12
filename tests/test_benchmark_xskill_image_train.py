"""xskill train 必须走官方训练镜像，不能走整篇 Chat 改写。"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from xskill.bench.hparams import xskill_official
from xskill.bench.split import resolve_train_uids
from xskill.bench.xskill_image import (
    XSKILL_TRAIN_IMAGES,
    XSKILL_TRAIN_IMAGES_LITELLM,
    build_docker_cmd,
    build_train_env,
    host_split_root,
    train_image,
    write_limit_overlay,
)

ROOT = Path(__file__).resolve().parents[1]
UNTOUCHED = {
    "scripts/bench/README.md": "2f3fb4030e0a3eb32be655dd5f994f04db47a9b0041300942a885f9383b8387b",
    "scripts/bench/evaluate.py": "7fa0916005ec820cbeedc1ba6d7c8b5211c56990f8b14c4d3db1840feb14600d",
    "scripts/bench/run_baseline.py": "3f28cac87db835ccbc13202b5fc962fa5fd5e530f0907a89f265fbf4e2b730ba",
    "benchmarks/validate.py": "77fa03d47e62621ac9fa9f01b6410d18645142109383c5d8c40cac97c1155cb4",
    "benchmarks/officeqa/manifests/officeqa_skillopt_id_split.json": None,
    "benchmarks/spreadsheet/manifests/spreadsheet_skillopt_id_split.json": None,
    "benchmarks/alfworld/manifests/alfworld_skillopt_id_split.json": None,
}


def _sha(rel: str) -> str:
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def test_official_images_and_isolation():
    for rel, digest in UNTOUCHED.items():
        path = ROOT / rel
        assert path.is_file(), rel
        if digest:
            assert _sha(rel) == digest, rel
    assert "hard-canary-noforce-fix-20260909" in XSKILL_TRAIN_IMAGES["officeqa"]
    assert "noforce-mergeval-20260909" in XSKILL_TRAIN_IMAGES["spreadsheet"]
    assert "noforce-mergeval-fix-20260909" in XSKILL_TRAIN_IMAGES["alfworld"]
    # LiteLLM 副本必须是新 tag，不许覆盖官方那三个。
    for bench, official in XSKILL_TRAIN_IMAGES.items():
        copy = XSKILL_TRAIN_IMAGES_LITELLM[bench]
        assert copy != official
        assert copy.endswith("-litellm-20260909")
        assert copy.rsplit(":", 1)[0] == official.rsplit(":", 1)[0]
    text = (ROOT / "src/xskill/bench/xskill_image.py").read_text(encoding="utf-8")
    assert "chat.completions" not in text
    assert "Revise this agent skill" not in text


def test_pipeline_routes_xskill_to_image_and_skillopt_to_official_train():
    pipe = (ROOT / "src/xskill/bench/pipeline.py").read_text(encoding="utf-8")
    assert "run_xskill_image_train" in pipe
    assert "if algo == \"xskill\":" in pipe
    assert "run_official_train" in pipe
    assert "run_live_train" not in pipe
    loop = (ROOT / "src/xskill/bench/train_loop.py").read_text(encoding="utf-8")
    # 整篇改写那套已经拆掉，只剩 skill 文件读写。
    assert "chat_rewrite" not in loop
    assert "Revise this agent skill" not in loop
    assert "skillopt_train.py" in loop


def test_train_env_uses_official_xskill_hparams_not_cli_defaults():
    env = build_train_env(benchmark="officeqa", model="deepseek-v4-flash", limit=0)
    official = xskill_official("officeqa")
    assert env["XSKILL_MERGE_VAL_INTO_TRAIN"] == "true"
    # 训练超参必须是 xskill 官方值：workers 3 不是命令行默认的 4。
    assert env["XSKILL_WORKERS"] == str(official["workers"]) == "3"
    assert env["XSKILL_EPOCHS"] == str(official["epochs"]) == "4"
    assert env["FINAL_SETTLE"] == str(official["canary_settle_s"]) == "600"
    assert env["XSKILL_CANARY_SETTLE"] == "600"
    assert env["ITEM_TIMEOUT"] == str(official["item_timeout_s"])
    assert env["MAX_TOOL_TURNS"] == str(official["max_tool_turns"])
    for bench in ("spreadsheet", "alfworld"):
        got = build_train_env(benchmark=bench, model="deepseek-v4-flash", limit=0)
        want = xskill_official(bench)
        assert got["XSKILL_WORKERS"] == str(want["workers"])
        assert got["ITEM_TIMEOUT"] == str(want["item_timeout_s"])


def test_docker_cmd_reaches_litellm_and_does_not_embed_upstream_keys():
    env = build_train_env(
        benchmark="officeqa",
        model="deepseek-v4-flash",
        limit=0,
        proxy_url="http://127.0.0.1:4000",
        proxy_key="sk-litellm-run",
    )
    assert env["XSKILL_LLM_BASE_URL"] == "http://host.docker.internal:4000"
    assert env["XSKILL_EMBEDDING_BASE_URL"] == "http://host.docker.internal:4000"
    # Claude Code 直接打 /v1/messages，不能带 /anthropic 后缀。
    assert env["XSKILL_ANTHROPIC_BASE_URL"] == "http://host.docker.internal:4000"
    assert env["DEEPSEEK_API_KEY"] == "sk-litellm-run"
    assert env["DASHSCOPE_API_KEY"] == "sk-litellm-run"
    cmd = build_docker_cmd(
        image=train_image("officeqa"),
        name="xskill-bench-train-t",
        env=env,
        skill_mount=Path("/tmp/skill"),
        out_mount=Path("/tmp/out"),
    )
    assert cmd[0:3] == ["docker", "run", "-d"]
    assert "--network" not in cmd
    assert "host.docker.internal:host-gateway" in cmd
    assert cmd[-1] == XSKILL_TRAIN_IMAGES_LITELLM["officeqa"]
    # 走代理时不再透传上游 key，只透传代理 key。
    assert not any(item == "DEEPSEEK_API_KEY" for item in cmd)


def test_docker_cmd_without_proxy_keeps_official_image_and_key_passthrough():
    env = build_train_env(benchmark="officeqa", model="deepseek-v4-flash", limit=0)
    cmd = build_docker_cmd(
        image=train_image("officeqa", via_litellm=False),
        name="xskill-bench-train-t",
        env=env,
        skill_mount=Path("/tmp/skill"),
        out_mount=Path("/tmp/out"),
    )
    assert cmd[-1] == XSKILL_TRAIN_IMAGES["officeqa"]
    assert "DEEPSEEK_API_KEY" in cmd
    assert "DASHSCOPE_API_KEY" in cmd
    assert not any(item.startswith("DEEPSEEK_API_KEY=") for item in cmd)


def test_limit_run_shortens_loop_but_keeps_official_shape():
    smoke = build_train_env(benchmark="officeqa", model="deepseek-v4-flash", limit=1)
    assert smoke["XSKILL_EPOCHS"] == "1"
    assert smoke["XSKILL_TRAIN_LIMIT"] == "2"
    assert smoke["XSKILL_WORKERS"] == "1"
    assert smoke["ITEM_TIMEOUT"] == "300"


def test_limit_overlay_does_not_touch_official_split(tmp_path):
    src = host_split_root("officeqa")
    before = hashlib.sha256((src / "train" / "items.json").read_bytes()).hexdigest()
    dest = tmp_path / "overlay"
    write_limit_overlay(src, dest, 1)
    after = hashlib.sha256((src / "train" / "items.json").read_bytes()).hexdigest()
    assert before == after
    train = json.loads((dest / "train" / "items.json").read_text(encoding="utf-8"))
    val = json.loads((dest / "val" / "items.json").read_text(encoding="utf-8"))
    assert [item.get("id") for item in train] == ["UID0002"]
    assert [item.get("id") for item in val] == ["UID0001"]
    with pytest.raises(RuntimeError, match="official split"):
        write_limit_overlay(src, src, 1)


def test_xskill_train_uids_still_merge_val():
    _path, uids, split_name, merge_val = resolve_train_uids("officeqa", "xskill")
    assert merge_val is True
    assert split_name == "train_plus_val"
    assert len(uids) == 74
    _path, skillopt_uids, skillopt_split, skillopt_merge = resolve_train_uids("officeqa", "skillopt")
    assert skillopt_merge is False
    assert skillopt_split == "train"
    assert len(skillopt_uids) == 50


def test_xskill_limit_keeps_train_and_val_slices():
    _path, uids, split_name, merge_val = resolve_train_uids(
        "officeqa", "xskill", limit=1
    )
    assert merge_val is True
    assert split_name == "train_plus_val"
    assert uids == ["UID0002", "UID0001"]
    _path, six, _, _ = resolve_train_uids("officeqa", "xskill", limit=6)
    assert len(six) == 12
    assert six[:6] == [
        "UID0002",
        "UID0007",
        "UID0014",
        "UID0017",
        "UID0018",
        "UID0019",
    ]
    assert six[6:] == [
        "UID0001",
        "UID0027",
        "UID0039",
        "UID0041",
        "UID0052",
        "UID0070",
    ]
    _, skillopt_uids, _, skillopt_merge = resolve_train_uids(
        "officeqa", "skillopt", limit=6
    )
    assert skillopt_merge is False
    assert skillopt_uids == [
        "UID0002",
        "UID0007",
        "UID0014",
        "UID0017",
        "UID0018",
        "UID0019",
    ]


def test_image_runner_mocked_docker_harvests(tmp_path, monkeypatch):
    from xskill.bench.xskill_image import run_xskill_image_train

    monkeypatch.setattr(
        "xskill.bench.xskill_image.load_aikey",
        lambda: {"DEEPSEEK_API_KEY": "sk-test", "DASHSCOPE_API_KEY": "sk-test"},
    )
    calls: list[list[str]] = []

    def docker(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="cidabc\n", stderr="")

    def wait(image_skill, image_out, deadline, name, env):
        del image_out, deadline, name, env
        pkg = Path(image_skill) / "skills" / "officeqa-document-qa"
        pkg.mkdir(parents=True)
        (pkg / "SKILL.md").write_text(
            "---\nname: officeqa-document-qa\n---\nshipped from canary\n",
            encoding="utf-8",
        )
        (Path(image_skill) / "DONE").write_text("", encoding="utf-8")
        (Path(image_skill) / "ALGO").write_text("xskill\n", encoding="utf-8")
        return "DONE"

    out = tmp_path / "train"
    result = run_xskill_image_train(
        benchmark="officeqa",
        model="deepseek-v4-flash",
        run_id="smoke-img",
        uids=["UID0002"],
        skill_out=out / "skill",
        output_dir=out,
        limit=1,
        proxy_url="http://127.0.0.1:4000",
        proxy_key="sk-litellm-run",
        docker_fn=docker,
        wait_fn=wait,
    )
    assert result.n_skills == 1
    assert result.action == "merged_to_main"
    assert "noforce" in result.image
    assert result.image == XSKILL_TRAIN_IMAGES_LITELLM["officeqa"]
    assert (out / "skill" / "officeqa-document-qa" / "SKILL.md").is_file()
    assert "canary" in (out / "skill" / "officeqa-document-qa" / "SKILL.md").read_text(encoding="utf-8")
    run_cmds = [c for c in calls if c[:2] == ["docker", "run"]]
    assert run_cmds
    joined = " ".join(run_cmds[0])
    assert XSKILL_TRAIN_IMAGES_LITELLM["officeqa"].split(":")[-1] in joined
    assert "host.docker.internal:host-gateway" in joined
    # 上游 key 不下发，容器只拿到代理的 key。
    assert "sk-test" not in joined
    overlay = json.loads((out / "_split_overlay" / "train" / "items.json").read_text(encoding="utf-8"))
    overlay_val = json.loads((out / "_split_overlay" / "val" / "items.json").read_text(encoding="utf-8"))
    assert overlay[0]["id"] == "UID0002"
    assert overlay_val[0]["id"] == "UID0001"
    official = json.loads((host_split_root("officeqa") / "train" / "items.json").read_text(encoding="utf-8"))
    assert len(official) == 50


def test_local_train_images_exist():
    for table in (XSKILL_TRAIN_IMAGES, XSKILL_TRAIN_IMAGES_LITELLM):
        for bench, image in table.items():
            proc = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"missing {bench} image {image}"


def test_skillopt_live_train_goes_official_not_xskill_image(tmp_path, monkeypatch):
    from xskill.bench.pipeline import run_train
    from xskill.bench.skillopt_train import OfficialTrainResult
    from xskill.bench.skill_io import sha256_file

    called = {"official": 0, "image": 0}

    def fake_official(**kwargs):
        called["official"] += 1
        skill_out = kwargs["skill_out"]
        sha = sha256_file(skill_out / "SKILL.md")
        run_dir = kwargs["output_dir"] / "skillopt_run"
        run_dir.mkdir(parents=True)
        log = kwargs["output_dir"] / "logs" / "skillopt_train.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("official train\n", encoding="utf-8")
        return OfficialTrainResult(
            frozen_sha=sha,
            checkpoints=[
                {
                    "step": 1,
                    "round": 1,
                    "skill_sha256": sha,
                    "checkpoint_dir": str(run_dir),
                    "action": "accept_new_best",
                    "log_file": str(log),
                    "val_score": 1.0,
                }
            ],
            log_text="official train\n",
            action="accept_new_best",
            val_score=1.0,
            hyperparameters={"num_epochs": 4, "batch_size": 40},
            runtime="host",
            n_steps=1,
            returncode=0,
            run_dir=run_dir,
        )

    def fake_image(**kwargs):
        called["image"] += 1
        raise AssertionError("skillopt must not launch the xskill image")

    monkeypatch.setattr("xskill.bench.pipeline.run_official_train", fake_official)
    monkeypatch.setattr("xskill.bench.pipeline.run_xskill_image_train", fake_image)
    skill = tmp_path / "SKILL.md"
    skill.write_text("# skillopt\n", encoding="utf-8")
    out = tmp_path / "so"
    run_train(
        benchmark="officeqa",
        algo="skillopt",
        harness="claude_code_exec",
        model="deepseek-v4-flash",
        output_dir=out,
        skill_file=skill,
        limit=1,
        executor="live",
    )
    assert called == {"official": 1, "image": 0}
    prov = json.loads((out / "train_provenance.json").read_text(encoding="utf-8"))
    assert prov["hyperparameters"]["num_epochs"] == 4
    assert prov["intermediate_checkpoints"][0]["action"] == "accept_new_best"
