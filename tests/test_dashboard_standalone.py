"""块 1 独立只读看板进程:serve_builtin=False 只挂只读路由,不挂 auth/console/敏感端点。"""
from __future__ import annotations

from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard.mount import mount_dashboard
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import get_connection

_CFG = {"dashboard": {"enabled": True, "public": True}}

_STANDALONE_ALLOWED_OPERATIONS = {
    ("get", "/"),
    ("get", "/app.js"),
    ("get", "/api/v1/dashboard/overview"),
    ("get", "/api/v1/dashboard/by-domain"),
    ("get", "/api/v1/dashboard/rates"),
    ("get", "/api/v1/dashboard/cost"),
    ("get", "/api/v1/dashboard/models"),
    ("get", "/api/v1/dashboard/dirs"),
    ("get", "/api/v1/dashboard/canary"),
    ("get", "/api/v1/dashboard/tags"),
    ("get", "/api/v1/dashboard/skills"),
    ("get", "/api/v1/dashboard/skill/{name}/ux/daily"),
    ("get", "/api/v1/dashboard/pipeline"),
    ("get", "/api/v1/dashboard/skill/{name}/ux"),
    ("get", "/api/v1/dashboard/skillhub/{name}/ux"),
    ("get", "/api/v1/dashboard/skill/{name}/trigger"),
}

_BUILTIN_ONLY_OPERATIONS = {
    # 用户、原始轨迹、原子、skill 内容及逐 case 数据。
    ("get", "/api/v1/dashboard/users"),
    ("get", "/api/v1/dashboard/skill/{name}/detail"),
    ("get", "/api/v1/dashboard/skill/{name}/graph"),
    ("get", "/api/v1/dashboard/skill/{name}/lineage"),
    ("get", "/api/v1/dashboard/skill/{name}/tree"),
    ("get", "/api/v1/dashboard/skill/{name}/file"),
    ("get", "/api/v1/dashboard/skill/{name}/diff"),
    ("get", "/api/v1/dashboard/skill/{name}/ux/atoms"),
    ("get", "/api/v1/dashboard/skillhub/{name}/ux/atoms"),
    ("get", "/api/v1/dashboard/skill/{name}/trigger/cases"),
    ("post", "/api/v1/dashboard/skill/{name}/trigger/rerun"),
    ("get", "/api/v1/dashboard/traj/{traj_id}"),
    ("get", "/api/v1/dashboard/traj/{traj_id}/atoms"),
    ("get", "/api/v1/dashboard/traj/{traj_id}/atom/{atom_id}"),
    ("get", "/api/v1/dashboard/users/status"),
    ("get", "/api/v1/dashboard/user/{user_key}/scatter"),
    # 登录、会话及控制台端点。
    ("post", "/api/v1/dashboard/login"),
    ("get", "/api/v1/dashboard/login/link"),
    ("post", "/api/v1/dashboard/logout"),
    ("get", "/api/v1/dashboard/me"),
    ("get", "/api/v1/dashboard/my/manifest"),
    ("post", "/api/v1/dashboard/my/prefs"),
    ("get", "/api/v1/dashboard/my/contributions"),
    ("get", "/api/v1/dashboard/my/reco-trigger"),
    ("get", "/api/v1/dashboard/events"),
    ("get", "/api/v1/dashboard/events/unread"),
    ("post", "/api/v1/dashboard/events/read"),
    ("get", "/api/v1/dashboard/admin/users-matrix"),
    ("put", "/api/v1/dashboard/admin/client/{client_id}/ingest"),
    ("get", "/api/v1/dashboard/admin/user/{user_key}/prefs"),
    ("post", "/api/v1/dashboard/admin/prefs"),
    ("get", "/api/v1/dashboard/admin/cluster-graph"),
    ("get", "/api/v1/dashboard/admin/skills"),
    ("post", "/api/v1/dashboard/admin/skill/{name}/retire"),
    ("post", "/api/v1/dashboard/admin/skill/{name}/unretire"),
    ("delete", "/api/v1/dashboard/admin/skill/{name}"),
    ("get", "/api/v1/dashboard/admin/config"),
    ("post", "/api/v1/dashboard/admin/config/validate"),
    ("post", "/api/v1/dashboard/admin/config/reload"),
    ("get", "/api/v1/dashboard/admin/kernels"),
    ("get", "/api/v1/dashboard/admin/kernels/runs"),
    ("get", "/api/v1/dashboard/admin/kernels/export"),
    ("get", "/api/v1/dashboard/admin/kernels/logs"),
    ("post", "/api/v1/dashboard/admin/kernels/activate"),
}


