from __future__ import annotations

from fastapi.testclient import TestClient


def _configure_server(monkeypatch, tmp_path):
    from xskill.api import app as srv

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(srv, "_config", {
        "llm": {},
        "embedding": {},
        "watcher": {"poll_interval": 30},
    })
    monkeypatch.setattr(srv, "_skill_dir", skill_dir)
    return srv, skill_dir


def test_status_handles_uninitialized_skill_dir(monkeypatch, tmp_path):
    srv, skill_dir = _configure_server(monkeypatch, tmp_path)

    app = srv.create_app(home_root=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/v1/status")

    assert resp.status_code == 200
    assert resp.json() == {
        "skill_dir": str(skill_dir),
        "skill_count": 0,
        "git_branch": None,
    }


def test_api_skill_search_missing_index_skips_embedding_client(monkeypatch, tmp_path):
    srv, _skill_dir = _configure_server(monkeypatch, tmp_path)
    monkeypatch.setattr(
        srv,
        "create_embed_client",
        lambda _config: (_ for _ in ()).throw(AssertionError("unexpected embed client")),
    )

    app = srv.create_app(home_root=tmp_path)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/skills/search", json={"query": "heartbeat", "top_k": 2})

    assert resp.status_code == 200
    assert resp.json() == []


def test_sdk_skill_search_missing_index_skips_embedding_client(monkeypatch, tmp_path):
    from xskill import core
    from xskill.utils import llm

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(core, "load_config", lambda config_path=None: {"embedding": {}})
    monkeypatch.setattr(core, "get_skill_dir", lambda: skill_dir)
    monkeypatch.setattr(
        llm,
        "create_embed_client",
        lambda _config: (_ for _ in ()).throw(AssertionError("unexpected embed client")),
    )

    xskill = core.XSkill()

    assert xskill.search_skills("heartbeat", top_k=2) == []
