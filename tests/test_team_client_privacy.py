"""
test_team_client_privacy.py -- 客户端本地上传排除规则（issue #244）
=================================================================

对应 issue 验收标准：
* 默认无规则时行为与当前一致（T1）
* 按项目排除后，该项目已有及以后产生的轨迹均不进入上传（T2，含子目录、
  符号链接、大小写、相对路径 / ~）
* 按轨迹 id 排除后，仅该轨迹不进入上传（T3）
* 排除判断完全在客户端、发生在读正文之前（T4：正文读取函数不被调用）
* 被排除轨迹不被标记为已上传，且不写任何状态（T5）
* 删除规则后可重新进入上传流程（T6）
* 混合放行 / 排除的一批（T7）
* Cursor / Trae 无 cwd 来源：项目规则不生效、轨迹规则生效（T8）
* 规则文件损坏时明确失败，而不是当作无规则放行（T9）
* CLI 五个子命令与 --json（T10）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from xskill.team.client import privacy as pv
from xskill.team.client.collector import TeamCollector

# ── helpers ────────────────────────────────────────────────────────

def _write_traj(bridge: Path, eco_dir: str, traj_id: str, *,
                cwd: str | None, source: str = "claude_code_jsonl",
                body: str = "# traj\n\nhello world\n") -> Path:
    d = bridge / eco_dir
    d.mkdir(parents=True, exist_ok=True)
    md = d / f"{traj_id}.md"
    md.write_text(body, encoding="utf-8")
    meta = {"source": source, "session_id": traj_id}
    if cwd is not None:
        meta["cwd"] = cwd
    md.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    return md


def _collector(home: Path) -> TeamCollector:
    """quiet_seconds=0 / min_change_interval=0：让闸门只剩隐私规则一道。
    时钟用真实时间：mtime 静默判断是 ``now - mtime < quiet``，固定的假时钟
    会让刚写的文件被当作「来自未来」而拦下。"""
    import time as _time
    xh = home / ".xskill"
    xh.mkdir(parents=True, exist_ok=True)
    return TeamCollector(
        cursor_path=xh / "clients" / "srv" / "cursor.json",
        quiet_seconds=0, min_change_interval=0,
        home_root=home, time_fn=lambda: _time.time() + 1.0,
        state_db_path=xh / "clients" / "srv" / "client_state.db",
        privacy_path=xh / "privacy.json",
    )


def _pending_ids(c: TeamCollector) -> list[str]:
    return sorted(p.traj_id for p in c.pending())


# ── T1 默认行为不变 ─────────────────────────────────────────────

def test_no_rules_uploads_everything(tmp_path):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    _write_traj(home / ".xskill", "codex_sessions", "traj_codex_b", cwd="/w/proj-b")
    c = _collector(home)
    assert not (home / ".xskill" / "privacy.json").exists()
    assert _pending_ids(c) == ["traj_cc_a", "traj_codex_b"]


# ── T2 按项目排除 ───────────────────────────────────────────────

def test_deny_project_excludes_dir_and_subdirs(tmp_path):
    home = tmp_path
    proj = tmp_path / "work" / "secret"
    (proj / "backend").mkdir(parents=True)
    _write_traj(home / ".xskill", "cc_sessions", "traj_root", cwd=str(proj))
    _write_traj(home / ".xskill", "cc_sessions", "traj_sub", cwd=str(proj / "backend"))
    _write_traj(home / ".xskill", "cc_sessions", "traj_other", cwd=str(tmp_path / "work" / "public"))
    _write_traj(home / ".xskill", "cc_sessions", "traj_prefix_trap",
                cwd=str(tmp_path / "work" / "secret-but-different"))  # 前缀相同的兄弟目录，不得误伤

    pol = pv.PrivacyPolicy()
    pol.deny_project(proj)
    pv.save_policy(pol, home / ".xskill" / "privacy.json")

    assert _pending_ids(_collector(home)) == ["traj_other", "traj_prefix_trap"]


def test_deny_project_normalizes_relative_tilde_and_symlink(tmp_path, monkeypatch):
    home = tmp_path
    real = tmp_path / "real-proj"
    real.mkdir()
    link = tmp_path / "link-proj"
    link.symlink_to(real, target_is_directory=True)
    _write_traj(home / ".xskill", "cc_sessions", "traj_via_link", cwd=str(link))
    _write_traj(home / ".xskill", "cc_sessions", "traj_via_real", cwd=str(real))

    # 用 ~ 与相对路径写规则，均应命中同一目录。
    # Windows 上 os.path.expanduser 读 USERPROFILE 而非 HOME（Python ≥3.8），
    # 两个都设，测试在三平台行为一致。
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    pol = pv.PrivacyPolicy()
    changed, norm = pol.deny_project("~/real-proj")
    assert changed
    changed2, norm2 = pol.deny_project("./link-proj")   # 解析符号链接后与上一条相同
    assert not changed2 and norm2 == norm
    pv.save_policy(pol, home / ".xskill" / "privacy.json")

    assert _pending_ids(_collector(home)) == []


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"),
                    reason="仅在不区分大小写的平台上验证大小写不敏感匹配")
def test_deny_project_case_insensitive_on_mac_win(tmp_path):
    home = tmp_path
    proj = tmp_path / "MixedCase"
    proj.mkdir()
    _write_traj(home / ".xskill", "cc_sessions", "traj_mc", cwd=str(tmp_path / "mixedcase"))
    pol = pv.PrivacyPolicy(); pol.deny_project(proj)
    pv.save_policy(pol, home / ".xskill" / "privacy.json")
    assert _pending_ids(_collector(home)) == []


# ── T3 按轨迹 id 排除 ───────────────────────────────────────────

def test_deny_trajectory_excludes_only_that_one(tmp_path):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_keep", cwd="/w/p")
    _write_traj(home / ".xskill", "cc_sessions", "traj_drop", cwd="/w/p")
    pol = pv.PrivacyPolicy(); pol.deny_trajectory("traj_drop")
    pv.save_policy(pol, home / ".xskill" / "privacy.json")
    assert _pending_ids(_collector(home)) == ["traj_keep"]


# ── T4 判定发生在读正文之前 ─────────────────────────────────────

def test_denied_trajectory_body_is_never_read(tmp_path, monkeypatch):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_secret", cwd="/w/secret")
    _write_traj(home / ".xskill", "cc_sessions", "traj_open", cwd="/w/open")
    pol = pv.PrivacyPolicy(); pol.deny_project("/w/secret")
    pv.save_policy(pol, home / ".xskill" / "privacy.json")

    c = _collector(home)
    read_ids: list[str] = []
    orig = c._read_trajectory_text

    def spy(md_path):
        read_ids.append(md_path.stem)
        return orig(md_path)

    monkeypatch.setattr(c, "_read_trajectory_text", spy)
    c.pending()
    assert read_ids == ["traj_open"]     # 被排除的正文一次都没读


# ── T5 不写状态 / 不标记为已上传 ─────────────────────────────────

def test_denied_trajectory_leaves_no_state(tmp_path):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_secret", cwd="/w/secret")
    pol = pv.PrivacyPolicy(); pol.deny_project("/w/secret")
    pv.save_policy(pol, home / ".xskill" / "privacy.json")
    c = _collector(home)
    c.pending()
    assert c._state_store.get("traj_secret") is None


# ── T6 删除规则后恢复上传 ───────────────────────────────────────

def test_allow_after_deny_restores_upload(tmp_path):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_x", cwd="/w/p")
    ppath = home / ".xskill" / "privacy.json"
    pol = pv.PrivacyPolicy(); pol.deny_trajectory("traj_x"); pv.save_policy(pol, ppath)
    c = _collector(home)
    assert _pending_ids(c) == []
    pol = pv.load_policy(ppath); assert pol.allow_trajectory("traj_x"); pv.save_policy(pol, ppath)
    assert _pending_ids(c) == ["traj_x"]      # 同一个 collector，下一轮即生效


# ── T7 混合批次 ─────────────────────────────────────────────────

def test_mixed_allow_deny_batch(tmp_path):
    home = tmp_path
    b = home / ".xskill"
    _write_traj(b, "cc_sessions", "traj_a_ok", cwd="/w/a")
    _write_traj(b, "cc_sessions", "traj_a_secret", cwd="/w/secret/x")
    _write_traj(b, "codex_sessions", "traj_b_ok", cwd="/w/b")
    _write_traj(b, "codex_sessions", "traj_b_denied_id", cwd="/w/b")
    _write_traj(b, "openclaw_sessions", "traj_c_ok", cwd="/w/c")
    pol = pv.PrivacyPolicy()
    pol.deny_project("/w/secret"); pol.deny_trajectory("traj_b_denied_id")
    pv.save_policy(pol, b / "privacy.json")
    assert _pending_ids(_collector(home)) == ["traj_a_ok", "traj_b_ok", "traj_c_ok"]


# ── T8 无 cwd 来源（Cursor / Trae） ──────────────────────────────

def test_cursor_without_cwd_project_rule_does_not_apply_but_id_rule_does(tmp_path):
    home = tmp_path
    b = home / ".xskill"
    _write_traj(b, "cursor_sessions", "traj_cursor_1", cwd=None, source="cursor_transcripts_jsonl")
    _write_traj(b, "cursor_sessions", "traj_cursor_2", cwd=None, source="cursor_transcripts_jsonl")
    pol = pv.PrivacyPolicy()
    pol.deny_project("/w/anything")            # 项目规则：对无 cwd 的轨迹不生效
    pol.deny_trajectory("traj_cursor_2")       # 轨迹规则：生效
    pv.save_policy(pol, b / "privacy.json")
    assert _pending_ids(_collector(home)) == ["traj_cursor_1"]
    assert pv.source_lacks_cwd("cursor_transcripts_jsonl")
    assert pv.source_lacks_cwd("trae_ide_session_json")
    assert not pv.source_lacks_cwd("claude_code_jsonl")


# ── T9 损坏的规则文件必须明确失败 ────────────────────────────────

def test_corrupt_policy_file_fails_loud_not_silent_allow(tmp_path):
    home = tmp_path
    _write_traj(home / ".xskill", "cc_sessions", "traj_x", cwd="/w/p")
    (home / ".xskill" / "privacy.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _collector(home).pending()


@pytest.mark.parametrize("metadata", [None, "{not json"])
def test_project_rules_fail_closed_when_cwd_metadata_is_unreadable(
        tmp_path, monkeypatch, metadata):
    home = tmp_path
    md = _write_traj(
        home / ".xskill", "cc_sessions", "traj_unknown_project",
        cwd="/w/private",
    )
    sidecar = md.with_suffix(".json")
    if metadata is None:
        sidecar.unlink()
    else:
        sidecar.write_text(metadata, encoding="utf-8")
    pol = pv.PrivacyPolicy()
    pol.deny_project("/w/private")
    pv.save_policy(pol, home / ".xskill" / "privacy.json")
    collector = _collector(home)
    monkeypatch.setattr(
        collector,
        "_read_trajectory_text",
        lambda path: pytest.fail(f"不得读取无法判定项目的轨迹正文: {path}"),
    )

    assert collector.pending() == []


# ── T10 CLI ────────────────────────────────────────────────────

def test_cli_privacy_roundtrip(tmp_path, monkeypatch, capsys):
    import xskill.config as cfg
    import xskill.team.client.privacy as pvm
    from xskill import cli

    xh = tmp_path / ".xskill"
    monkeypatch.setattr(cfg, "XSKILL_HOME", xh)
    monkeypatch.setattr(pvm, "default_privacy_path", lambda xskill_home=None: xh / "privacy.json")
    _write_traj(xh, "cc_sessions", "traj_cc_backend_a1b2c3d4", cwd=str(tmp_path / "code" / "backend"))
    _write_traj(xh, "cursor_sessions", "traj_cursor_unknown_9f8e7d6c", cwd=None,
                source="cursor_transcripts_jsonl")
    (tmp_path / "code" / "secret").mkdir(parents=True)

    def run(*argv):
        args = cli.build_parser().parse_args(["privacy", *argv])
        rc = cli.cmd_privacy(args)
        return rc, capsys.readouterr().out

    rc, out = run("list")
    assert rc == 0 and "(no privacy rules)" in out

    rc, out = run("deny-project", str(tmp_path / "code" / "secret"))
    assert rc == 0 and out.startswith("Denied project:")
    assert "Cursor 或 Trae" in out                     # 提示无 cwd 来源

    rc, out = run("deny-project", str(tmp_path / "code" / "secret"))
    assert rc == 0 and out.startswith("Already denied:")

    rc, out = run("deny-trajectory", "traj_cc_backend_a1b2c3d4")
    assert rc == 0 and "Denied trajectory: traj_cc_backend_a1b2c3d4" in out
    assert "来源: claude_code_jsonl" in out and "尚未上传" in out

    rc, out = run("deny-trajectory", "traj_not_here")
    assert rc == 0 and "规则已保存" in out           # 不存在的 id 仍保存（预设）

    rc, out = run("list")
    assert rc == 0
    assert "项目排除规则 (1)" in out and "轨迹排除规则 (2)" in out
    assert "共 1 条轨迹被排除上传" in out or "共 2 条轨迹被排除上传" in out

    rc, out = run("list", "--json")
    data = json.loads(out)
    # JSON / 回显中给用户看的是保留大小写的展示路径，不是比较用的小写键
    assert {p["path"] for p in data["projects"]} == {pv.canonical_project_path(tmp_path / "code" / "secret")}
    assert {t["trajectory_id"] for t in data["trajectories"]} == {"traj_cc_backend_a1b2c3d4", "traj_not_here"}

    rc, out = run("allow-trajectory", "traj_cc_backend_a1b2c3d4")
    assert rc == 0 and out.startswith("Allowed trajectory:")
    rc, out = run("allow-trajectory", "traj_cc_backend_a1b2c3d4")
    assert rc == 1 and out.startswith("Not found:")
    rc, out = run("allow-project", str(tmp_path / "code" / "secret"))
    assert rc == 0 and out.startswith("Allowed project:")


def test_cli_privacy_help_mentions_cursor_trae_limitation():
    import argparse

    from xskill import cli
    p = cli.build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    help_text = sub.choices["privacy"].format_help()
    assert "Cursor 与 Trae" in help_text
    assert "deny-project" in help_text and "allow-trajectory" in help_text


def test_python_module_entrypoint_can_run_privacy_command(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-m", "xskill.cli", "privacy", "list"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert "(no privacy rules)" in completed.stdout
