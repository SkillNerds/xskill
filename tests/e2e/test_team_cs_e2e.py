"""tests/e2e/test_team_cs_e2e.py — team C/S 端到端（mock LLM）

从"用户视角"打通 team C/S 全链路：

  server: ``xskill serve --server`` 子进程（真进程、真 HTTP、fake LLM 后端）
  client: 真 ``TeamClient`` over 真 httpx（register → collect/upload → sync
          → reconcile → push-edit → cleanup）

断言链：
  1. 脱敏     —— client 本地 CC session 里的 sk- key 上传后在 server 端落盘
                 内容里变成 [REDACTED]
  2. 上传     —— traj_*.md 落到 server 的 clients/<cid>/sessions/ 桶
  3. sync     —— manifest 含 server 预置的 fix-foo skill
  4. reconcile—— fix-foo working copy 物化到 client 本地 + 装进 .claude/skills/
  5. push-edit—— client 本地手改推成 server 的 user-staging/<cid> 隔离分支，
                 main 不受影响
  6. cleanup  —— manifest 外的本地 stale skill 被删

边界（SP1 已知）：server 端 agent 流水线（split/cluster/SkillEdit）跑在
uploaded 轨迹上的完整链路由 test_e2e_xskill_serve_auto.py + test_watcher_atom.py
覆盖；本 E2E 预置 fix-foo，专注验证 *新增的 C/S 同步骨架*。fake LLM 仍配齐
responder 让 server 端流水线不报错。
"""
from __future__ import annotations

import json
import os
import shutil
import site
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import requests
import yaml

# fake LLM server 是测试基础设施（不属 xskill.* 内部 API）
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))
from _fake_llm_server import (  # noqa: E402
    FakeLLMServer, Responder, make_openai_chat_response,
)

XSKILL_BIN = shutil.which("xskill")
SK_LEAK = "sk-abcdEFGH1234567890leak"

