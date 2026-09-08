"""test_team_client_privacy.py — 本机轨迹上传规则与模式（issue #244）

验收点：
* server 与本机都不配置时，行为与当前版本一致（全部上传）
* 生效模式取 server 与本机中更严者：本机 allowlist 不被 server denylist 放宽，
  server allowlist 下本机 denylist 无效
* allowlist 下无规则一条都不上传，且正文一次都不读、不留任何上传状态
* 项目规则含子目录、符号链接、相对路径、~、大小写；子目录规则优先于父目录
* Cursor / Trae 无 cwd 与 sidecar 损坏的轨迹按模式默认处理
* 删除规则后可重新进入上传流程
* server 改模式后下一轮 sync 生效并落盘，无需重新 connect
* 规则文件损坏时明确失败，而不是当作无规则放行
* CLI mode / status / allow / deny / clear / review 与 --json；review 非 TTY 退出码 2
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.team.client import privacy as pv
from xskill.team.client.collector import TeamCollector
from xskill.team.client.daemon import TeamClient, register_with_server_full
from xskill.team.client.state import ClientState, load_client_state, save_client_state
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry


def _write_traj(bridge: Path, eco_dir: str, traj_id: str, *, cwd: str | None,
                sidecar: str | None = "ok", body: str = "# traj\n\nhello world\n") -> Path:
    """sidecar="ok" 正常写；None 不写；"broken" 写坏 JSON。"""
    bridge_dir = bridge / eco_dir
    bridge_dir.mkdir(parents=True, exist_ok=True)
    md_path = bridge_dir / f"{traj_id}.md"
    md_path.write_text(body, encoding="utf-8")
    if sidecar == "ok":
        meta = {"session_id": traj_id, "model": "m"}
        if cwd is not None:
            meta["cwd"] = cwd
        md_path.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    elif sidecar == "broken":
        md_path.with_suffix(".json").write_text("{not json", encoding="utf-8")
    old = time.time() - 600
    os.utime(md_path, (old, old))
    return md_path


def _collector(home: Path, *, server_mode: str | None = None) -> TeamCollector:
    xskill_home = home / ".xskill"
    xskill_home.mkdir(parents=True, exist_ok=True)
    collector = TeamCollector(
        cursor_path=xskill_home / "clients" / "srv" / "cursor.json",
        quiet_seconds=0, min_change_interval=0, home_root=home,
        state_db_path=xskill_home / "clients" / "srv" / "client_state.db",
        privacy_path=xskill_home / "privacy.json",
    )
    collector.server_privacy_mode = server_mode
    return collector


def _pending_ids(collector: TeamCollector) -> list[str]:
    return sorted(pending.traj_id for pending in collector.pending())


def _policy_with(home: Path, *, local_mode: str = "auto", **rules: str) -> Path:
    policy = pv.PrivacyPolicy(local_mode=local_mode)
    for path, rule in rules.items():
        policy.set_project(path, rule)
    return pv.save_policy(policy, home / ".xskill" / "privacy.json")


# ── 模式合成 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("server_mode,local_mode,expected", [
    (None, "auto", "denylist"),
    ("denylist", "auto", "denylist"),
    ("allowlist", "auto", "allowlist"),
    ("denylist", "allowlist", "allowlist"),
    ("allowlist", "denylist", "allowlist"),
    (None, "allowlist", "allowlist"),
    ("denylist", "denylist", "denylist"),
])
def test_effective_mode_is_the_stricter_side(server_mode, local_mode, expected):
    assert pv.effective_mode(server_mode, local_mode) == expected


def test_mode_origin_labels():
    assert pv.mode_origin("allowlist", "denylist") == "server_forced"
    assert pv.mode_origin("denylist", "allowlist") == "local"
    assert pv.mode_origin("denylist", "auto") == "server_default"
    assert pv.mode_origin(None, "auto") == "server_missing"


# ── 默认行为不变 ─────────────────────────────────────────────────

def test_no_config_anywhere_uploads_everything_like_before(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    _write_traj(bridge, "codex_sessions", "traj_codex_b", cwd="/w/proj-b")
    _write_traj(bridge, "cursor_sessions", "traj_cursor_c", cwd=None)
    _write_traj(bridge, "cc_sessions", "traj_cc_broken", cwd=None, sidecar="broken")
    _write_traj(bridge, "cc_sessions", "traj_cc_nosidecar", cwd=None, sidecar=None)
    collector = _collector(tmp_path)
    assert not (bridge / "privacy.json").exists()
    assert _pending_ids(collector) == [
        "traj_cc_a", "traj_cc_broken", "traj_cc_nosidecar", "traj_codex_b", "traj_cursor_c",
    ]


# ── allowlist ────────────────────────────────────────────────────

def test_allowlist_without_rules_uploads_nothing_and_never_reads_body(tmp_path, monkeypatch):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    _write_traj(bridge, "cursor_sessions", "traj_cursor_c", cwd=None)
    collector = _collector(tmp_path, server_mode="allowlist")
    read_calls: list[Path] = []
    original = TeamCollector._read_trajectory_text

    def spy(self, md_path):
        read_calls.append(md_path)
        return original(self, md_path)

    monkeypatch.setattr(TeamCollector, "_read_trajectory_text", spy)
    assert _pending_ids(collector) == []
    assert read_calls == []
    assert collector._state_store.get("traj_cc_a") is None


def test_allowlist_allow_project_covers_subdirs_only(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_root", cwd="/w/proj")
    _write_traj(bridge, "cc_sessions", "traj_cc_sub", cwd="/w/proj/backend/api")
    _write_traj(bridge, "cc_sessions", "traj_cc_sibling", cwd="/w/proj-other")
    _write_traj(bridge, "cc_sessions", "traj_cc_prefix", cwd="/w/project")
    _policy_with(tmp_path, **{"/w/proj": "allow"})
    collector = _collector(tmp_path, server_mode="allowlist")
    assert _pending_ids(collector) == ["traj_cc_root", "traj_cc_sub"]


def test_local_allowlist_is_not_loosened_by_server_denylist(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    _write_traj(bridge, "cc_sessions", "traj_cc_b", cwd="/w/proj-b")
    _policy_with(tmp_path, local_mode="allowlist", **{"/w/proj-a": "allow"})
    assert _pending_ids(_collector(tmp_path, server_mode="denylist")) == ["traj_cc_a"]
    assert _pending_ids(_collector(tmp_path, server_mode=None)) == ["traj_cc_a"]


def test_server_allowlist_overrides_local_denylist(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    _policy_with(tmp_path, local_mode="denylist")
    assert _pending_ids(_collector(tmp_path, server_mode="allowlist")) == []


# ── denylist ─────────────────────────────────────────────────────

def test_denylist_deny_project_skips_only_that_tree(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_secret", cwd="/w/secret")
    _write_traj(bridge, "cc_sessions", "traj_cc_secret_sub", cwd="/w/secret/x")
    _write_traj(bridge, "cc_sessions", "traj_cc_open", cwd="/w/open")
    _write_traj(bridge, "cc_sessions", "traj_cc_lookalike", cwd="/w/secret-but-different")
    _policy_with(tmp_path, **{"/w/secret": "deny"})
    collector = _collector(tmp_path, server_mode="denylist")
    assert _pending_ids(collector) == ["traj_cc_lookalike", "traj_cc_open"]
    assert collector._state_store.get("traj_cc_secret") is None


def test_subdir_rule_wins_over_parent_rule(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_code", cwd="/w/code/app")
    _write_traj(bridge, "cc_sessions", "traj_cc_secret", cwd="/w/code/secret/x")
    _policy_with(tmp_path, **{"/w/code": "allow", "/w/code/secret": "deny"})
    assert _pending_ids(_collector(tmp_path, server_mode="allowlist")) == ["traj_cc_code"]
    assert _pending_ids(_collector(tmp_path, server_mode="denylist")) == ["traj_cc_code"]


def test_clearing_rule_restores_upload_on_next_scan(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    policy_path = _policy_with(tmp_path, **{"/w/proj-a": "deny"})
    collector = _collector(tmp_path, server_mode="denylist")
    assert _pending_ids(collector) == []
    policy = pv.load_policy(policy_path)
    assert policy.clear_project("/w/proj-a")[0]
    pv.save_policy(policy, policy_path)
    assert _pending_ids(collector) == ["traj_cc_a"]


# ── 无 cwd / sidecar 损坏 ────────────────────────────────────────

def test_no_cwd_and_broken_sidecar_follow_mode_default(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cursor_sessions", "traj_cursor_c", cwd=None)
    _write_traj(bridge, "trae_sessions", "traj_trae_t", cwd=None)
    _write_traj(bridge, "cc_sessions", "traj_cc_broken", cwd=None, sidecar="broken")
    _policy_with(tmp_path, **{"/w/anything": "allow"})
    assert _pending_ids(_collector(tmp_path, server_mode="denylist")) == [
        "traj_cc_broken", "traj_cursor_c", "traj_trae_t",
    ]
    assert _pending_ids(_collector(tmp_path, server_mode="allowlist")) == []


def test_decide_reasons():
    policy = pv.PrivacyPolicy()
    assert policy.decide(None, "cursor", "allowlist").reason == "no_cwd"
    assert policy.decide(None, "claude_code", "allowlist").reason == "broken_sidecar"
    assert policy.decide("/w/x", "claude_code", "denylist") == pv.Decision("upload", "default")


# ── 路径规范化 ───────────────────────────────────────────────────

def test_rule_path_normalizes_relative_tilde_and_symlink(tmp_path, monkeypatch):
    real = tmp_path / "home" / "code" / "proj"
    real.mkdir(parents=True)
    link = tmp_path / "home" / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "home" / "code")
    policy = pv.PrivacyPolicy()
    changed, key = policy.set_project("proj", "deny")
    assert changed and key == pv.normalize_project_path(real)
    assert policy.decide(str(link / "sub"), "claude_code", "denylist").action == "skip"
    assert policy.decide("~/code/proj", "claude_code", "denylist").action == "skip"
    assert policy.set_project("~/link", "deny") == (False, key)


def test_case_insensitive_match_on_mac_and_windows(monkeypatch):
    monkeypatch.setattr(pv, "_CASE_INSENSITIVE_FS", True)
    policy = pv.PrivacyPolicy()
    policy.set_project("/W/Proj", "deny")
    assert policy.decide("/w/proj/Sub", "claude_code", "denylist").action == "skip"
    assert policy.projects[pv.normalize_project_path("/W/Proj")].display.endswith("Proj")


# ── 规则文件 ─────────────────────────────────────────────────────

def test_policy_roundtrip_and_corrupt_file_fails_loud(tmp_path):
    policy_path = tmp_path / "privacy.json"
    policy = pv.PrivacyPolicy(local_mode="allowlist")
    policy.set_project("/w/a", "allow")
    policy.set_project("/w/b", "deny")
    pv.save_policy(policy, policy_path)
    loaded = pv.load_policy(policy_path)
    assert loaded.local_mode == "allowlist"
    assert {key: rule.rule for key, rule in loaded.projects.items()} == {
        pv.normalize_project_path("/w/a"): "allow", pv.normalize_project_path("/w/b"): "deny",
    }
    policy_path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        pv.load_policy(policy_path)
    policy_path.write_text(json.dumps({"mode": "sometimes"}), encoding="utf-8")
    with pytest.raises(ValueError):
        pv.load_policy(policy_path)


def test_corrupt_policy_stops_collector_instead_of_uploading(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a", cwd="/w/proj-a")
    bridge.mkdir(exist_ok=True)
    (bridge / "privacy.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        _collector(tmp_path).pending()


# ── 报表归组 ─────────────────────────────────────────────────────

def test_build_report_groups_by_rule_and_lists_unattributed(tmp_path):
    bridge = tmp_path / ".xskill"
    _write_traj(bridge, "cc_sessions", "traj_cc_a1", cwd="/w/a/x")
    _write_traj(bridge, "codex_sessions", "traj_codex_a2", cwd="/w/a/y")
    _write_traj(bridge, "cc_sessions", "traj_cc_b", cwd="/w/b")
    _write_traj(bridge, "cursor_sessions", "traj_cursor_c", cwd=None)
    policy = pv.PrivacyPolicy()
    policy.set_project("/w/a", "allow")
    policy.set_project("/w/future", "deny")
    rows, complete = pv.scan_local_trajectories(bridge)
    assert complete and len(rows) == 4
    report = pv.build_report(policy, "allowlist", rows)
    by_path = {summary.path: summary for summary in report.projects}
    assert by_path[pv.canonical_project_path("/w/a")].traj == 2
    assert by_path[pv.canonical_project_path("/w/a")].harnesses == ["claude_code", "codex"]
    assert by_path[pv.canonical_project_path("/w/b")].effective == "skip"
    assert by_path[pv.canonical_project_path("/w/future")].traj == 0
    assert report.no_cwd.traj == 1 and report.no_cwd.effective == "skip"
    assert (report.upload, report.skip) == (2, 2)
    assert report.to_dict()["mode_origin"] == "server_forced"


def test_scan_respects_time_budget(tmp_path, monkeypatch):
    bridge = tmp_path / ".xskill"
    for index in range(3):
        _write_traj(bridge, "cc_sessions", f"traj_cc_{index}", cwd="/w/a")
    ticks = iter([0.0, 0.0, 10.0, 10.0, 10.0])
    monkeypatch.setattr(pv.time, "monotonic", lambda: next(ticks))
    rows, complete = pv.scan_local_trajectories(bridge, time_budget_seconds=1.0)
    assert not complete and len(rows) == 1


# ── server 下发与热切换 ──────────────────────────────────────────

@pytest.fixture
def team_app(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    server_api.init_team_context(
        join_token="tok", client_registry=ClientRegistry(tmp_path / "clients.db"),
        skill_dir=skill_dir, traj_root=tmp_path / "team_traj",
        register_dir=lambda path, label: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return app


def _set_server_config(monkeypatch, cfg: dict | None):
    from xskill.api import app as app_mod
    monkeypatch.setattr(app_mod, "_config", cfg)


def test_register_and_sync_report_server_mode_without_restart(team_app, monkeypatch):
    _set_server_config(monkeypatch, {"team": {"server": {}}})
    http = TestClient(team_app)
    reg = register_with_server_full(http, token="tok", label="a", hostname="h")
    assert reg["privacy_mode"] == "denylist"
    headers = {"X-Xskill-Token": "tok", "X-Xskill-Client": reg["client_id"]}
    assert http.get("/api/v1/team/sync", headers=headers).json()["privacy_mode"] == "denylist"
    from xskill.api import app as app_mod
    app_mod._config["team"]["server"]["privacy_mode"] = "allowlist"
    assert http.get("/api/v1/team/sync", headers=headers).json()["privacy_mode"] == "allowlist"
    reg_again = register_with_server_full(http, token="tok", label="a", hostname="h")
    assert reg_again["privacy_mode"] == "allowlist"


def test_team_privacy_mode_config_validation():
    from xskill.config import team_privacy_mode
    assert team_privacy_mode(None) == "denylist"
    assert team_privacy_mode({"team": {"server": {"privacy_mode": "allowlist"}}}) == "allowlist"
    with pytest.raises(ValueError):
        team_privacy_mode({"team": {"server": {"privacy_mode": "whitelist"}}})


def test_client_sync_adopts_server_mode_and_persists_it(team_app, tmp_path, monkeypatch):
    _set_server_config(monkeypatch, {"team": {"server": {"privacy_mode": "allowlist"}}})
    http = TestClient(team_app)
    reg = register_with_server_full(http, token="tok", label="a", hostname="h")
    state_path = tmp_path / "client_home" / ".xskill" / "team_client.json"
    state = ClientState(server_url="http://testserver", client_id=reg["client_id"],
                        join_token="tok", server_privacy_mode=reg["privacy_mode"])
    save_client_state(state, state_path)
    assert load_client_state(state_path).server_privacy_mode == "allowlist"
    team_client = TeamClient(
        state=state, http=http, skill_dir=tmp_path / "client_home" / ".xskill" / "skill",
        cursor_path=tmp_path / "cursor.json", history_path=tmp_path / "history.jsonl",
        home_root=tmp_path / "client_home", min_change_interval=0, state_path=state_path,
    )
    assert team_client.collector.server_privacy_mode == "allowlist"
    from xskill.api import app as app_mod
    app_mod._config["team"]["server"]["privacy_mode"] = "denylist"
    team_client.sync()
    assert team_client.state.server_privacy_mode == "denylist"
    assert team_client.collector.server_privacy_mode == "denylist"
    assert load_client_state(state_path).server_privacy_mode == "denylist"


def test_old_state_file_without_mode_loads_as_none(tmp_path):
    state_path = tmp_path / "team_client.json"
    state_path.write_text(json.dumps({"server_url": "http://s", "client_id": "c", "join_token": "t"}))
    assert load_client_state(state_path).server_privacy_mode is None


# ── CLI ──────────────────────────────────────────────────────────

@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    import xskill.config as cfg
    xskill_home = tmp_path / ".xskill"
    xskill_home.mkdir()
    monkeypatch.setattr(cfg, "XSKILL_HOME", xskill_home)
    _write_traj(xskill_home, "cc_sessions", "traj_cc_backend", cwd=str(tmp_path / "code" / "backend"))
    _write_traj(xskill_home, "cc_sessions", "traj_cc_secret", cwd=str(tmp_path / "code" / "secret"))
    _write_traj(xskill_home, "cursor_sessions", "traj_cursor_x", cwd=None)
    (tmp_path / "code" / "backend").mkdir(parents=True)
    (tmp_path / "code" / "secret").mkdir(parents=True)
    return tmp_path


def _run_privacy(capsys, *argv):
    from xskill import cli
    args = cli.build_parser().parse_args(["privacy", *argv])
    return cli.cmd_privacy(args), capsys.readouterr()


def test_cli_status_and_mode_before_connect(cli_home, capsys):
    return_code, captured = _run_privacy(capsys, "status")
    assert return_code == 0
    assert "mode: denylist（未连接 server；server (未连接)）" in captured.out
    assert "上传 3 条，不上传 0 条。" in captured.out
    assert "Cursor / Trae" in captured.out

    return_code, captured = _run_privacy(capsys, "mode", "allowlist")
    assert return_code == 0
    assert captured.out.startswith("Mode: allowlist（本机设置；server 当前 (未连接)，生效 allowlist）")
    assert "已放行 0 个项目" in captured.out

    return_code, captured = _run_privacy(capsys, "mode")
    assert return_code == 0 and "生效: allowlist" in captured.out

    return_code, captured = _run_privacy(capsys, "status", "--json")
    payload = json.loads(captured.out)
    assert payload["mode"] == "allowlist" and payload["mode_origin"] == "local"
    assert (payload["upload"], payload["skip"]) == (0, 3)
    assert payload["unattributed"]["traj"] == 1


def test_cli_allow_deny_clear_roundtrip(cli_home, capsys, monkeypatch):
    backend = cli_home / "code" / "backend"
    secret = cli_home / "code" / "secret"
    _run_privacy(capsys, "mode", "allowlist")

    monkeypatch.chdir(backend)
    return_code, captured = _run_privacy(capsys, "allow")
    assert return_code == 0
    assert captured.out.startswith(f"Allowed: {pv.canonical_project_path(backend)}")
    assert "含已有的 1 条" in captured.out and "Cursor / Trae" in captured.out

    return_code, captured = _run_privacy(capsys, "allow", str(backend))
    assert return_code == 0 and captured.out.startswith("Already allowed:")

    return_code, captured = _run_privacy(capsys, "deny", str(secret))
    assert return_code == 0
    assert captured.out.startswith(f"Denied: {pv.canonical_project_path(secret)}")
    assert "本来就不上传" in captured.out and "已上传过" not in captured.out

    return_code, captured = _run_privacy(capsys, "allow", str(cli_home / "code" / "future"))
    assert return_code == 0 and "该目录当前不存在" in captured.out

    return_code, captured = _run_privacy(capsys, "status")
    assert "allow" in captured.out and "deny" in captured.out
    assert "上传 1 条，不上传 2 条。" in captured.out

    return_code, captured = _run_privacy(capsys, "clear", str(secret), "--json")
    assert return_code == 0 and json.loads(captured.out)["status"] == "cleared"
    return_code, captured = _run_privacy(capsys, "clear", str(secret))
    assert return_code == 1 and captured.out.startswith("Not found:")

    policy = pv.load_policy(cli_home / ".xskill" / "privacy.json")
    assert policy.local_mode == "allowlist"
    assert {rule.rule for rule in policy.projects.values()} == {"allow"}


def test_cli_deny_reports_previously_uploaded_count(cli_home, capsys):
    from xskill.team.client.upload_state import TrajectoryUploadStateStore
    store = TrajectoryUploadStateStore(
        db_path=cli_home / ".xskill" / "clients" / "srv" / "client_state.db",
        legacy_cursor_path=cli_home / ".xskill" / "clients" / "srv" / "cursor.json",
        home_root=cli_home,
    )
    md_path = cli_home / ".xskill" / "cc_sessions" / "traj_cc_secret.md"
    stat = md_path.stat()
    store.record_seen_file(
        trajectory_id="traj_cc_secret", file_path=str(md_path), harness_name="claude_code",
        model_name="m", file_size_bytes=stat.st_size,
        file_modified_time_nanoseconds=stat.st_mtime_ns,
        file_changed_time_nanoseconds=stat.st_ctime_ns,
        original_content_hash="raw", cleaned_content_hash="clean",
    )
    store.mark_uploaded("traj_cc_secret", "clean")
    return_code, captured = _run_privacy(capsys, "deny", str(cli_home / "code" / "secret"))
    assert return_code == 0 and "其中 1 条此前已上传过" in captured.out


def test_cli_mode_rejects_unknown_value_and_review_needs_tty(cli_home, capsys, monkeypatch):
    return_code, captured = _run_privacy(capsys, "mode", "whitelist")
    assert return_code == 2 and "mode 只能是" in captured.err
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return_code, captured = _run_privacy(capsys, "review")
    assert return_code == 2 and "交互式终端" in captured.err


def test_cli_review_applies_choices(cli_home, capsys, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["a", "d"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    return_code, captured = _run_privacy(capsys, "review")
    assert return_code == 0
    assert "完成：放行 1 个，排除 1 个，保持不变 0 个。" in captured.out
    policy = pv.load_policy(cli_home / ".xskill" / "privacy.json")
    assert sorted(rule.rule for rule in policy.projects.values()) == ["allow", "deny"]


def test_cli_corrupt_rules_file_errors_out(cli_home, capsys):
    (cli_home / ".xskill" / "privacy.json").write_text("{oops", encoding="utf-8")
    return_code, captured = _run_privacy(capsys, "status")
    assert return_code == 2 and "cannot read privacy rules" in captured.err


def test_cli_help_explains_modes():
    import argparse
    from xskill import cli
    parser = cli.build_parser()
    subparsers = next(action for action in parser._actions
                      if isinstance(action, argparse._SubParsersAction))
    help_text = subparsers.choices["privacy"].format_help()
    assert "allowlist" in help_text and "denylist" in help_text
    assert "Cursor 与 Trae" in help_text
    connect_help = subparsers.choices["connect"].format_help()
    assert "--privacy" in connect_help


def test_python_module_entrypoint_runs_privacy_status(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "xskill.cli", "privacy", "status"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "mode: denylist" in completed.stdout
    assert "(本机尚未发现任何轨迹)" in completed.stdout
