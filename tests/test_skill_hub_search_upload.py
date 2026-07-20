"""`xskill search` / `xskill upload` 的 server 端点 + client 槽位测试。"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from xskill import cli
from xskill.ecosystems._fallback import _install_meta_path, install_dir
from xskill.recommend.skillhub import SkillHub
from xskill.team.client.search_slots import SearchSlots
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry, safe_dir_name

TOKEN = "secret-token"


class _FailingSearchHub:
    enabled = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def cached_search(self, _query: str, _limit: int):
        return None

    def search(self, _query: str, _limit: int):
        raise self.error


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
        # 槽位 100/80 = team_server_slots_config 缺省值,无需另设 live config
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


def test_search_unknown_error_returns_safe_correlated_response(tmp_path, caplog):
    sensitive_error = RuntimeError(
        "database password leaked from /root/private/search-index.db"
    )
    client = _make_team_client(
        tmp_path,
        skillhub=_FailingSearchHub(sensitive_error),
    )
    client_id, headers = _register(client)
    caplog.set_level(logging.ERROR, logger="xskill.server")

    response = client.get(
        "/api/v1/team/skill_hub/search",
        params={"query": "private user search phrase", "limit": 9},
        headers=headers,
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload == {
        "code": "SKILL_HUB_SEARCH_FAILED",
        "message": "服务器执行 SkillHub 搜索时发生异常",
        "request_id": payload["request_id"],
        "retryable": False,
    }
    assert payload["request_id"].startswith("search-")
    assert len(payload["request_id"]) == len("search-") + 16
    assert response.headers["X-Request-ID"] == payload["request_id"]
    assert "password" not in response.text
    assert "/root/private" not in response.text
    assert payload["request_id"] in caplog.text
    assert client_id in caplog.text
    assert "limit=9" in caplog.text
    assert "query_length=26" in caplog.text
    assert "private user search phrase" not in caplog.text
    assert "RuntimeError: database password leaked" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_search_missing_source_returns_503_and_logs_path(tmp_path, caplog):
    missing_path = "/root/private/missing-skillhub"
    client = _make_team_client(
        tmp_path,
        skillhub=_FailingSearchHub(FileNotFoundError(missing_path)),
    )
    client_id, headers = _register(client)
    caplog.set_level(logging.WARNING, logger="xskill.server")

    response = client.get(
        "/api/v1/team/skill_hub/search",
        params={"query": "docker", "limit": 5},
        headers=headers,
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload == {
        "code": "SKILL_HUB_SOURCE_UNAVAILABLE",
        "message": "SkillHub 数据源暂时不可用",
        "request_id": payload["request_id"],
        "retryable": True,
    }
    request_id = response.headers["X-Request-ID"]
    assert request_id == payload["request_id"]
    assert request_id.startswith("search-")
    assert missing_path not in response.text
    assert request_id in caplog.text
    assert client_id in caplog.text
    assert missing_path in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_search_preserves_http_exception_status_detail_and_headers(tmp_path):
    rate_limited = HTTPException(
        status_code=429,
        detail={"code": "SEARCH_RATE_LIMITED", "retry_after": 7},
        headers={"Retry-After": "7"},
    )
    client = _make_team_client(
        tmp_path,
        skillhub=_FailingSearchHub(rate_limited),
    )
    _client_id, headers = _register(client)

    response = client.get(
        "/api/v1/team/skill_hub/search",
        params={"query": "docker"},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.json() == {
        "detail": {"code": "SEARCH_RATE_LIMITED", "retry_after": 7},
    }
    assert response.headers["Retry-After"] == "7"
    assert "X-Request-ID" not in response.headers


# ── server: upload ──────────────────────────────────────────────

def test_upload_stores_under_user_skill_hub_and_is_searchable(hub_env, tmp_path):
    cid, hdr = _register(hub_env.client, user_name="alice")
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


def test_upload_rejects_archive_expanding_beyond_limit(hub_env, monkeypatch):
    _cid, hdr = _register(hub_env.client)
    monkeypatch.setattr(server_api, "_SKILL_ARCHIVE_MAX_TOTAL_BYTES", 64)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md",
                    "---\nname: bomb\ndescription: bomb\n---\n" + "x" * 128)
    response = hub_env.client.post(
        "/api/v1/team/skill_hub/upload",
        files={"file": ("bomb.zip", buf.getvalue(), "application/zip")},
        headers=hdr,
    )
    assert response.status_code == 413


def test_skillhub_bundle_is_deterministic_and_excludes_server_ux_metadata(tmp_path):
    skill_dir = _write_hub_skill(tmp_path / "hub", "pack", "pack", "desc")
    (skill_dir / "references").mkdir()
    asset = skill_dir / "references" / "note.md"
    asset.write_text("stable", encoding="utf-8")
    ux_file = skill_dir / ".ux_scores.jsonl"
    ux_file.write_text("first", encoding="utf-8")
    first = server_api._make_skillhub_archive(skill_dir)

    ux_file.write_text("changed server metadata", encoding="utf-8")
    asset.touch()
    second = server_api._make_skillhub_archive(skill_dir)
    assert second == first

    asset.write_text("new package content", encoding="utf-8")
    assert server_api._make_skillhub_archive(skill_dir) != first


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


def _enable_link_and_copy_ecosystems(home: Path) -> None:
    """启用 Claude Code（link）和 OpenClaw（copy）检测。"""
    (home / ".claude" / "projects").mkdir(parents=True)
    (home / ".openclaw" / "agents").mkdir(parents=True)


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


def test_search_slot_eviction_records_and_removes_link_and_copy_targets(tmp_path):
    home = tmp_path / "home"
    _enable_link_and_copy_ecosystems(home)
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=home, capacity=1)

    old = slots.install(_fake_result("old"), _fake_archive("old"), query="q")
    old_id = old.name
    link_dest = home / ".claude" / "skills" / old_id
    copy_dest = home / ".agents" / "skills" / old_id

    records = slots.entries()[0]["installations"]
    assert {(Path(r["target"]), r["mode"], Path(r["source"])) for r in records} == {
        (link_dest, "symlink", old),
        (copy_dest, "copy", old),
    }

    slots.install(_fake_result("new"), _fake_archive("new"), query="q")

    assert not link_dest.exists() and not link_dest.is_symlink()
    assert not copy_dest.exists()
    assert not _install_meta_path(link_dest).exists()
    assert not _install_meta_path(copy_dest).exists()


def test_search_slot_eviction_does_not_delete_copy_without_sidecar_identity(
    tmp_path,
):
    home = tmp_path / "home"
    _enable_link_and_copy_ecosystems(home)
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=home, capacity=1)

    old = slots.install(_fake_result("legacy"), _fake_archive("legacy"), query="q")
    old_id = old.name
    ledger = slots.entries()
    ledger[0].pop("installations", None)
    slots.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    _install_meta_path(home / ".agents" / "skills" / old_id).unlink()

    slots.install(_fake_result("new"), _fake_archive("new"), query="q")

    assert not (home / ".claude" / "skills" / old_id).exists()
    # copy target 内 marker 单独不能证明所有权；sidecar 已丢失时必须留给用户
    # 手工确认，不能为了兼容旧台账而递归删除。
    assert (home / ".agents" / "skills" / old_id).exists()


def test_search_slot_eviction_preserves_targets_taken_over_by_another_source(tmp_path):
    home = tmp_path / "home"
    _enable_link_and_copy_ecosystems(home)
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=home, capacity=1)

    old = slots.install(_fake_result("shared"), _fake_archive("shared"), query="q")
    old_id = old.name
    link_dest = home / ".claude" / "skills" / old_id
    copy_dest = home / ".agents" / "skills" / old_id
    takeover = tmp_path / "sync" / old_id
    takeover.mkdir(parents=True)
    (takeover / "SKILL.md").write_text("owned by sync\n", encoding="utf-8")
    install_dir(takeover, link_dest, force_mode="symlink", auto_reset=True)
    install_dir(takeover, copy_dest, force_mode="copy", auto_reset=True)

    slots.install(_fake_result("new"), _fake_archive("new"), query="q")

    assert link_dest.resolve() == takeover.resolve()
    assert (copy_dest / "SKILL.md").read_text(encoding="utf-8") == "owned by sync\n"
    assert json.loads(_install_meta_path(link_dest).read_text(encoding="utf-8"))[
        "source"
    ] == str(takeover.resolve())
    assert json.loads(_install_meta_path(copy_dest).read_text(encoding="utf-8"))[
        "source"
    ] == str(takeover.resolve())


def test_search_slot_records_every_detected_ecosystem_target(tmp_path):
    home = tmp_path / "home"
    _enable_link_and_copy_ecosystems(home)
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".cac" / "projects").mkdir(parents=True)
    (home / ".cursor" / "projects").mkdir(parents=True)
    opencode_db = home / ".local" / "share" / "opencode" / "opencode.db"
    opencode_db.parent.mkdir(parents=True)
    opencode_db.touch()
    ngagent_db = opencode_db.parent / "db" / "ngagent.db"
    ngagent_db.parent.mkdir()
    ngagent_db.touch()
    (home / ".trae-cn").mkdir()
    (home / ".trae").mkdir()
    slots = SearchSlots(xskill_home=tmp_path / "xhome", home_root=home)

    installed = slots.install(
        _fake_result("all-targets"), _fake_archive("all-targets"), query="q",
    )

    records = slots.entries()[0]["installations"]
    by_target = {Path(r["target"]): (r["mode"], Path(r["source"])) for r in records}
    skill_id = installed.name
    assert by_target == {
        home / ".claude" / "skills" / skill_id: ("symlink", installed),
        home / ".agents" / "skills" / skill_id: ("copy", installed),
        home / ".cac" / "skills" / skill_id: ("symlink", installed),
        home / ".config" / "opencode" / "skills" / skill_id: ("copy", installed),
        home / ".cursor" / "skills" / skill_id: ("symlink", installed),
        home / ".trae-cn" / "skills" / skill_id: ("symlink", installed),
        home / ".trae" / "skills" / skill_id: ("symlink", installed),
    }


def test_search_slot_trae_windows_copy_targets_are_recorded_and_evicted(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / ".trae-cn").mkdir(parents=True)
    (home / ".trae").mkdir()
    monkeypatch.setattr("xskill.ecosystems.trae.sys.platform", "win32")
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=home, capacity=1)

    old = slots.install(_fake_result("win"), _fake_archive("win"), query="q")
    records = slots.entries()[0]["installations"]
    assert {(Path(r["target"]), r["mode"], Path(r["source"])) for r in records} == {
        (home / ".trae-cn" / "skills" / old.name, "copy", old),
        (home / ".trae" / "skills" / old.name, "copy", old),
    }

    slots.install(_fake_result("new"), _fake_archive("new"), query="q")

    assert not (home / ".trae-cn" / "skills" / old.name).exists()
    assert not (home / ".trae" / "skills" / old.name).exists()


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


def test_search_slots_refreshes_auxiliary_files_when_skill_md_sha_is_unchanged(tmp_path):
    slots = SearchSlots(xskill_home=tmp_path / "xhome",
                        home_root=tmp_path / "home", capacity=3)
    result = _fake_result("aux-refresh")

    def archive_with_note(note: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md",
                        "---\nname: aux-refresh\ndescription: d\n---\nbody\n")
            zf.writestr("references/note.md", note)
        return buf.getvalue()

    installed = slots.install(result, archive_with_note("old"), query="q")
    first_marker = json.loads((installed / ".xskill_search.json").read_text(
        encoding="utf-8"))
    slots.install(result, archive_with_note("new"), query="q")
    second_marker = json.loads((installed / ".xskill_search.json").read_text(
        encoding="utf-8"))

    assert (installed / "references" / "note.md").read_text(encoding="utf-8") == "new"
    assert first_marker["sha"] == second_marker["sha"] == result["content_sha"]
    assert first_marker["archive_sha"] != second_marker["archive_sha"]


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


# ── client: 网络异常兜底 + 展示字段 ─────────────────────────────

class _FakeResp:
    def __init__(self, status_code, *, json_data=None, content=b""):
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        return self._json


def test_cmd_search_hub_network_error_returns_one_without_traceback(capsys):
    import httpx

    class _BoomHttp:
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    args = SimpleNamespace(terms=["docker"], top_k=5, json=False)
    rc = cli.cmd_search_hub(args, http=_BoomHttp(), headers={})
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "ConnectError" in err
    assert "管理员" in err or "网络" in err


def test_cmd_upload_network_error_returns_one_without_traceback(tmp_path, capsys):
    import httpx

    class _BoomHttp:
        def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout("timed out")

    src = tmp_path / "up-src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: net-skill\ndescription: d\n---\nbody\n", encoding="utf-8")
    args = SimpleNamespace(path=str(src), json=False)
    rc = cli.cmd_upload(args, http=_BoomHttp(), headers={})
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "ConnectTimeout" in err
    assert "管理员" in err or "网络" in err


def test_cmd_search_hub_renders_source_and_ux_defensively(tmp_path, monkeypatch,
                                                          capsys):
    class _FakeHttp:
        def get(self, path, **kwargs):
            if "search" in path:
                return _FakeResp(200, json_data={"results": [
                    {"skill_id": "with@ff", "display_name": "with-meta",
                     "description": "has metadata", "content_sha": "aa",
                     "source_path": "p1", "source": "上传者:alice",
                     "ux_avg": 4.2, "match": {"field": "name"}},
                    {"skill_id": "bare@ff", "display_name": "bare-meta",
                     "description": "no metadata", "content_sha": "bb",
                     "source_path": "p2"},
                ]})
            return _FakeResp(200, content=_fake_archive("x"))

    monkeypatch.setattr(
        "xskill.team.client.search_slots.SearchSlots",
        lambda **kw: SearchSlots(xskill_home=tmp_path / "xh",
                                 home_root=tmp_path / "h"))
    args = SimpleNamespace(terms=["anything"], top_k=5, json=False)
    assert cli.cmd_search_hub(args, http=_FakeHttp(), headers={}) == 0
    out = capsys.readouterr().out
    assert "ux 4.2" in out
    assert "来源: 上传者:alice" in out
    # 缺 source/ux_avg 的命中正常渲染，其名字行不带 ux/来源 后缀，不报错
    bare_line = next(line for line in out.splitlines() if "bare-meta" in line)
    assert "ux" not in bare_line and "来源" not in bare_line


def test_cmd_search_hub_json_passes_through_all_fields(tmp_path, monkeypatch,
                                                       capsys):
    class _FakeHttp:
        def get(self, path, **kwargs):
            if "search" in path:
                return _FakeResp(200, json_data={"results": [
                    {"skill_id": "j@ff", "display_name": "json-skill",
                     "description": "d", "content_sha": "cc", "source_path": "p",
                     "source": "skillhub", "ux_avg": None,
                     "match": {"field": "description"}},
                ]})
            return _FakeResp(200, content=_fake_archive("x"))

    monkeypatch.setattr(
        "xskill.team.client.search_slots.SearchSlots",
        lambda **kw: SearchSlots(xskill_home=tmp_path / "xh",
                                 home_root=tmp_path / "h"))
    args = SimpleNamespace(terms=["anything"], top_k=5, json=True)
    assert cli.cmd_search_hub(args, http=_FakeHttp(), headers={}) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["source"] == "skillhub"
    assert row["match"] == {"field": "description"}
    assert row["name"] == "json-skill" and row["path"]
