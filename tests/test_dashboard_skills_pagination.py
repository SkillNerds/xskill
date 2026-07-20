"""海量 skill 分页:/skills 支持 limit/offset 分页 + name 定向查,total/by_state 按全量。

分页 / 计数 / 深拷贝隔离都走真实的 :func:`skills_catalog_page` + 缓存 bundle
（审计 L9），测试只在最底层磁盘扫描处注入假清单，让真实读路径全程被覆盖。
"""
from __future__ import annotations

from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard import metrics as metrics_mod
from xskill.dashboard import router as router_mod
from xskill.dashboard.mount import mount_dashboard
from xskill.dashboard.router import build_dashboard_router
from xskill.pipeline.registry import get_connection


def _fake_catalog(n):
    return [
        {
            "name": f"s{i}",
            "state": "main" if i % 2 else "staging",
            "version": "1",
            "candidates": 0,
            "source": "native",
            "description": "",
        }
        for i in range(n)
    ]


def _client(tmp_path, monkeypatch, n):
    db = tmp_path / "r.db"
    get_connection(db).close()
    app = FastAPI()
    mount_dashboard(app, {"dashboard": {"enabled": True, "public": True}}, db_path=db)

    # 注入到最底层磁盘扫描：真实的缓存 bundle + 分页 + 计数 + 单页深拷贝全程被覆盖。
    # 每个用例用独立 tmp_path/skill 作缓存键，天然隔离不串缓存。
    def fake_build(*_args, **_kwargs):
        return _fake_catalog(n)

    monkeypatch.setattr(metrics_mod, "_build_skills_catalog_uncached", fake_build)
    return TestClient(app)


def test_default_returns_all_backward_compatible(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, 250).get("/api/v1/dashboard/skills").json()
    assert body["total"] == 250
    assert len(body["skills"]) == 250  # limit=0 默认返回全部(向后兼容)


def test_limit_offset_returns_one_page(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, 250).get(
        "/api/v1/dashboard/skills?limit=100&offset=100").json()
    assert body["total"] == 250  # total 仍按全量
    assert len(body["skills"]) == 100
    assert body["skills"][0]["name"] == "s100"
    assert body["offset"] == 100 and body["limit"] == 100


def test_by_state_counts_full_catalog_not_page(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, 250).get(
        "/api/v1/dashboard/skills?limit=10").json()
    assert sum(body["by_state"].values()) == 250  # 概览计数按全量,不受分页影响


def test_name_filter_returns_single_skill(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch, 10000).get(
        "/api/v1/dashboard/skills?name=s4242").json()
    assert len(body["skills"]) == 1  # 1 万 skill 里定向查一条,不返回全量
    assert body["skills"][0]["name"] == "s4242"
    assert body["total"] == 10000


def test_standalone_projects_10000_skills_for_all_page_and_name(
        tmp_path, monkeypatch):
    def fake_build(*_args, **_kwargs):
        return _fake_catalog(10000)

    monkeypatch.setattr(metrics_mod, "_build_skills_catalog_uncached", fake_build)
    monkeypatch.setattr(
        router_mod,
        "_build_skillhub",
        Mock(return_value=None),
    )
    router = build_dashboard_router(
        db_path=tmp_path / "r.db",
        default_harness="unknown",
        default_model="unknown",
        expose_sensitive=False,
    )
    skills_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/v1/dashboard/skills"
    )

    all_skills = skills_endpoint()
    assert all_skills["total"] == 10000
    assert len(all_skills["skills"]) == 10000
    assert set(all_skills["skills"][0]) == {
        "name", "state", "source", "version", "candidates",
    }

    page = skills_endpoint(limit=100, offset=4200)
    assert len(page["skills"]) == 100
    assert page["skills"][0]["name"] == "s4200"
    assert page["offset"] == 4200
    assert page["limit"] == 100

    named = skills_endpoint(name="s4242")
    assert named["total"] == 10000
    assert named["skills"] == [{
        "name": "s4242",
        "state": "staging",
        "source": "native",
        "version": "1",
        "candidates": 0,
    }]


def test_standalone_projection_reuses_catalog_page_list(tmp_path, monkeypatch):
    source_rows = _fake_catalog(10000)
    original_list = source_rows
    original_first_row = source_rows[0]
    page = {
        "total": 10000,
        "by_state": {"main": 5000, "staging": 5000},
        "offset": 0,
        "limit": 0,
        "skills": source_rows,
    }
    monkeypatch.setattr(
        router_mod,
        "skills_catalog_page",
        Mock(return_value=page),
    )
    monkeypatch.setattr(
        router_mod,
        "_build_skillhub",
        Mock(return_value=None),
    )
    router = build_dashboard_router(
        db_path=tmp_path / "r.db",
        default_harness="unknown",
        default_model="unknown",
        expose_sensitive=False,
    )
    skills_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/v1/dashboard/skills"
    )

    projected_page = skills_endpoint()

    assert projected_page is page
    assert projected_page["skills"] is original_list
    assert projected_page["skills"][0] is not original_first_row
    assert len(projected_page["skills"]) == 10000
