"""第九步：live executor 接线。不改官方 split，不在 pytest 里打真模型。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from xskill.bench.constants import SKILLOPT_TOOLS, XSKILL_TOOLS
from xskill.bench.dataset import SMOKE_IDS, officeqa_gold, officeqa_split_root
from xskill.bench.executor import make_executor
from xskill.bench.live import (
    _as_text,
    _tools_csv,
    alfworld_skill_mode,
    build_alfworld_docker_cmd,
    prepare_alfworld_workspace,
)
from xskill.bench.split import resolve_eval_uids

ROOT = Path(__file__).resolve().parents[1]
UNTOUCHED = {
    "scripts/bench/README.md": "2f3fb4030e0a3eb32be655dd5f994f04db47a9b0041300942a885f9383b8387b",
    "scripts/bench/evaluate.py": "7fa0916005ec820cbeedc1ba6d7c8b5211c56990f8b14c4d3db1840feb14600d",
    "scripts/bench/run_baseline.py": "3f28cac87db835ccbc13202b5fc962fa5fd5e530f0907a89f265fbf4e2b730ba",
    "benchmarks/validate.py": "77fa03d47e62621ac9fa9f01b6410d18645142109383c5d8c40cac97c1155cb4",
}


def _sha(rel: str) -> str:
    data = (ROOT / rel).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def test_live_does_not_touch_official_manifests_or_splitter():
    for rel, digest in UNTOUCHED.items():
        assert _sha(rel) == digest, rel
    for name in ("officeqa", "spreadsheet", "alfworld"):
        path = ROOT / "benchmarks" / name / "manifests" / f"{name}_skillopt_id_split.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["train"][0] == SMOKE_IDS[name]["train"]
        assert doc["val"][0] == SMOKE_IDS[name]["val"]
        assert doc["test"][0] == SMOKE_IDS[name]["test"]


def test_limit_one_keeps_official_first_id():
    for bench, split in (
        ("officeqa", "test"),
        ("spreadsheet", "train"),
        ("alfworld", "val"),
    ):
        _path, uids = resolve_eval_uids(bench, split, limit=1)
        assert uids == [SMOKE_IDS[bench][split]]


def test_make_executor_live_and_fake_still_available():
    live = make_executor("live", benchmark="officeqa", algo="xskill", run_id="t")
    fake = make_executor("fake", benchmark="officeqa")
    assert type(live).__name__ == "LiveExecutor"
    assert type(fake).__name__ == "FakeExecutor"
    assert live.tools == "Read,Bash,Skill"


def test_live_tools_locked():
    assert _tools_csv("xskill", XSKILL_TOOLS) == "Read,Bash,Skill"
    assert _tools_csv("skillopt", SKILLOPT_TOOLS) == "Read,Bash"
    with pytest.raises(RuntimeError, match="Write"):
        _tools_csv("xskill", ["Read", "Write", "Edit", "Bash", "Skill"])
    with pytest.raises(RuntimeError, match="Skill"):
        _tools_csv("skillopt", ["Read", "Bash", "Skill"])


def test_as_text_accepts_bytes_from_timeout_expired():
    assert _as_text(None) == ""
    assert _as_text("ok") == "ok"
    assert _as_text(b"timeout-bytes") == "timeout-bytes"
    assert (_as_text(b"out") + "\n" + _as_text(None) + "\ntimeout") == "out\n\ntimeout"


def test_alfworld_skillopt_gets_skill_file_and_exec_tools(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# skillopt alfworld\nlook then act\n", encoding="utf-8")
    work = tmp_path / "ws"
    work.mkdir()
    chome = work / "chome"
    chome.mkdir()
    dest = prepare_alfworld_workspace("skillopt", work, skill)
    assert dest == work / "SKILL.md"
    assert dest.read_text(encoding="utf-8") == skill.read_text(encoding="utf-8")
    assert prepare_alfworld_workspace("xskill", work, skill) is None
    assert alfworld_skill_mode("skillopt") == "exec"
    assert alfworld_skill_mode("xskill") == "native"

    so_cmd = build_alfworld_docker_cmd(
        uid="test:0000",
        work=work,
        chome=chome,
        model="deepseek-v4-flash",
        timeout_s=600,
        max_tool_turns=16,
        max_steps=50,
        tools="Read,Bash",
        skill_mode="exec",
        litellm_url="http://127.0.0.1:4000",
        api_key="dummy",
        run_id="t",
    )
    assert "--tools" in so_cmd
    assert so_cmd[so_cmd.index("--tools") + 1] == "Read,Bash"
    assert so_cmd[so_cmd.index("--skill-mode") + 1] == "exec"
    assert any(part.endswith("alfworld_eval_rollout.py:/app/alfworld_rollout.py:ro") for part in so_cmd)
    assert "Skill" not in so_cmd[so_cmd.index("--tools") + 1]

    xs_cmd = build_alfworld_docker_cmd(
        uid="test:0000",
        work=work,
        chome=chome,
        model="deepseek-v4-flash",
        timeout_s=600,
        max_tool_turns=16,
        max_steps=50,
        tools="Read,Bash,Skill",
        skill_mode="native",
        litellm_url="http://127.0.0.1:4000",
        api_key="dummy",
        run_id="t",
    )
    assert xs_cmd[xs_cmd.index("--tools") + 1] == "Read,Bash,Skill"
    assert xs_cmd[xs_cmd.index("--skill-mode") + 1] == "native"


def test_alfworld_rollout_prompts_and_rejects_exec_with_skill_tool():
    from xskill.bench.alfworld_eval_rollout import build_turn0_prompt

    exec_prompt = build_turn0_prompt("task", "obs", skill_mode="exec")
    native_prompt = build_turn0_prompt("task", "obs", skill_mode="native")
    assert "Read `SKILL.md`" in exec_prompt
    assert "Skill tool" not in exec_prompt
    assert "Skill tool" in native_prompt

    import subprocess
    import sys

    script = ROOT / "src/xskill/bench/alfworld_eval_rollout.py"
    bad = subprocess.run(
        [
            sys.executable,
            str(script),
            "--item-id",
            "test:0000",
            "--data-root",
            "/tmp",
            "--chome",
            "/tmp",
            "--tools",
            "Read,Bash,Skill",
            "--skill-mode",
            "exec",
        ],
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0
    assert "cannot include the Skill tool" in (bad.stderr + bad.stdout)


def test_officeqa_official_split_not_q1():
    root = officeqa_split_root()
    if not (root / "test" / "items.json").is_file():
        pytest.skip(f"official OfficeQA split missing: {root}")
    assert root.name == "officeqa_split"
    test_items = json.loads((root / "test" / "items.json").read_text(encoding="utf-8"))
    assert len(test_items) == 172
    first = test_items[0]
    assert first["id"] == "UID0003"
    assert officeqa_gold(first) == "44,463"
