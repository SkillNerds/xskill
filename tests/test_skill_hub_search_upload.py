"""`xskill search` / `xskill upload` 的 server 端点 + client 槽位测试。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill import cli
from xskill.recommend.skillhub import SkillHub
from xskill.team.client.search_slots import SearchSlots
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry, safe_dir_name

TOKEN = "secret-token"


def _write_hub_skill(hub_dir: Path, folder: str, name: str, description: str) -> Path:
    d = hub_dir / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n",
        encoding="utf-8")
    return d


def _zip_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    return buf.getvalue()


def _make_team_client(tmp_path: Path, *, skillhub) -> TestClient:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    server_api.init_team_context(
        join_token=TOKEN,
        client_registry=ClientRegistry(tmp_path / "clients.db"),
        skill_dir=skill_dir,
        traj_root=tmp_path / "team_traj",
        probability=0.2, ranked_slots=80, total_slots=100,
        register_dir=lambda path, label: None,
        skillhub=skillhub,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app)


def _register(client: TestClient, user_name: str | None = None) -> tuple[str, dict]:
    body = {"token": TOKEN, "client_label": "t", "hostname": "h"}
    if user_name:
        body["user_name"] = user_name
    r = client.post("/api/v1/team/register", json=body)
    assert r.status_code == 200
    cid = r.json()["client_id"]
    return cid, {"X-Xskill-Token": TOKEN, "X-Xskill-Client": cid}


@pytest.fixture
def hub_env(tmp_path):
    hub_dir = tmp_path / "skillhub_skills"
    _write_hub_skill(hub_dir, "docker-helper", "docker-helper",
                     "Manage docker containers and compose files")
    _write_hub_skill(hub_dir, "k8s-deploy", "k8s-deploy",
                     "Deploy workloads to kubernetes clusters")
    _write_hub_skill(hub_dir, "unrelated", "poetry-writer",
                     "Write beautiful poems")
    hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
    client = _make_team_client(tmp_path, skillhub=hub)
    return SimpleNamespace(client=client, hub=hub, hub_dir=hub_dir,
                           tmp_path=tmp_path)


# ── server: search ──────────────────────────────────────────────

def test_search_matches_by_keyword_without_profile(hub_env):
    _cid, hdr = _register(hub_env.client)
    r = hub_env.client.get("/api/v1/team/skill_hub/search",
                           params={"query": "docker"}, headers=hdr)
    assert r.status_code == 200
    results = r.json()["results"]
    assert [x["display_name"] for x in results] == ["docker-helper"]
    top = results[0]
    assert top["description"].startswith("Manage docker")
    assert top["skill_id"] and top["content_sha"] and top["source_path"]


def test_search_name_hit_outranks_description_hit(hub_env):
    _cid, hdr = _register(hub_env.client)
    r = hub_env.client.get("/api/v1/team/skill_hub/search",
                           params={"query": "deploy"}, headers=hdr)
    names = [x["display_name"] for x in r.json()["results"]]
    assert names[0] == "k8s-deploy"


def test_search_rejects_empty_query_and_requires_auth(hub_env):
    _cid, hdr = _register(hub_env.client)
    r = hub_env.client.get("/api/v1/team/skill_hub/search",
                           params={"query": "  "}, headers=hdr)
    assert r.status_code == 400
    r = hub_env.client.get("/api/v1/team/skill_hub/search",
                           params={"query": "docker"},
                           headers={"X-Xskill-Token": "wrong",
                                    "X-Xskill-Client": "ghost"})
    assert r.status_code == 401


def test_search_and_upload_503_when_skillhub_disabled(tmp_path):
    client = _make_team_client(tmp_path, skillhub=None)
    _cid, hdr = _register(client)
    r = client.get("/api/v1/team/skill_hub/search",
                   params={"query": "docker"}, headers=hdr)
    assert r.status_code == 503
    r = client.post("/api/v1/team/skill_hub/upload",
                    files={"file": ("x.zip", b"zz", "application/zip")},
                    headers=hdr)
    assert r.status_code == 503


# ── server: upload ──────────────────────────────────────────────

def test_upload_stores_under_user_skill_hub_and_is_searchable(hub_env, tmp_path):
    cid, hdr = _register(hub_env.client, user_name="alice")
    # 先建立不含上传件的 TTL 快照，验证 upload 会显式刷新而非等 5 秒。
    before = hub_env.client.get(
        "/api/v1/team/skill_hub/search",
        params={"query": "terraform"}, headers=hdr,
    )
    assert before.status_code == 200 and before.json()["results"] == []
    src = tmp_path / "my-skill-src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: terraform-ops\ndescription: Provision cloud infra with terraform\n"
        "---\n# terraform-ops\nbody\n", encoding="utf-8")
    (src / "references").mkdir()
    (src / "references" / "notes.md").write_text("extra", encoding="utf-8")

    r = hub_env.client.post("/api/v1/team/skill_hub/upload",
                            files={"file": ("s.zip", _zip_dir(src),
                                            "application/zip")},
                            headers=hdr)
    assert r.status_code == 200, r.text
    stored = r.json()
    owner = safe_dir_name("alice", cid)
    expected_dir = hub_env.hub_dir / "user_skill_hub" / owner / "terraform-ops"
    assert Path(stored["stored_path"]) == expected_dir
    assert (expected_dir / "SKILL.md").is_file()
    assert (expected_dir / "references" / "notes.md").read_text(
        encoding="utf-8") == "extra"
    assert stored["source_path"] == f"user_skill_hub/{owner}/terraform-ops"

    # 上传件立即可被搜索、可按 skill_id 拉 bundle（zip）
    r = hub_env.client.get("/api/v1/team/skill_hub/search",
                           params={"query": "terraform"}, headers=hdr)
    hits = r.json()["results"]
    assert [x["display_name"] for x in hits] == ["terraform-ops"]
    assert hits[0]["skill_id"] == stored["skill_id"]
    r = hub_env.client.get(f"/api/v1/team/skill/{stored['skill_id']}/bundle",
                           headers=hdr)
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "SKILL.md" in zf.namelist()


def test_upload_reupload_replaces_same_folder(hub_env, tmp_path):
    _cid, hdr = _register(hub_env.client, user_name="bob")
    src = tmp_path / "v1"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: swiss-knife\ndescription: first version\n---\nbody\n",
        encoding="utf-8")
    (src / "old.txt").write_text("old", encoding="utf-8")
    r1 = hub_env.client.post("/api/v1/team/skill_hub/upload",
                             files={"file": ("s.zip", _zip_dir(src),
                                             "application/zip")}, headers=hdr)
    assert r1.status_code == 200
    (src / "SKILL.md").write_text(
        "---\nname: swiss-knife\ndescription: second version\n---\nbody\n",
        encoding="utf-8")
    (src / "old.txt").unlink()
    r2 = hub_env.client.post("/api/v1/team/skill_hub/upload",
                             files={"file": ("s.zip", _zip_dir(src),
                                             "application/zip")}, headers=hdr)
    assert r2.status_code == 200
    stored_dir = Path(r2.json()["stored_path"])
    assert stored_dir == Path(r1.json()["stored_path"])
    assert not (stored_dir / "old.txt").exists()
    assert r2.json()["content_sha"] != r1.json()["content_sha"]


def test_upload_rejects_bad_archives(hub_env):
    _cid, hdr = _register(hub_env.client)
    post = lambda payload: hub_env.client.post(  # noqa: E731
        "/api/v1/team/skill_hub/upload",
        files={"file": ("s.zip", payload, "application/zip")}, headers=hdr)

    assert post(b"not a zip").status_code == 400

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "no skill here")
    assert post(buf.getvalue()).status_code == 400  # 缺 SKILL.md

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "no frontmatter at all")
    assert post(buf.getvalue()).status_code == 400  # frontmatter 非法

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md",
                    "---\nname: evil\ndescription: evil\n---\nbody\n")
        zf.writestr("../escape.txt", "evil")
    assert post(buf.getvalue()).status_code == 400  # 路径穿越
    assert not (hub_env.hub_dir.parent / "escape.txt").exists()


# ── client: SearchSlots 滚动槽位 ────────────────────────────────

def _fake_result(name: str) -> dict:
    return {"skill_id": f"{name}@0000000000ff", "display_name": name,
            "description": f"desc of {name}", "content_sha": "ab" * 8,
            "source_path": name}


def _fake_archive(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md",
                    f"---\nname: {name}\ndescription: d\n---\nbody\n")
    return buf.getvalue()


def test_search_slots_marker_ledger_and_rolling_eviction(tmp_path):
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=tmp_path / "home", capacity=3)
    for i in range(4):
        path = slots.install(_fake_result(f"s{i}"), _fake_archive(f"s{i}"),
                             query=f"q{i}")
        assert path.is_file() is False and (path / "SKILL.md").is_file()
        marker = json.loads((path / ".xskill_search.json").read_text(
            encoding="utf-8"))
        assert marker["query"] == f"q{i}" and marker["searched_at"]

    ledger = slots.entries()
    assert [s["skill_id"] for s in ledger] == [
        "s1@0000000000ff", "s2@0000000000ff", "s3@0000000000ff"]
    assert not (slots.slots_dir / "s0@0000000000ff").exists()  # 最旧被淘汰
    assert (slots.slots_dir / "s3@0000000000ff" / "SKILL.md").is_file()


def test_search_slots_rehit_moves_to_newest_without_duplicate(tmp_path):
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=tmp_path / "home", capacity=3)
    for name in ("a", "b", "c"):
        slots.install(_fake_result(name), _fake_archive(name), query="q")
    slots.install(_fake_result("a"), _fake_archive("a"), query="again")
    ledger = slots.entries()
    assert [s["skill_id"] for s in ledger] == [
        "b@0000000000ff", "c@0000000000ff", "a@0000000000ff"]
    assert len(ledger) == 3
    assert ledger[-1]["query"] == "again"


def test_search_slots_default_capacity_is_ten(tmp_path):
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=tmp_path / "home")
    assert slots.capacity == 10


# ── client: CLI 端到端（TestClient 注入） ───────────────────────

def test_cmd_search_hub_installs_and_prints_paths(hub_env, tmp_path,
                                                  monkeypatch, capsys):
    _cid, hdr = _register(hub_env.client)
    xhome = tmp_path / "cli-xhome"
    monkeypatch.setattr(
        "xskill.team.client.search_slots.SearchSlots",
        lambda **kw: SearchSlots(xskill_home=xhome,
                                 home_root=tmp_path / "cli-home"))
    args = SimpleNamespace(terms=["docker"], top_k=5, json=True)
    assert cli.cmd_search_hub(args, http=hub_env.client, headers=hdr) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["name"] == "docker-helper"
    installed = Path(rows[0]["path"])
    assert (installed / "SKILL.md").is_file()
    assert installed.is_absolute()
    assert (installed / ".xskill_search.json").is_file()


def test_cmd_search_hub_no_match_returns_zero(hub_env, capsys):
    _cid, hdr = _register(hub_env.client)
    args = SimpleNamespace(terms=["nonexistent-topic-xyz"], top_k=5, json=False)
    assert cli.cmd_search_hub(args, http=hub_env.client, headers=hdr) == 0
    assert "无匹配" in capsys.readouterr().out


def test_cmd_upload_end_to_end(hub_env, tmp_path, capsys):
    _cid, hdr = _register(hub_env.client, user_name="carol")
    src = tmp_path / "upload-src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: rust-review\ndescription: Review rust code\n---\nbody\n",
        encoding="utf-8")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    args = SimpleNamespace(path=str(src), json=True)
    assert cli.cmd_upload(args, http=hub_env.client, headers=hdr) == 0
    stored = json.loads(capsys.readouterr().out)
    stored_dir = Path(stored["stored_path"])
    assert (stored_dir / "SKILL.md").is_file()
    assert not (stored_dir / ".git").exists()  # 打包时剔除 .git


def test_cmd_upload_rejects_non_skill_dir(tmp_path, capsys):
    args = SimpleNamespace(path=str(tmp_path), json=False)
    assert cli.cmd_upload(args) == 2
    assert "SKILL.md" in capsys.readouterr().err
