"""tests/e2e/test_smoke.py — 3-agent smoke E2E（P4）

参数化 agent ∈ {claude_code, codex, opencode}：
  1. 在 tmp_path 模拟该 agent 的 sessions 目录布局
  2. 丢一份入仓 fixture（codex/opencode）或内联 fixture（claude_code）
  3. 跑对应 ingester（``JsonlIngester`` / ``SqliteIngester``）—— 不起 daemon、
     不调 LLM，只验"从源磁盘读到 → 桥成 traj_*.md 落 watch dir"链路
  4. 造一个最小 skill 目录 → 调 ``install_to_<agent>`` 把它装到该 agent 的
     discovery 路径
  5. 断言：
     - traj_*.md / .json 在 watch dir 出现
     - traj 元数据含 session_id（schema 正确）
     - skill 装到正确的 agent 路径（CC → ``.claude/skills/``；
       Codex/OpenCode → 共享的 ``.agents/skills/``）

设计原则：
- **不调外网 / 不起子进程**：直接 instantiate ingester、调 installer
- **tmp_path 隔离**：所有 home / source / watch dir 都在 tmp_path 下；
  不读不写用户真实 ``~/.xskill``、``~/.claude``、``~/.codex``
- **单测时间 < 10s**：纯文件 IO + 一次 ingester scan + 一次 installer 调用
- **schema 真实性**：codex / opencode fixture 来自 P2/P3 入仓的真实 schema
  样本；claude_code fixture 是最小 CC JSONL（user + assistant 各一条），
  schema 与 ``_adapt_claude_code_jsonl`` 期望对齐
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from xskill.ecosystems import (
    CC_SPEC,
    CODEX_SPEC,
    JsonlIngester,
    OPENCODE_SPEC,
    SqliteIngester,
    install_to_claude_code,
    install_to_codex,
    install_to_opencode,
)


# ──────────────────────────────────────────────────────────────────
# Fixtures — 三 agent 的 sessions seed + 路径 helpers
# ──────────────────────────────────────────────────────────────────

# claude_code 没有入仓 fixture（CC 用户机器上每天都有真实数据；这里造
# 最小可消费样本，schema 与 ``_adapt_claude_code_jsonl`` 期望对齐）。
_CC_SESSION_ID = "cc-smoke-session-001"
_CC_FIXTURE = "\n".join([
    json.dumps({
        "type": "user",
        "sessionId": _CC_SESSION_ID,
        "cwd": "/tmp/cc-smoke-workspace",
        "timestamp": "2026-05-13T10:00:00.000Z",
        "message": {"role": "user", "content": "list .py files please"},
    }),
    json.dumps({
        "type": "assistant",
        "sessionId": _CC_SESSION_ID,
        "cwd": "/tmp/cc-smoke-workspace",
        "timestamp": "2026-05-13T10:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Here are the .py files: a.py, b.py."},
            ],
        },
    }),
]) + "\n"

_CODEX_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "codex" / "sample_rollout.jsonl"
)
_CODEX_SESSION_ID = "11111111-2222-3333-4444-555555555555"

_OPENCODE_FIXTURE_DB = (
    Path(__file__).resolve().parent.parent
    / "fixtures" / "opencode" / "sample.db"
)
_OPENCODE_SESSION_ID = "ses_bbbbbbbbbbbb"


# ──────────────────────────────────────────────────────────────────
# Per-agent harness
# ──────────────────────────────────────────────────────────────────


@dataclass
class SmokeHarness:
    """打包"在 tmp_path 下伪造该 agent → 跑 ingester → 跑 installer"四件套。

    每个 agent 一份实例（fixture 化），smoke 测试参数化时按 agent 名字取。
    """

    name: str
    seed_sessions: Callable[[Path], None]           # (home) → 在 home 下铺 fixture
    run_ingester: Callable[[Path, Path], list[dict]]  # (home, traj_dir) → results
    install_to: Callable[[Path, Path], Path]        # (skill_path, home) → dest SKILL.md
    expected_session_id: str
    expected_install_subpath: Path                  # 相对 home 的 skill 装入路径


def _seed_cc(home: Path) -> None:
    """在 ``<home>/.claude/projects/<cwd-hash>/<sid>.jsonl`` 写 CC 风格 fixture。"""
    project_dir = home / ".claude" / "projects" / "abcd1234"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / f"{_CC_SESSION_ID}.jsonl").write_text(_CC_FIXTURE, encoding="utf-8")


def _seed_codex(home: Path) -> None:
    """把入仓 codex fixture 复制到 ``<home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``。"""
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "15"
    sessions.mkdir(parents=True, exist_ok=True)
    dest = sessions / f"rollout-2026-01-15T10-00-00-{_CODEX_SESSION_ID}.jsonl"
    dest.write_bytes(_CODEX_FIXTURE_PATH.read_bytes())


def _seed_opencode(home: Path) -> None:
    """把入仓 opencode fixture db 复制到 ``<home>/.local/share/opencode/opencode.db``。"""
    target_dir = home / ".local" / "share" / "opencode"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "opencode.db").write_bytes(_OPENCODE_FIXTURE_DB.read_bytes())


def _run_cc_ingester(home: Path, traj_dir: Path) -> list[dict]:
    return JsonlIngester(CC_SPEC).scan_and_bridge(traj_dir, home_root=home)


def _run_codex_ingester(home: Path, traj_dir: Path) -> list[dict]:
    return JsonlIngester(CODEX_SPEC).scan_and_bridge(traj_dir, home_root=home)


def _run_opencode_ingester(home: Path, traj_dir: Path) -> list[dict]:
    return SqliteIngester(traj_dir, home_root=home, spec=OPENCODE_SPEC).run_once()


def _build_mock_skill(skill_root: Path, name: str = "smoke-skill") -> Path:
    """造一个最小可装 skill：``<skill_root>/<name>/SKILL.md``（带 frontmatter）。"""
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: smoke-test skill for P4 E2E\n"
        "version: 1\n"
        "---\n\n"
        "# Body\n\nThis is a smoke-test skill.\n",
        encoding="utf-8",
    )
    return skill_path


_HARNESSES: dict[str, SmokeHarness] = {
    "claude_code": SmokeHarness(
        name="claude_code",
        seed_sessions=_seed_cc,
        run_ingester=_run_cc_ingester,
        install_to=lambda skill, home: install_to_claude_code(skill, target_root=home),
        expected_session_id=_CC_SESSION_ID,
        expected_install_subpath=Path(".claude") / "skills",
    ),
    "codex": SmokeHarness(
        name="codex",
        seed_sessions=_seed_codex,
        run_ingester=_run_codex_ingester,
        install_to=lambda skill, home: install_to_codex(skill, target_root=home),
        expected_session_id=_CODEX_SESSION_ID,
        expected_install_subpath=Path(".agents") / "skills",
    ),
    "opencode": SmokeHarness(
        name="opencode",
        seed_sessions=_seed_opencode,
        run_ingester=_run_opencode_ingester,
        install_to=lambda skill, home: install_to_opencode(skill, target_root=home),
        expected_session_id=_OPENCODE_SESSION_ID,
        expected_install_subpath=Path(".agents") / "skills",
    ),
}


# ──────────────────────────────────────────────────────────────────
# Smoke parametrized over agent
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("agent_name", ["claude_code", "codex", "opencode"])
def test_smoke_e2e(agent_name: str, tmp_path: Path) -> None:
    """3 agent × 单 OS smoke E2E：sessions seed → ingest → traj 落盘 → install。

    四段断言：
      1. ingester 桥出 ≥1 条 traj（session_id 与 fixture 对得上）
      2. ``traj_*.md`` 文件实际写到 watch dir
      3. ``traj_*.json`` metadata 存在且含 session_id（schema sanity check）
      4. install_to_<agent> 把 skill 装到正确路径（.claude/skills 或
         .agents/skills；CC 一处、Codex+OpenCode 共享一处）
    """
    harness = _HARNESSES[agent_name]
    home = tmp_path / "home"
    home.mkdir(parents=True)
    traj_dir = tmp_path / "watch"

    # 1) 在 home 下铺该 agent 的 fixture session
    harness.seed_sessions(home)

    # 2) 跑 ingester 把 fixture 桥成 traj_*.md
    results = harness.run_ingester(home, traj_dir)
    assert len(results) >= 1, (
        f"[{agent_name}] ingester returned 0 trajectories — "
        f"fixture seeded under {home} but nothing bridged to {traj_dir}"
    )
    rec = results[0]
    assert rec.get("session_id") == harness.expected_session_id, (
        f"[{agent_name}] session_id mismatch: got {rec.get('session_id')!r}, "
        f"expected {harness.expected_session_id!r}"
    )

    # 3) traj_*.md / .json 文件在 watch dir 落盘
    traj_md = Path(rec["path"])
    assert traj_md.is_file(), (
        f"[{agent_name}] traj markdown not on disk: {traj_md}"
    )
    assert traj_md.parent == traj_dir, (
        f"[{agent_name}] traj written to wrong dir: {traj_md.parent} != {traj_dir}"
    )
    traj_json = traj_md.with_suffix(".json")
    assert traj_json.is_file(), (
        f"[{agent_name}] traj metadata json not on disk: {traj_json}"
    )
    meta = json.loads(traj_json.read_text(encoding="utf-8"))
    assert meta.get("session_id") == harness.expected_session_id, (
        f"[{agent_name}] traj metadata session_id mismatch in {traj_json}: "
        f"got {meta.get('session_id')!r}"
    )

    # 4) install_to_<agent>：mock skill 装到 agent discovery 目录
    skill_path = _build_mock_skill(tmp_path / "src")
    dest_md = harness.install_to(skill_path, home)

    expected_dest = home / harness.expected_install_subpath / "smoke-skill" / "SKILL.md"
    assert dest_md == expected_dest, (
        f"[{agent_name}] install dest mismatch:\n"
        f"  got      {dest_md}\n"
        f"  expected {expected_dest}"
    )
    assert dest_md.is_file(), (
        f"[{agent_name}] installed SKILL.md not on disk: {dest_md}"
    )
    # SKILL.md 内容应能从源仓回读（symlink 或 copy 都 OK）
    body = dest_md.read_text(encoding="utf-8")
    assert "name: smoke-skill" in body, (
        f"[{agent_name}] installed SKILL.md does not contain expected frontmatter; "
        f"first 200 chars: {body[:200]!r}"
    )


# ──────────────────────────────────────────────────────────────────
# 路径隔离回归 —— 防止 smoke 误污染用户 home
# ──────────────────────────────────────────────────────────────────


def test_smoke_does_not_touch_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity：跑一遍 codex harness，断言 ``Path.home()`` 不被任何调用方读到。

    我们对 ``Path.home`` 打 monkeypatch 让它返回一个**永远不存在**的路径；
    如果 ingester 或 installer 走默认 home 而不是显式 target_root，会因
    ``not is_dir()`` 立即返回空 results 或 ``FileNotFoundError``——任一信号
    都说明路径隔离不彻底。

    本测**只**对 codex harness 跑，因为它代表了"显式 home_root + target_root"
    的最完整调用面。CC / OpenCode 同样语义由参数化主测覆盖。
    """
    forbidden_home = tmp_path / "_NEVER_TOUCH_REAL_HOME_"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: forbidden_home))

    harness = _HARNESSES["codex"]
    home = tmp_path / "isolated_home"
    home.mkdir(parents=True)
    traj_dir = tmp_path / "watch"
    harness.seed_sessions(home)

    # 跑 ingester（显式 home_root=home，不用 Path.home() 默认）
    results = harness.run_ingester(home, traj_dir)
    assert len(results) >= 1

    # 装 skill（显式 target_root=home）
    skill_path = _build_mock_skill(tmp_path / "src")
    dest_md = harness.install_to(skill_path, home)

    # 验真路径都落在 home 下，绝不落到 forbidden_home
    assert str(dest_md).startswith(str(home)), (
        f"install leaked outside isolated home: {dest_md} not under {home}"
    )
    assert not forbidden_home.exists(), (
        f"smoke harness somehow created {forbidden_home} — "
        f"isolation broken; some code path called Path.home() and wrote to it"
    )