# TaskAgent 期望 <atoms><atom>...</atom></atoms> schema；used_skills 指 fix-foo
# 让 server 端 CS 归因路径也跑起来。
_FAKE_ATOM_SPLIT_XML = """<atoms>
<atom>
  <offset_start>10</offset_start>
  <offset_end>200</offset_end>
  <intent>配置 API key</intent>
  <summary>用户请求配置 API key；agent 协助完成。</summary>
  <tags><tag>config</tag></tags>
  <used_skills><skill>fix-foo</skill></used_skills>
  <ux_score>7</ux_score>
</atom>
</atoms>"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _poll(predicate, *, timeout: float = 30.0, interval: float = 0.5,
          desc: str = "predicate"):
    deadline = time.monotonic() + timeout
    last_err = None
    while time.monotonic() < deadline:
        try:
            val = predicate()
            if val:
                return val
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(interval)
    msg = f"timeout waiting for {desc} after {timeout}s"
    if last_err is not None:
        msg += f"; last exception: {last_err!r}"
    raise AssertionError(msg)


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                   text=True, check=True)


def _seed_server_skill(skill_dir: Path, name: str = "fix-foo") -> None:
    """server 预置一个 main-only skill 仓。"""
    d = skill_dir / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d)
    _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d)
    _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 预置 skill\nmetadata:\n  version: 1\n"
        f"---\n# {name}\n\n步骤一。\n",
        encoding="utf-8",
    )
    _git(["add", "."], d)
    _git(["commit", "-q", "-m", "v1"], d)


def _seed_fake_cc_session(client_home: Path) -> None:
    """在 client home 造一个含 sk- key 的假 CC session JSONL。"""
    sid = "11111111-2222-3333-4444-555555555555"
    proj = client_home / ".claude" / "projects" / "hash1"
    proj.mkdir(parents=True)
    lines = [
        json.dumps({
            "type": "user", "sessionId": sid, "cwd": "/work/demo",
            "timestamp": "2026-05-14T10:00:00.000Z",
            "message": {"role": "user",
                        "content": f"帮我配置 API key: {SK_LEAK}"},
        }),
        json.dumps({
            "type": "assistant", "sessionId": sid, "cwd": "/work/demo",
            "timestamp": "2026-05-14T10:00:01.000Z",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "已配置。"}]},
        }),
    ]
    (proj / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _program_fake_server(fake: FakeLLMServer) -> None:
    """配齐 server 端 agent 流水线的 responder，避免 server 报错。"""
    fake.reset()

    def _sys_text(body: dict) -> str:
        for m in body.get("messages", []):
            if m.get("role") == "system":
                c = m.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "\n".join(p.get("text", "") for p in c
                                     if isinstance(p, dict))
        return ""

    # TaskAgent atom 拆分
    fake.add_responder("/chat/completions", Responder(
        name="atom-split",
        match=lambda b: "AtomTask 拆分员" in _sys_text(b),
        build=lambda b: make_openai_chat_response(
            _FAKE_ATOM_SPLIT_XML, model=b.get("model", "fake")),
    ))
    # TaskClusterAgent noop
    fake.add_responder("/chat/completions", Responder(
        name="cluster-noop",
        match=lambda b: "TaskClusterAgent" in _sys_text(b),
        build=lambda b: make_openai_chat_response(
            "信号不足，跳过。", model=b.get("model", "fake")),
    ))
    # ux 评分员
    fake.add_responder("/chat/completions", Responder(
        name="ux-score",
        match=lambda b: "用户体验评审员" in _sys_text(b),
        build=lambda b: make_openai_chat_response(
            json.dumps({"score": 8, "reasons": "[fake] ok"}, ensure_ascii=False),
            model=b.get("model", "fake")),
    ))
    # SkillEditAgent noop（保险垫底）
    fake.add_responder("/chat/completions", Responder(
        name="edit-noop",
        match=lambda b: "SkillEditAgent" in _sys_text(b),
        build=lambda b: make_openai_chat_response(
            "candidates 不足，跳过。", model=b.get("model", "fake")),
    ))


@pytest.fixture(scope="module")
def fake_server():
    srv = FakeLLMServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def team_server(tmp_path, fake_server):
    """起 `xskill serve --server` 子进程；yield (base_url, token, server_home, skill_dir)。"""
    server_home = tmp_path / "server_home"
    xhome = server_home / ".xskill"
    xhome.mkdir(parents=True)
    skill_dir = xhome / "skill"
    skill_dir.mkdir()
    _seed_server_skill(skill_dir)

    config_yaml = {
        "skill_dir": str(skill_dir),
        "llm": {"base_url": fake_server.base_url, "model": "fake-llm",
                "api_key": "fake-key"},
        "embedding": {"base_url": fake_server.base_url, "model": "fake-embed",
                      "api_key": "fake-key", "dim": 0},
        "canary": {"enabled": True, "probability": 0.5, "min_samples": 5,
                   "max_days_hold": 14, "rotate_interval": 300},
        "watcher": {"poll_interval": 2},
        "team": {"server": {"skill_slots": 100, "ranked_slots": 80}},
    }
    (xhome / "config.yaml").write_text(
        yaml.safe_dump(config_yaml, allow_unicode=True), encoding="utf-8")

    _program_fake_server(fake_server)

    port = _free_port()
    env = os.environ.copy()
    env["HOME"] = str(server_home)
    env["PYTHONUSERBASE"] = site.USER_BASE
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    proc = subprocess.Popen(
        [XSKILL_BIN, "serve", "--server", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"

    def _ready() -> bool:
        r = requests.get(f"{base_url}/api/v1/health", timeout=1.5)
        return r.status_code == 200

    try:
        try:
            _poll(_ready, timeout=30.0, desc="xskill serve --server startup")
        except AssertionError:
            proc.terminate()
            try:
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                "xskill serve --server did not become ready; stderr:\n"
                + stderr.decode("utf-8", errors="replace")[-3000:]
            )
        token = json.loads(
            (xhome / "team_server.json").read_text(encoding="utf-8")
        )["join_token"]
        yield base_url, token, server_home, skill_dir
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.mark.skipif(XSKILL_BIN is None, reason="xskill CLI not installed")
def test_team_cs_full_loop(tmp_path, team_server):
    base_url, token, server_home, server_skill_dir = team_server
    client_home = tmp_path / "client_home"
    client_home.mkdir()
    _seed_fake_cc_session(client_home)

    from xskill.team.client.daemon import TeamClient, register_with_server
    from xskill.team.client.state import ClientState

    http = httpx.Client(base_url=base_url, timeout=30.0)
    cid = register_with_server(http, token=token, label="e2e", hostname="e2e")
    assert cid

    client = TeamClient(
        state=ClientState(server_url=base_url, client_id=cid, join_token=token),
        http=http,
        skill_dir=client_home / ".xskill" / "skill",
        cursor_path=client_home / ".xskill" / "cursor.json",
        history_path=client_home / ".xskill" / "install_history.jsonl",
        home_root=client_home,
        quiet_seconds=0,   # 测试不等 3min 静默窗口
        min_change_interval=0,   # 测试不等 10min 上传频率拦截窗口
    )

    client.collector.start_ingesters()
    try:
        # ── 1+2. collect + upload + 脱敏断言 ──────────────────────
        _poll(lambda: client.collect_and_upload() >= 1,
              timeout=40.0, desc="client collect+upload >=1 traj")
        sessions_dir = (server_home / ".xskill" / "team_trajectories"
                        / "clients" / cid / "sessions")
        uploaded = list(sessions_dir.glob("traj_*.md"))
        assert uploaded, f"no uploaded traj under {sessions_dir}"
        body = uploaded[0].read_text(encoding="utf-8")
        assert SK_LEAK not in body, "sk- key 未脱敏就上传到了 server"
        assert "[REDACTED]" in body, "脱敏标记缺失"

        # ── 3. sync ───────────────────────────────────────────────
        manifest = client.sync()
        assert any(s.skill_name == "fix-foo" for s in manifest.slots), \
            f"manifest 不含预置 skill fix-foo: {[s.skill_name for s in manifest.slots]}"

        # ── 4. reconcile + install ────────────────────────────────
        client.reconcile_skill_sides(manifest)
        repo = client_home / ".xskill" / "skill" / "fix-foo"
        assert (repo / ".git").is_dir(), "fix-foo working copy 未物化"
        assert (repo / "SKILL.md").read_text(encoding="utf-8").startswith("---")
        installed = client_home / ".claude" / "skills" / "fix-foo"
        assert installed.exists(), "fix-foo 未装进 client 的 .claude/skills/"

        # ── 5. push-edit ──────────────────────────────────────────
        skill_md = repo / "SKILL.md"
        skill_md.write_text(
            skill_md.read_text(encoding="utf-8") + "\n<!-- user tweak by e2e -->\n",
            encoding="utf-8")
        pushed = client.push_user_edits()
        assert pushed == 1, f"push_user_edits 应推 1 个 skill，实际 {pushed}"
        # server 端 fix-foo 仓出现 user-staging/<cid> 分支
        central = server_skill_dir / "fix-foo"
        branches = subprocess.run(
            ["git", "-C", str(central), "branch", "--list",
             f"user-staging/{cid}"],
            capture_output=True, text=True).stdout
        assert f"user-staging/{cid}" in branches, \
            f"server 未创建 user-staging/{cid} 分支；branches={branches!r}"
        # 推上去的内容含我的 tweak
        edited = subprocess.run(
            ["git", "-C", str(central), "show",
             f"user-staging/{cid}:SKILL.md"],
            capture_output=True, text=True).stdout
        assert "user tweak by e2e" in edited
        # main 未被动
        main_body = subprocess.run(
            ["git", "-C", str(central), "show", "main:SKILL.md"],
            capture_output=True, text=True).stdout
        assert "user tweak by e2e" not in main_body, "client 手改污染了公共 main"

        # ── 6. cleanup ────────────────────────────────────────────
        stale = client_home / ".xskill" / "skill" / "stale-skill"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("# stale", encoding="utf-8")
        client.cleanup(client.sync())
        assert not stale.exists(), "cleanup 未删除 manifest 外的 stale skill"
        assert repo.is_dir(), "cleanup 误删了 manifest 内的 skill"
    finally:
        client.collector.stop_ingesters()
        http.close()
