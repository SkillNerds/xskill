"""#213 xskill import 落仓：四种情形、体验分禁令、stash、upload 不变。"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.canary import has_staging, load_ux_scores, main_sha
from xskill.skill.git import (
    commit_to_staging_branch,
    commit_update_main_branch,
    current_branch,
    init_imported_repo_on_main,
    init_skill_repo_on_baby,
    run_git,
)
from xskill.skill import catalog_store
from xskill.skill.importer import (
    HARNESS_IMPORT_WARNING,
    discover_import_sources,
    import_one_skill,
    import_skill_path,
    is_harness_skill_path,
    pack_import_zip,
    source_has_dirty_or_untracked,
    stash_import_dir,
)
from xskill.skill.scripting import (
    has_scripting_request,
    request_scripting,
    scripting_gate_reason,
    scripting_status,
)
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry


TOKEN = "secret-token"


@pytest.fixture(autouse=True)
def _isolate_import_registry(tmp_path, monkeypatch):
    """import 会按 get_registry_db_path() 写投影表，测试不得碰到本机 registry。"""
    registry = tmp_path / "registry.db"
    monkeypatch.setattr(
        "xskill.config.get_registry_db_path",
        lambda: registry,
    )
    return registry


def _catalog_names(skill_dir: Path, registry: Path) -> list[str]:
    return [
        row["name"]
        for row in catalog_store.list_skills_catalog(skill_dir, db_path=registry)
    ]


def _skill_md(name: str, body: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {name} helper\n---\n{body}\n"
    )


def _write_skill(path: Path, name: str, body: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(_skill_md(name, body), encoding="utf-8")
    return path


def _log_subjects(repo: Path, n: int = 20) -> str:
    code, out, err = run_git(["log", "--oneline", f"-{n}"], cwd=str(repo))
    assert code == 0, err
    return out


def _write_ux_row(skill_dir: Path, *, side: str, sha: str, traj_id: str,
                  scored_at: str, score: float = 8.0) -> None:
    p = skill_dir / ".ux_scores.jsonl"
    rec = {
        "traj_id": traj_id,
        "skill_name": skill_dir.name,
        "side": side,
        "commit_sha": sha,
        "score": score,
        "reasons": "t",
        "scored_at": scored_at,
    }
    with p.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_discover_parent_and_single(tmp_path):
    parent = tmp_path / "skills"
    _write_skill(parent / "alpha", "alpha", "# a")
    _write_skill(parent / "beta", "beta", "# b")
    (parent / "notes.txt").write_text("no", encoding="utf-8")
    found = discover_import_sources(parent)
    assert [p.name for p in found] == ["alpha", "beta"]
    assert discover_import_sources(parent / "alpha") == [parent / "alpha"]


def test_standalone_new_skill_keeps_source_git_history(tmp_path):
    source = _write_skill(tmp_path / "src" / "foo", "foo", "# v1")
    init_imported_repo_on_main(source, "first commit")
    (source / "SKILL.md").write_text(_skill_md("foo", "# v2"), encoding="utf-8")
    commit_update_main_branch(str(source), "second commit")
    skill_dir = tmp_path / "skill"
    imported = import_one_skill(skill_dir, source)
    dest = skill_dir / "foo"
    assert imported.existed is False
    assert current_branch(str(dest)) == "main"
    subjects = _log_subjects(dest)
    assert "first commit" in subjects
    assert "second commit" in subjects
    assert "# v2" in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_standalone_new_skill_without_git_starts_on_main(tmp_path):
    source = _write_skill(tmp_path / "src" / "bar", "bar", "# only")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    skill_dir = tmp_path / "skill"
    imported = import_one_skill(skill_dir, source)
    dest = skill_dir / "bar"
    assert imported.sha
    assert current_branch(str(dest)) == "main"
    assert (dest / "scripts" / "run.sh").is_file()
    assert (dest / ".git").is_dir()


def test_import_new_skill_appears_in_catalog_after_backfill(
    tmp_path, _isolate_import_registry,
):
    """灌过投影表之后再 import：磁盘已有，看板列表仍应出现新名字。"""
    registry = _isolate_import_registry
    skill_dir = tmp_path / "skill"
    old = _write_skill(skill_dir / "old", "old", "# already there")
    init_imported_repo_on_main(old, "seed")
    catalog_store.ensure_skills_catalog(skill_dir, db_path=registry)
    assert _catalog_names(skill_dir, registry) == ["old"]

    source = _write_skill(tmp_path / "src" / "fresh", "fresh", "# brand new")
    imported = import_one_skill(skill_dir, source)
    assert imported.existed is False
    page = catalog_store.page_skills_catalog(
        skill_dir, db_path=registry, name="fresh",
    )
    assert page["total"] == 1
    assert page["skills"][0]["name"] == "fresh"
    assert page["skills"][0]["state"] == "main"
    assert "fresh helper" in page["skills"][0]["description"]
    assert _catalog_names(skill_dir, registry) == ["fresh", "old"]


def test_import_nothing_to_commit_still_upserts_catalog(
    tmp_path, _isolate_import_registry,
):
    """同名同内容、git 无新提交时，漏掉的投影行也要补上。"""
    registry = _isolate_import_registry
    skill_dir = tmp_path / "skill"
    dest = _write_skill(skill_dir / "foo", "foo", "# same")
    init_imported_repo_on_main(dest, "already")
    catalog_store.ensure_skills_catalog(skill_dir, db_path=registry)
    catalog_store.delete_native_skill("foo", db_path=registry)
    assert _catalog_names(skill_dir, registry) == []

    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# same")
    imported = import_one_skill(skill_dir, source)
    assert imported.existed is True
    page = catalog_store.page_skills_catalog(
        skill_dir, db_path=registry, name="foo",
    )
    assert page["total"] == 1
    assert page["skills"][0]["name"] == "foo"


def test_standalone_existing_replaces_files_keeps_history_and_sidecars(tmp_path):
    skill_dir = tmp_path / "skill"
    dest = _write_skill(skill_dir / "foo", "foo", "# old body")
    init_imported_repo_on_main(dest, "original main")
    (dest / ".ux_scores.jsonl").write_text(
        json.dumps({"traj_id": "old", "skill_name": "foo", "side": "main",
                    "commit_sha": "abc", "score": 9, "scored_at": "2020-01-01T00:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    (dest / ".candidates.yml").write_text("candidates: []\n", encoding="utf-8")
    old_sha = main_sha(dest)

    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# new body only")
    (source / "scripts").mkdir()
    (source / "scripts" / "do.py").write_text("print(1)\n", encoding="utf-8")
    imported = import_one_skill(skill_dir, source)
    assert imported.existed is True
    text = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "# new body only" in text
    assert "# old body" not in text
    assert (dest / "scripts" / "do.py").is_file()
    subjects = _log_subjects(dest)
    assert "original main" in subjects
    assert main_sha(dest) != old_sha
    scores = load_ux_scores(dest)
    assert any(row.get("traj_id") == "old" for row in scores)
    assert (dest / ".candidates.yml").read_text(encoding="utf-8") == "candidates: []\n"


def test_import_overwrites_baby_without_error(tmp_path, caplog):
    import logging
    skill_dir = tmp_path / "skill"
    dest = skill_dir / "foo"
    dest.mkdir(parents=True)
    init_skill_repo_on_baby(str(dest), name="foo", description="draft")
    assert current_branch(str(dest)) == "baby"
    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# finished")
    with caplog.at_level(logging.INFO, logger="xskill.skill.importer"):
        imported = import_one_skill(skill_dir, source)
    assert imported.baby_overwritten is True
    assert current_branch(str(dest)) == "main"
    assert "# finished" in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "overwriting baby" in caplog.text


def test_same_name_with_staging_clears_only_current_main_round(tmp_path):
    skill_dir = tmp_path / "skill"
    dest = _write_skill(skill_dir / "foo", "foo", "# main v1")
    init_imported_repo_on_main(dest, "older main")
    older = main_sha(dest)
    (dest / "SKILL.md").write_text(_skill_md("foo", "# main v2"), encoding="utf-8")
    commit_update_main_branch(str(dest), "current main")
    current = main_sha(dest)
    (dest / "SKILL.md").write_text(_skill_md("foo", "# staging cand"), encoding="utf-8")
    assert commit_to_staging_branch(str(dest), "staging candidate") is True
    staging = run_git(["rev-parse", "staging"], cwd=str(dest))[1].strip()
    assert has_staging(dest)

    for i in range(5):
        _write_ux_row(
            dest, side="main", sha=current, traj_id=f"cur{i}",
            scored_at=f"2026-08-01T00:0{i}:00+00:00",
        )
    _write_ux_row(
        dest, side="main", sha=older, traj_id="hist",
        scored_at="2026-07-01T00:00:00+00:00", score=7.0,
    )
    _write_ux_row(
        dest, side="staging", sha=staging, traj_id="stg1",
        scored_at="2026-08-02T00:00:00+00:00", score=6.0,
    )

    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# imported")
    imported = import_one_skill(skill_dir, source)
    assert imported.staging_kept is True
    assert imported.main_round_scores_cleared == 5
    assert has_staging(dest)
    rows = load_ux_scores(dest)
    main_current = [
        r for r in rows if r.get("side") == "main" and r.get("commit_sha") == current
    ]
    assert main_current == []
    assert any(r.get("traj_id") == "hist" for r in rows)
    assert any(r.get("traj_id") == "stg1" for r in rows)
    assert "# imported" in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "original main" in _log_subjects(dest) or "older main" in _log_subjects(dest)


def test_harness_path_and_stash(tmp_path):
    home = tmp_path / "home"
    source = _write_skill(home / ".claude" / "skills" / "foo", "foo", "# h")
    init_imported_repo_on_main(source, "tracked")
    (source / "dirty.txt").write_text("untracked", encoding="utf-8")
    assert is_harness_skill_path(source, home_root=home)
    assert source_has_dirty_or_untracked(source)
    stash = stash_import_dir(source, "foo", home_root=tmp_path / "xskill")
    assert (stash / "dirty.txt").read_text(encoding="utf-8") == "untracked"
    assert (source / "dirty.txt").is_file()
    elsewhere = _write_skill(tmp_path / "other" / "foo", "foo", "# e")
    assert not is_harness_skill_path(elsewhere, home_root=home)


def test_import_skill_path_warns_only_for_harness(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xhome = tmp_path / "xskill"
    skill_dir = xhome / "skill"
    harness = _write_skill(home / ".claude" / "skills" / "foo", "foo", "# h")
    init_imported_repo_on_main(harness, "h1")
    (harness / "wip.txt").write_text("dirty", encoding="utf-8")
    results = import_skill_path(
        skill_dir, harness, install=False, home_root=home, stash_home=xhome,
    )
    assert HARNESS_IMPORT_WARNING in results[0].warnings
    assert results[0].stash_path
    assert Path(results[0].stash_path).is_dir()

    other = _write_skill(tmp_path / "disk" / "bar", "bar", "# o")
    results2 = import_skill_path(
        skill_dir, other, install=False, home_root=home, stash_home=xhome,
    )
    assert results2[0].warnings == []
    assert results2[0].stash_path == ""
    assert other.is_dir()


def test_cli_import_has_no_agents_flag():
    from xskill.cli import build_parser
    args = build_parser().parse_args(["import", "/tmp/foo"])
    assert args.command == "import"
    assert args.path == "/tmp/foo"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import", "/tmp/foo", "--agents", "claude-code"])


def test_cmd_import_standalone(tmp_path, monkeypatch, capsys):
    from xskill import cli
    from xskill.skill.importer import ImportResult

    skill_dir = tmp_path / "skill"
    source = _write_skill(tmp_path / "incoming" / "zz", "zz", "# z")
    monkeypatch.setattr(cli, "_is_thin_team_client", lambda: False)
    monkeypatch.setattr(cli, "_local_import_skill_dir", lambda: skill_dir)
    monkeypatch.setattr(
        "xskill.team.client.daemon.install_skill_to_ecosystems",
        lambda *a, **k: [],
    )
    args = SimpleNamespace(path=str(source), json=False)
    assert cli.cmd_import(args) == 0
    out = capsys.readouterr().out
    assert "imported: zz" in out
    assert (skill_dir / "zz" / "SKILL.md").is_file()
    assert isinstance(ImportResult(name="zz", existed=False), ImportResult)


def _team_client(tmp_path: Path) -> TestClient:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    server_api.init_team_context(
        join_token=TOKEN,
        client_registry=ClientRegistry(tmp_path / "clients.db"),
        skill_dir=skill_dir,
        traj_root=tmp_path / "team_traj",
        register_dir=lambda path, label: None,
        skillhub=None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app)


def _register(client: TestClient) -> dict:
    r = client.post("/api/v1/team/register", json={
        "token": TOKEN, "client_label": "t", "hostname": "h",
    })
    assert r.status_code == 200
    return {"X-Xskill-Token": TOKEN, "X-Xskill-Client": r.json()["client_id"]}


def test_team_import_new_skill_keeps_history_not_skillhub(
    tmp_path, _isolate_import_registry,
):
    client = _team_client(tmp_path)
    hdr = _register(client)
    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# v1")
    init_imported_repo_on_main(source, "source first")
    (source / "SKILL.md").write_text(_skill_md("foo", "# v2"), encoding="utf-8")
    commit_update_main_branch(str(source), "source second")
    payload = pack_import_zip(source, include_git=True)
    r = client.post(
        "/api/v1/team/skills/import",
        files={"file": ("foo.zip", payload, "application/zip")},
        data={"name": "foo"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["existed"] is False
    dest = tmp_path / "skill" / "foo"
    assert (dest / "SKILL.md").is_file()
    subjects = _log_subjects(dest)
    assert "source first" in subjects
    assert "foo" in _catalog_names(tmp_path / "skill", _isolate_import_registry)
    upload = client.post(
        "/api/v1/team/skill_hub/upload",
        files={"file": ("foo.zip", payload, "application/zip")},
        headers=hdr,
    )
    assert upload.status_code == 503


def test_team_import_existing_keeps_server_git(tmp_path):
    client = _team_client(tmp_path)
    hdr = _register(client)
    dest = _write_skill(tmp_path / "skill" / "foo", "foo", "# server old")
    init_imported_repo_on_main(dest, "server original")
    source = _write_skill(tmp_path / "incoming" / "foo", "foo", "# from client")
    init_imported_repo_on_main(source, "client history should not overlay")
    r = client.post(
        "/api/v1/team/skills/import",
        files={"file": ("foo.zip", pack_import_zip(source), "application/zip")},
        data={"name": "foo"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["existed"] is True
    subjects = _log_subjects(dest)
    assert "server original" in subjects
    text = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "# from client" in text
    assert "# server old" not in text


def test_upload_still_strips_git(tmp_path):
    """对照：upload 打包仍应丢掉 .git（与 import 的 pack_import_zip 不同）。"""
    from xskill import cli
    skill_dir = tmp_path / "s"
    _write_skill(skill_dir, "keep-git", "# body")
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    args = SimpleNamespace(path=str(skill_dir), json=False)

    class _Capture:
        def __init__(self):
            self.files = None

        def post(self, url, files=None, headers=None):
            self.files = files
            self.url = url
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "display_name": "keep-git",
                    "skill_id": "id",
                    "stored_path": "/hub/keep-git",
                },
                text="",
            )

    cap = _Capture()
    assert cli.cmd_upload(args, http=cap, headers={}) == 0
    assert cap.url.endswith("/skill_hub/upload")
    payload = cap.files["file"][1]
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
    assert "SKILL.md" in names
    assert not any(n.startswith(".git/") or n == ".git" for n in names)


def test_scripting_button_gates(tmp_path):
    dest = _write_skill(tmp_path / "skill" / "foo", "foo", "# main")
    init_imported_repo_on_main(dest, "main1")
    status = scripting_status(dest)
    assert status["enabled"] is True
    request_scripting(dest)
    assert has_scripting_request(dest)
    assert scripting_status(dest)["enabled"] is False

    baby = tmp_path / "skill" / "baby"
    baby.mkdir(parents=True)
    init_skill_repo_on_baby(str(baby), name="baby", description="d")
    assert "预备分支" in scripting_gate_reason(baby)
    with pytest.raises(ValueError):
        request_scripting(baby)

    staged = _write_skill(tmp_path / "skill" / "stg", "stg", "# m")
    init_imported_repo_on_main(staged, "m")
    (staged / "SKILL.md").write_text(_skill_md("stg", "# s"), encoding="utf-8")
    assert commit_to_staging_branch(str(staged), "cand")
    assert "灰度" in scripting_gate_reason(staged)
