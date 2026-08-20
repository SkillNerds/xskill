"""generate 快路径：直接提交 main、edit 先读后改、team API 与 CLI。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.agents import agent_tools
from xskill.cli import build_parser, cmd_generate
from xskill.pipeline.registry import prefs_for
from xskill.skill.git import (
    commit_generate_to_main_branch,
    current_branch,
    init_skill_repo_on_baby,
)
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.server.generate_jobs import (
    create_job, enqueue_generate_job, iter_job_events, pin_generated_skills,
)


def _call_tool(tool, *args, **kwargs):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args, **kwargs)


def _bind_skill_ctx(tmp_path: Path, **extra):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        atom_skill_dir=skill_dir,
        extra_read_roots=extra.get("extra_read_roots", (skill_dir,)),
        generate_user_id=extra.get("generate_user_id", "alice"),
        registry_db_path=extra.get("registry_db_path"),
    )
    agent_tools.reset_generate_session()
    return skill_dir, ctx


def test_commit_generate_creates_main_from_empty_dir(tmp_path: Path):
    target = tmp_path / "empty-skill"
    sha = commit_generate_to_main_branch(str(target), "generate-by: alice\n\ninit")
    assert sha
    assert current_branch(str(target)) == "main"
    second = commit_generate_to_main_branch(str(target), "generate-by: alice\n\nagain")
    assert second != sha
    assert current_branch(str(target)) == "main"


def test_commit_generate_promotes_baby_to_main(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    init_skill_repo_on_baby(str(skill_dir / "demo"), name="demo", description="d")
    assert current_branch(str(skill_dir / "demo")) == "baby"
    commit_generate_to_main_branch(str(skill_dir / "demo"), "generate-by: bob\n\npublish")
    assert current_branch(str(skill_dir / "demo")) == "main"


def test_edit_requires_prior_read(tmp_path: Path):
    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    target = skill_dir / "demo"
    target.mkdir()
    skill_md = target / "SKILL.md"
    skill_md.write_text(
        "---\nname: demo\ndescription: hello world\n---\n\n# Demo\n\nold line\n",
        encoding="utf-8",
    )
    with agent_tools.use_agent_tool_context(ctx):
        denied = _call_tool(
            agent_tools.edit_file,
            path=str(skill_md),
            old_string="old line",
            new_string="new line",
        )
        assert denied.startswith("error:")
        assert "has not been read" in denied
        _call_tool(agent_tools.read_file, str(skill_md))
        ok = _call_tool(
            agent_tools.edit_file,
            path=str(skill_md),
            old_string="old line",
            new_string="new line",
        )
        assert ok.startswith("edited:")
    assert "new line" in skill_md.read_text(encoding="utf-8")


def test_generate_prompt_teaches_edit_not_full_rewrite():
    from xskill.agents.generate_agent import SYSTEM_PROMPT

    assert "怎么改文件" in SYSTEM_PROMPT
    assert "edit(path, old_string, new_string)" in SYSTEM_PROMPT
    assert "不要整文件 write_file" in SYSTEM_PROMPT
    assert "刚 write_file 过的脚本" in SYSTEM_PROMPT
    assert "write_file 只用于" in SYSTEM_PROMPT


def test_generate_agent_registers_edit_tool(tmp_path: Path):
    from xskill.agents.generate_agent import GenerateAgent

    captured: dict[str, list[str]] = {}

    def factory(*, instructions, tools):
        captured["tools"] = [
            getattr(tool, "name", None) or getattr(tool, "__name__", "")
            for tool in tools
        ]

        class _Agent:
            def run(self, *_args, **_kwargs):
                class _Result:
                    content = "ok"

                return _Result()

        return _Agent()

    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    agent = GenerateAgent(
        skill_dir=skill_dir,
        agno_agent_factory=factory,
        llm_cfg={},
        logs_dir=None,
    )
    with agent_tools.use_agent_tool_context(ctx):
        agent.run(instruction="改一下发票 skill", user_id="alice", job_id="j1")
    assert "edit" in captured["tools"]
    assert "write_file" in captured["tools"]


def test_generate_new_folder_then_edit_relative_skill_md(tmp_path: Path):
    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        created = _call_tool(
            agent_tools.new_skill_folder, "demo", "发票核对流程",
        )
        assert not str(created).startswith("error:"), created
        stub_path = skill_dir / "demo" / "SKILL.md"
        assert stub_path.is_file()
        _call_tool(agent_tools.read_file, "SKILL.md")
        ok = _call_tool(
            agent_tools.edit_file,
            "SKILL.md",
            "(placeholder — SkillEditAgent 在 candidates 攒满阈值后会用真实 atom 内容填充正文)",
            "用 edit 填进去的正文",
        )
        assert str(ok).startswith("edited:"), ok
        text = stub_path.read_text(encoding="utf-8")
        assert "用 edit 填进去的正文" in text
        assert not (skill_dir / "SKILL.md").exists()


def test_generate_skill_read_then_edit_existing(tmp_path: Path):
    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    target = skill_dir / "invoice"
    target.mkdir()
    (target / "SKILL.md").write_text(
        "---\nname: invoice\ndescription: hello world\n---\n\n# Invoice\n\nold line\n",
        encoding="utf-8",
    )
    with agent_tools.use_agent_tool_context(ctx):
        _call_tool(agent_tools.skill_read, "invoice")
        ok = _call_tool(
            agent_tools.edit_file,
            "SKILL.md",
            "old line",
            "new line",
        )
        assert str(ok).startswith("edited:"), ok
    assert "new line" in (target / "SKILL.md").read_text(encoding="utf-8")
    assert not (skill_dir / "SKILL.md").exists()


def test_generate_read_roots_include_traj_not_parent_secrets(tmp_path: Path):
    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    traj = tmp_path / "team_trajectories" / "clients" / "alice" / "sessions"
    traj.mkdir(parents=True)
    (traj / "traj_1.md").write_text("invoice workflow\n", encoding="utf-8")
    secret = tmp_path / "config.yaml"
    secret.write_text("api_key: nope\n", encoding="utf-8")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        atom_skill_dir=skill_dir,
        extra_read_roots=(skill_dir, tmp_path / "team_trajectories"),
    )
    with agent_tools.use_agent_tool_context(ctx):
        hit = _call_tool(
            agent_tools.grep_files,
            pattern="invoice",
            path=str(tmp_path / "team_trajectories"),
        )
        assert "invoice" in hit
        blocked = _call_tool(agent_tools.read_file, str(secret))
        assert blocked.startswith("error:")


def test_commit_generate_main_tool_prefixes_user(tmp_path: Path):
    skill_dir, ctx = _bind_skill_ctx(tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        result = _call_tool(
            agent_tools.commit_generate_main,
            skill_name="fresh-skill",
            message="created from generate",
        )
    assert result.startswith("committed to main: fresh-skill")
    repo = skill_dir / "fresh-skill"
    assert current_branch(str(repo)) == "main"
    assert agent_tools.generate_committed_skills() == ["fresh-skill"]


def test_pin_generated_skills(tmp_path: Path):
    db = tmp_path / "registry.db"
    from xskill.pipeline.registry import register_dir
    register_dir(tmp_path / "wd", label="t", db_path=db)
    pinned = pin_generated_skills(
        user_id="alice",
        skill_names=["invoice-check"],
        db_path=db,
        max_pinned=10,
    )
    assert pinned == ["invoice-check"]
    rows = prefs_for("alice", db_path=db)
    assert any(r["skill_name"] == "invoice-check" and r["pref"] == "pinned" for r in rows)
    from xskill.pipeline.registry import skill_origin_user
    assert skill_origin_user("invoice-check", db_path=db) == "alice"


def test_iter_job_events_pings_while_running(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    job = create_job(
        client_id="c1",
        user_id="alice",
        instruction="x",
        preferred_names=[],
        logs_dir=logs,
    )
    seen = []
    for event in iter_job_events(
        job["job_id"], poll_seconds=0.01, ping_every=0.02,
    ):
        seen.append(event["type"])
        if event["type"] == "ping":
            assert event.get("status") == "queued"
            break
    assert "ping" in seen
    assert "done" not in seen
    assert job["status"] == "queued"
    assert "waiting for SkillEdit pool seat" in Path(job["log_path"]).read_text(
        encoding="utf-8",
    )


def test_enqueue_writes_pending_file(tmp_path: Path):
    logs = tmp_path / "logs"
    logs.mkdir()
    job = create_job(
        client_id="c1",
        user_id="alice",
        instruction="写一个发票技能",
        preferred_names=["alice"],
        logs_dir=logs,
    )
    enqueue_generate_job(job, logs_dir=logs)
    pending = tmp_path / "generate_jobs" / "pending" / f"{job['job_id']}.json"
    assert pending.is_file()
    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload["instruction"] == "写一个发票技能"
    assert payload["user_id"] == "alice"


def test_iter_job_events_follows_status_file(tmp_path: Path):
    import threading
    import time

    logs = tmp_path / "logs"
    logs.mkdir()
    job = create_job(
        client_id="c1",
        user_id="alice",
        instruction="x",
        preferred_names=[],
        logs_dir=logs,
    )
    status_path = Path(job["log_path"]).with_suffix(".status.json")

    def flip():
        time.sleep(0.04)
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        payload["status"] = "succeeded"
        payload["skill_names"] = ["invoice-check"]
        payload["pinned"] = ["invoice-check"]
        status_path.write_text(json.dumps(payload), encoding="utf-8")

    threading.Thread(target=flip, daemon=True).start()
    events = list(iter_job_events(
        job["job_id"], poll_seconds=0.01, ping_every=1.0,
    ))
    done = events[-1]
    assert done["type"] == "done"
    assert done["ok"] is True
    assert done["skill_names"] == ["invoice-check"]


@pytest.fixture
def team_client(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    traj_root = tmp_path / "team_traj"
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr("xskill.config.get_logs_dir", lambda: logs)
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="secret-token",
        client_registry=reg,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda path, label: None,
    )

    def fake_enqueue(job, *, logs_dir):
        from xskill.team.server.generate_jobs import _update_job
        Path(job["log_path"]).write_text("round 1 thinking\n", encoding="utf-8")
        _update_job(
            job["job_id"],
            status="succeeded",
            skill_names=["invoice-check"],
            pinned=["invoice-check"],
            error="",
        )

    monkeypatch.setattr(
        "xskill.team.server.generate_jobs.enqueue_generate_job",
        fake_enqueue,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app), logs


def test_generate_api_streams_log_and_done(team_client):
    client, _logs = team_client
    registered = client.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "client_label": "alice",
              "hostname": "a", "user_name": "alice"},
    )
    assert registered.status_code == 200
    cid = registered.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}
    started = client.post(
        "/api/v1/team/generate",
        headers=hdr,
        json={"instruction": "写一个发票核对技能", "names": ["alice"]},
    )
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    with client.stream(
        "GET", f"/api/v1/team/generate/{job_id}/events", headers=hdr,
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers.get("x-accel-buffering") == "no"
        body = "".join(stream.iter_text())
    assert "thinking" in body
    assert '"type": "done"' in body or '"type":"done"' in body
    assert "invoice-check" in body


def test_generate_cli_parser_and_stream(monkeypatch, capsys):
    parser = build_parser()
    args = parser.parse_args(
        ["generate", "--name", "alice,bob", "写一个", "发票技能"],
    )
    assert args.command == "generate"
    assert args.name == "alice,bob"
    assert args.instruction == ["写一个", "发票技能"]

    class FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_text(self):
            events = [
                {"type": "log", "chunk": "thinking...\n"},
                {"type": "ping", "status": "running"},
                {"type": "done", "ok": True,
                 "skill_names": ["invoice-check"],
                 "pinned": ["invoice-check"], "error": ""},
            ]
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"

    class FakeClient:
        base_url = "http://server"

        def __init__(self, **kwargs):
            del kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, headers=None):
            assert url == "/api/v1/team/generate"
            assert json["instruction"] == "写一个 发票技能"
            assert json["names"] == ["alice", "bob"]
            return FakeResp(200, {"job_id": "abc"})

        def stream(self, method, url, headers=None):
            assert method == "GET"
            assert url.endswith("/abc/events")
            return FakeStream()

        def close(self):
            return None

    class FakeOuter(FakeClient):
        pass

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    rc = cmd_generate(args, http=FakeOuter(), headers={})
    assert rc == 0
    out = capsys.readouterr().out
    assert "thinking" in out
    assert "invoice-check" in out
    assert "generate job abc" in out
    assert "仍在执行" in out


def test_generate_jobs_submit_to_edit_pool(tmp_path, monkeypatch):
    import time

    from xskill.pipeline.runner import DirectoryWatcher

    logs = tmp_path / "logs"
    logs.mkdir()
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    job = create_job(
        client_id="c1",
        user_id="alice",
        instruction="写一个发票技能",
        preferred_names=[],
        logs_dir=logs,
    )
    enqueue_generate_job(job, logs_dir=logs)
    held = []

    def fake_run(claimed, **kwargs):
        held.append(claimed["job_id"])
        time.sleep(0.25)

    monkeypatch.setattr(
        "xskill.team.server.generate_jobs.run_claimed_generate_job",
        fake_run,
    )
    watcher = DirectoryWatcher(
        llm=None, embed_client=None, config={},
        skill_dir=skill_dir, poll_interval=1,
        logs_dir=logs, xskill_home=tmp_path, home_root=tmp_path,
        pool_config={
            "split": {"workers": 1, "llm_weight": 1},
            "cluster": {"workers": 1, "batch_size": 1, "llm_weight": 1},
            "edit": {"workers": 2, "batch_size": 1, "llm_weight": 1},
            "embed": {"workers": 1},
        },
    )
    watcher._submit_generate_jobs()
    deadline = time.time() + 2
    generate_seats = []
    while time.time() < deadline:
        generate_seats = [
            seat for seat in watcher._pools["edit"].status["seats"]
            if seat and (seat.get("task") or {}).get("kind") == "generate"
        ]
        if generate_seats:
            break
        time.sleep(0.02)
    assert generate_seats
    assert generate_seats[0]["task"]["job_id"] == job["job_id"]
    assert generate_seats[0]["task"]["user_id"] == "alice"
    for fut in list(watcher._futures):
        fut.result(timeout=2)
    watcher._harvest()
    assert held == [job["job_id"]]
    assert watcher._generate_completed == 1
    claimed = tmp_path / "generate_jobs" / "claimed" / f"{job['job_id']}.json"
    assert not claimed.exists()