def _build(tmp_path, *, serve_builtin: bool) -> FastAPI:
    db = tmp_path / "r.db"
    get_connection(db).close()
    app = FastAPI()
    mount_dashboard(app, _CFG, serve_builtin=serve_builtin, db_path=db)
    return app


def test_standalone_serves_readonly_aggregate(tmp_path):
    app = _build(tmp_path, serve_builtin=False)
    assert TestClient(app).get("/api/v1/dashboard/overview").status_code == 200


def test_standalone_hides_sensitive_and_write_routes(tmp_path, monkeypatch):
    skill_directory = tmp_path / "skill" / "demo"
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: private description\n"
        "metadata:\n  version: 7\n---\nprivate skill",
        encoding="utf-8",
    )
    watch_directory = tmp_path / "private-client-trajectories"
    watch_directory.mkdir()
    AtomTaskStore(root=watch_directory).save(AtomTask(
        atom_id="atom_private_0001",
        traj_id="private",
        offset_start=1,
        offset_end=2,
        intent="private intent",
        summary="private summary",
        tags=["private-tag"],
        used_skills=[],
        ux_score=7,
        pre_atom_id=None,
        post_atom_id=None,
        context_prefix="",
        raw_segment="",
    ))
    monkeypatch.setattr(
        "xskill.dashboard.router._build_skillhub",
        Mock(return_value=[{
            "display_name": "external-demo",
            "name": "external-demo",
            "source_path": "/private/skillhub/source",
            "skill_id": "external-demo@private",
            "description": "private external description",
            "use_count": 3,
        }]),
    )
    app = _build(tmp_path, serve_builtin=False)
    connection = get_connection(tmp_path / "r.db")
    connection.execute(
        "INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
        (str(watch_directory), "private-client-id", "team_client"),
    )
    connection.commit()
    connection.close()
    openapi_paths = app.openapi()["paths"]
    standalone_operations = {
        (method, path)
        for path, operation_schema in openapi_paths.items()
        for method in operation_schema
        if method != "parameters"
    }
    assert standalone_operations == _STANDALONE_ALLOWED_OPERATIONS
    assert all(method in {"get", "head", "options"}
               for method, _path in standalone_operations)

    client = TestClient(app)
    tags_response = client.get("/api/v1/dashboard/tags")
    assert tags_response.json() == {
        "tags": [{"tag": "private-tag", "count": 1}],
    }
    dirs_response = client.get("/api/v1/dashboard/dirs")
    assert dirs_response.json() == {
        "dirs": [{
            "ecosystem": "team_client",
            "traj_count": 0,
            "indexed_count": 0,
        }],
    }
    skills_response = client.get("/api/v1/dashboard/skills")
    assert skills_response.json() == {
        "total": 2,
        "by_state": {"unknown": 1, "skillhub": 1},
        "offset": 0,
        "limit": 0,
        "skills": [
            {
                "name": "demo",
                "state": "unknown",
                "source": "native",
                "version": 7,
                "candidates": 0,
            },
            {
                "name": "external-demo",
                "state": "skillhub",
                "source": "skillhub",
                "version": 0,
                "candidates": 0,
            },
        ],
    }
    standalone_payload = (
        tags_response.text + dirs_response.text + skills_response.text
    )
    for private_value in (
        "private-client-id",
        str(watch_directory),
        "private description",
        "private external description",
        "/private/skillhub/source",
        "external-demo@private",
    ):
        assert private_value not in standalone_payload
    assert client.get("/api/v1/dashboard/users").status_code == 404
    assert client.get(
        "/api/v1/dashboard/skill/demo/file",
        params={"path": "SKILL.md"},
    ).status_code == 404
    assert client.get(
        "/api/v1/dashboard/skill/demo/trigger/cases",
    ).status_code == 404
    assert client.post(
        "/api/v1/dashboard/skill/demo/trigger/rerun",
    ).status_code == 404
    assert client.post("/api/v1/dashboard/login", json={}).status_code == 404
    assert client.post("/api/v1/dashboard/my/prefs", json={}).status_code == 404


def test_serve_builtin_mounts_sensitive_and_auth(tmp_path, monkeypatch):
    """对照:serve_builtin=True(api 进程形态)才挂敏感端点与登录路由。"""
    skill_directory = tmp_path / "skill" / "demo"
    skill_directory.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text(
        "---\nname: demo\ndescription: private description\n"
        "metadata:\n  version: 7\n---\nprivate skill",
        encoding="utf-8",
    )
    watch_directory = tmp_path / "private-client-trajectories"
    watch_directory.mkdir()
    AtomTaskStore(root=watch_directory).save(AtomTask(
        atom_id="atom_private_0001",
        traj_id="private",
        offset_start=1,
        offset_end=2,
        intent="private intent",
        summary="private summary",
        tags=["private-tag"],
        used_skills=[],
        ux_score=7,
        pre_atom_id=None,
        post_atom_id=None,
        context_prefix="",
        raw_segment="",
    ))
    monkeypatch.setattr(
        "xskill.dashboard.router._build_skillhub",
        Mock(return_value=[{
            "display_name": "external-demo",
            "name": "external-demo",
            "source_path": "/private/skillhub/source",
            "skill_id": "external-demo@private",
            "description": "private external description",
            "use_count": 3,
        }]),
    )
    app = _build(tmp_path, serve_builtin=True)
    connection = get_connection(tmp_path / "r.db")
    connection.execute(
        "INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
        (str(watch_directory), "private-client-id", "team_client"),
    )
    connection.commit()
    connection.close()
    openapi_paths = app.openapi()["paths"]
    builtin_operations = {
        (method, path)
        for path, operation_schema in openapi_paths.items()
        for method in operation_schema
        if method != "parameters"
    }
    assert builtin_operations == (
        _STANDALONE_ALLOWED_OPERATIONS | _BUILTIN_ONLY_OPERATIONS
    )

    client = TestClient(app)
    assert client.get("/api/v1/dashboard/tags").json()["tags"] == [{
        "tag": "private-tag",
        "count": 1,
        "users": ["private-client-id"],
    }]
    assert client.get("/api/v1/dashboard/dirs").json()["dirs"] == [{
        "ecosystem": "team_client",
        "path": str(watch_directory),
        "label": "private-client-id",
        "traj_count": 0,
        "indexed_count": 0,
    }]
    builtin_skills = client.get("/api/v1/dashboard/skills").json()["skills"]
    assert builtin_skills[0]["description"] == "private description"
    assert builtin_skills[0]["source"] == "native"
    assert builtin_skills[1]["description"] == "private external description"
    assert builtin_skills[1]["source"] == "skillhub"
    assert builtin_skills[1]["hub"] == "/private/skillhub/source"
    assert client.get("/api/v1/dashboard/users").status_code == 200
    file_response = client.get(
        "/api/v1/dashboard/skill/demo/file",
        params={"path": "SKILL.md"},
    )
    assert file_response.status_code == 200
    assert "description: private description" in file_response.json()["content"]
    assert file_response.json()["content"].endswith("private skill")
    assert client.get(
        "/api/v1/dashboard/skill/demo/trigger/cases",
    ).status_code == 200
    assert client.post(
        "/api/v1/dashboard/skill/demo/trigger/rerun",
    ).status_code == 422
    assert client.post("/api/v1/dashboard/login", json={}).status_code == 422
    assert client.post("/api/v1/dashboard/my/prefs", json={}).status_code == 401
