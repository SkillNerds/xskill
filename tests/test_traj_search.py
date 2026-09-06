"""`xskill traj search` 与 `xskill atom search`：两条检索 + CLI / team 分流。

traj 对 `traj_*.md` 做全文检索，可加 `--cards` 与 `--page`。
会话索引仍给上传写穿用。atom 走 Atom 混合检索。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill import cli
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry
from xskill.traj_search import (
    format_session_hit,
    format_traj_hit,
    parse_search_names,
    refresh_session_index,
    resolve_named_session_dirs,
    search_indexed_trajectories,
    search_session_trajectories,
    upsert_session_file,
    watch_session_dirs,
)
from xskill.utils.search import HybridSearch
from tests.test_atom_task_store import _FakeEmbed

TOKEN = "secret-token"


@pytest.fixture(autouse=True)
def _no_real_local_bootstrap(monkeypatch):
    """CLI 单测不要去扫真实 HOME 的 harness。"""
    monkeypatch.setattr(cli, "_maybe_bootstrap_local_traj", lambda: None)


def _args(**overrides) -> SimpleNamespace:
    base = {
        "terms": ["django", "migration"],
        "top_k": 5,
        "json": False,
        "team": False,
        "local": False,
        "name": "",
        "page": 1,
        "cards": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _atom_hit(**overrides) -> dict:
    row = {
        "atom_id": "atom_t_0001",
        "traj_id": "traj_cc_alice_memleak",
        "intent": "diagnose a python process leaking memory",
        "summary": "tracemalloc found a cache",
        "sources": ["vector", "keyword"],
        "vector_similarity": 0.91,
        "bm25_score": 4.2,
        "used_skills": ["python-memory-debug"],
        "offset_start": 12,
        "offset_end": 88,
        "md_path": "/secret/server/traj_cc_alice_memleak.md",
        "dataset_dir": "/secret/server/clients/alice/sessions",
        "raw_segment": "MUST_NOT_LEAK",
    }
    row.update(overrides)
    return row


def _session_hit(**overrides) -> dict:
    row = format_session_hit(
        traj_id="traj_cc_alice_memleak",
        user="alice",
        query="diagnose a python process leaking memory",
        turns=2,
        score=2.4,
    )
    row.update(overrides)
    return row


def _write_traj_md(root: Path, *, traj_id: str, query: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{traj_id}.md"
    path.write_text(
        "# Trajectory\n\n"
        f"## Initial Query\n\n{query}\n\n"
        f"## User\n\n{query}\n\n"
        "## Assistant\n\nlooking into it.\n\n"
        "padding-line-to-pass-min-traj-bytes\n",
        encoding="utf-8",
    )
    upsert_session_file(root, path)
    return path


def _browse_hit(**overrides) -> dict:
    row = {
        "kind": "traj",
        "traj_id": "traj_cc_alice_memleak",
        "user": "alice",
        "line": 5,
        "snippet": "diagnose a python process leaking memory",
        "hit_count": 1,
        "context": [
            {"line": 2, "hit": False, "text": ""},
            {"line": 3, "hit": False, "text": "## Initial Query"},
            {"line": 4, "hit": False, "text": ""},
            {"line": 5, "hit": True, "text": "diagnose a python process leaking memory"},
            {"line": 6, "hit": False, "text": ""},
            {"line": 7, "hit": False, "text": "## User"},
            {"line": 8, "hit": False, "text": ""},
        ],
    }
    row.update(overrides)
    return row


def _seed_watch(monkeypatch, tmp_path: Path, rows: list[tuple[str, str, str]]):
    dirs: list[tuple[str, Path]] = []
    for user, traj_id, query in rows:
        root = tmp_path / user / "sessions"
        _write_traj_md(root, traj_id=traj_id, query=query)
        dirs.append((user, root))
    monkeypatch.setattr("xskill.traj_search.watch_session_dirs", lambda: dirs)
    return dirs


class _Response:
    def __init__(self, status_code: int, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class _TrajHttp:
    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {
            "results": [_browse_hit()],
            "count": 1,
            "meta": {
                "unknown_names": [],
                "corpus_empty": False,
                "total": 1,
                "page": 1,
                "page_size": 30,
            },
        }
        self.error = error
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, **kwargs):
        self.calls.append((path, kwargs.get("params")))
        if self.error is not None:
            raise self.error
        return _Response(self.status_code, self.payload)


def _seed_sessions(root: Path, *, traj_id: str, intent: str,
                   used_skills: list[str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    store = AtomTaskStore(root=root)
    atom = AtomTask(
        atom_id=f"atom_{traj_id}_0001",
        traj_id=traj_id,
        offset_start=0,
        offset_end=10,
        intent=intent,
        summary=intent,
        tags=[],
        used_skills=used_skills or [],
        ux_score=None,
        pre_atom_id=None,
        post_atom_id=None,
        context_prefix="",
        raw_segment="raw session text must not appear in hits",
    )
    store.save(atom)
    store.rebuild_vector_index(_FakeEmbed())
    return root


def _hybrid_search_one(dataset_dir, query_text, top_k=5, **_kwargs):
    """与 ``utils.search.search`` 同形，embed 用测试 FakeEmbed。"""
    store = AtomTaskStore(root=Path(dataset_dir))
    hits = HybridSearch(store, _FakeEmbed()).search(query_text, top_k=top_k)
    out = []
    for hit in hits:
        atom = store.load(hit["atom_id"])
        hit["traj_id"] = atom.traj_id
        hit["md_path"] = str(Path(dataset_dir) / f"{atom.traj_id}.md")
        hit["intent"] = atom.intent
        hit["summary"] = atom.summary
        hit["offset_start"] = atom.offset_start
        hit["offset_end"] = atom.offset_end
        hit["used_skills"] = list(atom.used_skills or [])
        out.append(hit)
    return out


# ── 装配 ──────────────────────────────────────────────────────────


def test_parse_search_names_keeps_order_and_dedupes():
    assert parse_search_names(" 张三, 李四,张三, ") == ["张三", "李四"]
    assert parse_search_names("") == []
    assert parse_search_names(None) == []


def test_format_traj_hit_drops_path_and_raw_text():
    hit = format_traj_hit(_atom_hit(), user="alice")
    assert hit["traj_id"] == "traj_cc_alice_memleak"
    assert hit["atom_id"] == "atom_t_0001"
    assert hit["user"] == "alice"
    assert hit["used_skills"] == ["python-memory-debug"]
    assert hit["kind"] == "atom"
    assert hit["offset_start"] == 12
    assert hit["offset_end"] == 88
    assert hit["score"] == pytest.approx(0.91)
    assert "md_path" not in hit
    assert "dataset_dir" not in hit
    assert "raw_segment" not in hit
    assert "MUST_NOT_LEAK" not in json.dumps(hit)


def test_format_prefers_vector_score_else_bm25():
    vector = format_traj_hit(_atom_hit(vector_similarity=0.2, bm25_score=9.0))
    assert vector["score"] == pytest.approx(0.2)
    bm25_only = format_traj_hit(_atom_hit(
        vector_similarity=None, bm25_score=9.0, sources=["keyword"],
    ))
    assert bm25_only["score"] == pytest.approx(9.0)


def test_search_indexed_merges_named_dirs_and_caps_top_k(tmp_path):
    alice = tmp_path / "alice" / "sessions"
    bob = tmp_path / "bob" / "sessions"
    alice.mkdir(parents=True)
    bob.mkdir(parents=True)
    seen: list[str] = []

    def search_one(*, dataset_dir, query_text, top_k):
        seen.append(str(dataset_dir))
        assert query_text == "memory leak"
        assert top_k == 1
        label = Path(dataset_dir).parent.name
        return [_atom_hit(
            traj_id=f"traj_{label}",
            atom_id=f"atom_{label}",
            vector_similarity=0.4 if label == "alice" else 0.9,
            user=label,
        )]

    hits = search_indexed_trajectories(
        "memory leak",
        top_k=1,
        dataset_dirs=[("alice", alice), ("bob", bob)],
        search_one=search_one,
    )
    assert [hit["traj_id"] for hit in hits] == ["traj_bob"]
    assert hits[0]["user"] == "bob"
    assert set(seen) == {str(alice), str(bob)}


def test_search_indexed_puts_bm25_only_after_vector_hits(tmp_path):
    directory = tmp_path / "sessions"
    directory.mkdir()

    def search_one(*, dataset_dir, query_text, top_k):
        return [
            _atom_hit(
                traj_id="traj_kw",
                atom_id="atom_kw",
                vector_similarity=None,
                bm25_score=99.0,
                sources=["keyword"],
            ),
            _atom_hit(
                traj_id="traj_vec",
                atom_id="atom_vec",
                vector_similarity=0.11,
                bm25_score=0.1,
            ),
        ]

    hits = search_indexed_trajectories(
        "q",
        top_k=5,
        dataset_dirs=[("u", directory)],
        search_one=search_one,
    )
    assert [hit["traj_id"] for hit in hits] == ["traj_vec", "traj_kw"]


def test_search_indexed_skips_missing_dir_and_search_errors(tmp_path):
    missing = tmp_path / "gone" / "sessions"
    broken = tmp_path / "broken" / "sessions"
    broken.mkdir(parents=True)

    def search_one(*, dataset_dir, query_text, top_k):
        raise RuntimeError("embed down")

    assert search_indexed_trajectories(
        "q",
        dataset_dirs=[("gone", missing), ("broken", broken)],
        search_one=search_one,
    ) == []


def test_search_indexed_all_uses_injected_search_all():
    def search_all_fn(*, query_text, top_k):
        assert query_text == "auth retry"
        assert top_k == 3
        return [_atom_hit(user="alice")]

    hits = search_indexed_trajectories(
        "auth retry", top_k=3, search_all_fn=search_all_fn,
    )
    assert hits[0]["traj_id"] == "traj_cc_alice_memleak"
    assert "md_path" not in hits[0]


def test_resolve_named_session_dirs_keeps_unknown(tmp_path):
    found, unknown = resolve_named_session_dirs(
        ["alice", "ghost"],
        traj_root=tmp_path,
        find_client_id={"alice": "cid-a"}.get,
        dir_name_for={"cid-a": "alice"}.get,
    )
    assert found == [("alice", tmp_path / "clients" / "alice" / "sessions")]
    assert unknown == ["ghost"]


def test_resolve_named_session_dirs_dir_lookup_failure_is_unknown(tmp_path):
    def dir_name_for(_client_id: str) -> str:
        raise ValueError("unknown client_id")

    found, unknown = resolve_named_session_dirs(
        ["alice"],
        traj_root=tmp_path,
        find_client_id={"alice": "cid-a"}.get,
        dir_name_for=dir_name_for,
    )
    assert found == []
    assert unknown == ["alice"]


def test_search_session_bm25_reads_user_query_not_atom(tmp_path):
    alice = tmp_path / "alice" / "sessions"
    bob = tmp_path / "bob" / "sessions"
    _write_traj_md(
        alice,
        traj_id="traj_cc_alice_memleak",
        query="diagnose a python process leaking memory",
    )
    _write_traj_md(
        bob,
        traj_id="traj_cc_bob_gc",
        query="tighten the cache after rss climbed",
    )
    hits = search_session_trajectories(
        "python process leaking",
        top_k=5,
        dataset_dirs=[("alice", alice), ("bob", bob)],
    )
    assert hits
    assert hits[0]["kind"] == "traj"
    assert hits[0]["traj_id"] == "traj_cc_alice_memleak"
    assert hits[0]["user"] == "alice"
    assert "memory" in hits[0]["query"]
    assert "atom_id" not in hits[0]
    assert "summary" not in hits[0]
    dumped = json.dumps(hits)
    assert str(tmp_path) not in dumped


def test_search_session_reads_index_not_md(tmp_path, monkeypatch):
    alice = tmp_path / "alice" / "sessions"
    _write_traj_md(
        alice,
        traj_id="traj_cc_alice_memleak",
        query="diagnose a python process leaking memory",
    )

    def boom(path):
        raise AssertionError(f"search must not open {path}")

    monkeypatch.setattr("xskill.traj_search._read_session_query", boom)
    hits = search_session_trajectories(
        "python process leaking",
        dataset_dirs=[("alice", alice)],
    )
    assert hits[0]["traj_id"] == "traj_cc_alice_memleak"


def test_search_session_ignores_md_until_indexed(tmp_path):
    alice = tmp_path / "alice" / "sessions"
    alice.mkdir(parents=True)
    (alice / "traj_cc_alice_memleak.md").write_text(
        "## Initial Query\n\ndiagnose a python process leaking memory\n",
        encoding="utf-8",
    )
    assert search_session_trajectories(
        "python process leaking",
        dataset_dirs=[("alice", alice)],
    ) == []
    assert refresh_session_index(alice, limit=None) == 0
    hits = search_session_trajectories(
        "python process leaking",
        dataset_dirs=[("alice", alice)],
    )
    assert hits[0]["traj_id"] == "traj_cc_alice_memleak"


def test_watch_session_dirs_includes_harness_bridge(tmp_path, monkeypatch):
    home = tmp_path / ".xskill"
    sessions = home / "cc_sessions"
    sessions.mkdir(parents=True)
    (sessions / "traj_cc_offline.md").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr("xskill.config.XSKILL_HOME", home)
    monkeypatch.setattr("xskill.pipeline.registry.list_watch_dirs", lambda: [])
    dirs = watch_session_dirs()
    assert [(label, path) for label, path in dirs] == [("cc_sessions", sessions)]


def test_watch_session_dirs_dedupes_registry_and_bridge(tmp_path, monkeypatch):
    home = tmp_path / ".xskill"
    sessions = home / "cc_sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr("xskill.config.XSKILL_HOME", home)
    monkeypatch.setattr(
        "xskill.pipeline.registry.list_watch_dirs",
        lambda: [{"path": str(sessions), "label": "alice"}],
    )
    dirs = watch_session_dirs()
    assert [(label, path.resolve()) for label, path in dirs] == [
        ("alice", sessions.resolve()),
    ]


def test_refresh_session_index_drops_deleted(tmp_path):
    alice = tmp_path / "alice" / "sessions"
    path = _write_traj_md(
        alice,
        traj_id="traj_cc_alice_memleak",
        query="diagnose a python process leaking memory",
    )
    path.unlink()
    assert refresh_session_index(alice, limit=None) == 0
    assert search_session_trajectories(
        "python process leaking",
        dataset_dirs=[("alice", alice)],
    ) == []


def test_hybrid_search_one_returns_atom_fields_not_raw(tmp_path):
    sessions = _seed_sessions(
        tmp_path / "sessions",
        traj_id="traj_cc_alice_memleak",
        intent="fix django migration conflict",
        used_skills=["alembic-half"],
    )
    hits = _hybrid_search_one(sessions, "django migration", top_k=5)
    assert hits
    assert hits[0]["traj_id"] == "traj_cc_alice_memleak"
    assert "django" in hits[0]["intent"]
    assert hits[0]["used_skills"] == ["alembic-half"]
    formatted = format_traj_hit(hits[0], user="alice")
    assert "raw session text" not in json.dumps(formatted)
    assert "md_path" not in formatted


# ── CLI ───────────────────────────────────────────────────────────


def test_cli_search_traj_local_prints_hits(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    _seed_watch(monkeypatch, tmp_path, [
        ("alice", "traj_cc_alice_memleak", "fix django migration conflict"),
    ])
    rc = cli.cmd_search_traj(_args())
    assert rc == 0
    out = capsys.readouterr()
    assert "query='django migration' 命中 1 条不同轨迹，展示 1 条" in out.out
    assert "按相关度排序" in out.out
    assert "看内容用 --cards，一次最多 8 个 id。" in out.out
    assert "看原文：xskill traj read <traj_id>" not in out.out
    assert "traj_cc_alice_memleak" in out.out
    assert "django migration" in out.out
    assert "*" in out.out
    assert "## Initial Query" in out.out
    assert "首问:" not in out.out
    assert "轨迹 ID：" not in out.out
    assert "找到 1 条轨迹" not in out.out
    assert "Atom：" not in out.out
    assert "MUST_NOT_LEAK" not in out.out
    assert "/secret/server" not in out.out
    assert "本机轨迹原文目录" not in out.out


def test_cli_search_traj_flag_local_prints_md_dirs(monkeypatch, capsys, tmp_path):
    home = tmp_path / ".xskill"
    cc = home / "cc_sessions"
    cursor = home / "cursor_sessions"
    cc.mkdir(parents=True)
    cursor.mkdir(parents=True)
    monkeypatch.setattr("xskill.runtime.role", lambda: "client")
    monkeypatch.setattr("xskill.config.XSKILL_HOME", home)
    _seed_watch(monkeypatch, tmp_path, [
        ("alice", "traj_cc_alice_memleak", "diagnose 内存泄漏 in python"),
    ])
    rc = cli.cmd_search_traj(_args(local=True, terms=["内存泄漏"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "query='内存泄漏' 命中 1 条不同轨迹" in out
    assert "看内容用 --cards" in out
    assert "traj_cc_alice_memleak" in out
    assert "内存泄漏" in out
    assert "*" in out
    assert "本机轨迹原文目录如下，可用本 harness 的 grep 直接搜里面的 traj_*.md：" in out
    assert str(cc) in out
    assert str(cursor) in out
    rc = cli.cmd_search_traj(_args(local=True, json=True, terms=["内存泄漏"]))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload[0]["traj_id"] == "traj_cc_alice_memleak"
    assert payload[0]["line"] >= 1
    assert payload[0]["context"]
    assert any(item.get("hit") for item in payload[0]["context"])
    assert str(cc) in captured.err
    assert "本机轨迹原文目录" in captured.err


def test_cli_search_traj_local_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    _seed_watch(monkeypatch, tmp_path, [
        ("alice", "traj_cc_alice_memleak", "fix alembic half migration"),
    ])
    rc = cli.cmd_search_traj(_args(json=True, terms=["alembic"]))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["traj_id"] == "traj_cc_alice_memleak"
    assert payload[0]["kind"] == "traj"
    assert "snippet" in payload[0]
    assert payload[0]["context"]
    assert "md_path" not in payload[0]
    assert "raw_segment" not in payload[0]


def test_cli_search_traj_local_warns_and_ignores_name(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    _seed_watch(monkeypatch, tmp_path, [])
    rc = cli.cmd_search_traj(_args(name="alice", terms=["q"]))
    assert rc == 0
    captured = capsys.readouterr()
    assert "仅 team" in captured.err
    assert "query='q' 没有命中" in captured.out
    assert "轨迹检索索引尚未建成" not in captured.out


def test_cli_search_traj_local_cards_and_page(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    rows = [
        ("alice", f"traj_cc_alice_{index:02d}", "patentdagger crawl routine")
        for index in range(10)
    ]
    _seed_watch(monkeypatch, tmp_path, rows)
    listed = cli.cmd_search_traj(_args(terms=["patentdagger"], top_k=3, page=2))
    assert listed == 0
    listing = capsys.readouterr().out
    assert "命中 10 条不同轨迹，展示 3 条" in listing
    assert "还有 4 条未列出" in listing
    assert "--page 3" in listing
    assert "看内容用 --cards" in listing
    cards = cli.cmd_search_traj(_args(terms=["patentdagger"], cards=True))
    assert cards == 0
    card_out = capsys.readouterr().out
    assert "cards=8（卡片只是索引，不算精读）" in card_out
    assert "来源: claude-code" in card_out
    assert "问: patentdagger crawl routine" in card_out
    assert "精读：xskill traj read " in card_out
    assert "--offset-start <上面的 L 行号>" in card_out
    assert "还有 2 张未列出" in card_out
    assert "--cards --page 2" in card_out
    by_id = cli.cmd_search_traj(_args(
        terms=["traj_cc_alice_00"], cards=True,
    ))
    assert by_id == 0
    id_out = capsys.readouterr().out
    assert "cards=1（卡片只是索引，不算精读）" in id_out
    assert "--- traj_cc_alice_00 ---" in id_out


def test_cli_search_traj_missing_query_errors(capsys):
    rc = cli.cmd_search_traj(_args(terms=[]))
    assert rc == 2
    assert "xskill traj search <query>" in capsys.readouterr().err


def test_cli_search_atom_missing_query_errors(capsys):
    rc = cli.cmd_search_atom(_args(terms=[]))
    assert rc == 2
    assert "xskill atom search <query>" in capsys.readouterr().err


def test_cli_search_atom_local_prints_hits(monkeypatch, capsys):
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    monkeypatch.setattr(
        "xskill.traj_search.search_indexed_atoms",
        lambda query, **_kw: [format_traj_hit(_atom_hit(), user="alice")],
    )
    monkeypatch.setattr(
        "xskill.pipeline.registry.all_index_paths", lambda: [Path("/x")],
    )
    rc = cli.cmd_search_atom(_args(terms=["django", "migration"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "找到 1 个 Atom" in out
    assert "Atom ID：atom_t_0001" in out
    assert "行号：L12-L88" in out


def test_cli_search_atom_team_uses_atoms_path(capsys):
    http = _TrajHttp(payload={
        "results": [format_traj_hit(_atom_hit(), user="alice")],
        "count": 1,
        "meta": {"unknown_names": [], "corpus_empty": False},
    })
    rc = cli.cmd_search_atom(
        _args(team=True, terms=["memory"]),
        http=http,
        headers={"X-Xskill-Token": TOKEN},
    )
    assert rc == 0
    assert http.calls[0][0] == "/api/v1/team/atoms/search"
    out = capsys.readouterr().out
    assert "找到 1 个 Atom" in out
    assert "行号：L12-L88" in out


def test_cli_search_word_traj_stays_on_skill_search(monkeypatch):
    called = []
    monkeypatch.setattr(
        cli, "cmd_search_traj", lambda args: called.append("traj") or 0,
    )
    monkeypatch.setattr(
        cli, "cmd_search_hub", lambda args: called.append("hub") or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_search_local", lambda args: called.append("local") or 0,
    )
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    rc = cli.cmd_search(_args(terms=["traj"]))
    assert rc == 0
    assert called == ["local"]


def test_parser_noun_first_and_search_keeps_traj_word():
    parser = cli.build_parser()
    skill = parser.parse_args(["search", "traj", "内存泄漏"])
    assert skill.command == "search"
    assert skill.terms == ["traj", "内存泄漏"]
    traj = parser.parse_args(["traj", "search", "内存泄漏"])
    assert traj.command == "traj"
    assert traj.traj_action == "search"
    assert traj.terms == ["内存泄漏"]
    assert traj.page == 1
    assert traj.cards is False
    assert traj.top_k == 30
    assert not hasattr(traj, "download")
    paged = parser.parse_args(
        ["traj", "search", "patentdagger", "--cards", "--page", "3"],
    )
    assert paged.cards is True
    assert paged.page == 3
    atom = parser.parse_args(["atom", "search", "编辑器"])
    assert atom.command == "atom"
    assert atom.atom_action == "search"
    read_db = parser.parse_args(["read", "/tmp/foo.db"])
    assert read_db.command == "read"
    assert read_db.path == "/tmp/foo.db"
    traj_read = parser.parse_args(["traj", "read", "traj_cc_alice"])
    assert traj_read.command == "traj"
    assert traj_read.traj_action == "read"
    assert traj_read.target == "traj_cc_alice"


def test_cli_search_without_traj_prefix_stays_on_skill_search(monkeypatch):
    called = []
    monkeypatch.setattr(
        cli, "cmd_search_traj", lambda args: called.append("traj") or 0,
    )
    monkeypatch.setattr(
        cli, "cmd_search_hub", lambda args: called.append("hub") or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_search_local", lambda args: called.append("local") or 0,
    )
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    rc = cli.cmd_search(_args(terms=["docker", "compose"]))
    assert rc == 0
    assert called == ["local"]


def test_cli_search_traj_dispatches_team_when_client(monkeypatch):
    called = []
    monkeypatch.setattr("xskill.runtime.role", lambda: "client")
    monkeypatch.setattr(
        cli, "_cmd_search_kind_team",
        lambda query, **kw: called.append(
            ("team", query, kw.get("kind"), kw.get("names"))
        ) or 0,
    )
    monkeypatch.setattr(
        cli, "_cmd_search_kind_local",
        lambda query, **kw: called.append(("local", query)) or 0,
    )
    rc = cli.cmd_search_traj(_args(name="alice,bob", terms=["发票"]))
    assert rc == 0
    assert called == [("team", "发票", "traj", ["alice", "bob"])]


def test_cli_search_traj_team_prints_and_forwards_names(capsys):
    http = _TrajHttp()
    rc = cli.cmd_search_traj(
        _args(team=True, name="alice,ghost", terms=["memory"]),
        http=http,
        headers={"X-Xskill-Token": TOKEN},
    )
    assert rc == 0
    assert http.calls[0][0] == "/api/v1/team/trajectories/search"
    assert http.calls[0][1] == {
        "query": "memory", "limit": 5, "page": 1, "names": "alice,ghost",
    }
    out = capsys.readouterr().out
    assert "query='memory' 命中 1 条不同轨迹，展示 1 条" in out
    assert "看内容用 --cards，一次最多 8 个 id。" in out
    assert "traj_cc_alice_memleak" in out
    assert "  L5* diagnose a python process leaking memory" in out
    assert "  L3  ## Initial Query" in out
    assert "首问:" not in out
    assert "轨迹 ID：" not in out


def test_cli_search_traj_team_forwards_cards_and_page(capsys):
    http = _TrajHttp(payload={
        "text": (
            "cards=1（卡片只是索引，不算精读）\n\n"
            "--- traj_cc_alice_memleak ---\n来源: claude-code"
        ),
        "results": [{"traj_id": "traj_cc_alice_memleak"}],
        "count": 1,
        "meta": {"unknown_names": [], "total": 1, "page": 2, "page_size": 8},
    })
    rc = cli.cmd_search_traj(
        _args(team=True, cards=True, page=2, terms=["patentdagger"]),
        http=http,
        headers={"X-Xskill-Token": TOKEN},
    )
    assert rc == 0
    assert http.calls[0][1] == {
        "query": "patentdagger", "limit": 5, "page": 2, "cards": "1",
    }
    assert "cards=1（卡片只是索引，不算精读）" in capsys.readouterr().out


def test_cli_search_traj_team_unknown_names_warn(capsys):
    http = _TrajHttp(payload={
        "results": [],
        "count": 0,
        "meta": {"unknown_names": ["ghost"], "corpus_empty": False},
    })
    rc = cli.cmd_search_traj(
        _args(team=True, terms=["q"]),
        http=http,
        headers={},
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "未识别工号 ghost" in captured.err
    assert "query='q' 没有命中" in captured.out


def test_cli_search_traj_team_old_server(capsys):
    http = _TrajHttp(status_code=404)
    rc = cli.cmd_search_traj(
        _args(team=True, terms=["q"]),
        http=http,
        headers={},
    )
    assert rc == 1
    assert "升级 server" in capsys.readouterr().err


def test_cli_search_traj_team_network_error(capsys):
    http = _TrajHttp(error=httpx.ConnectError("refused"))
    rc = cli.cmd_search_traj(
        _args(team=True, terms=["q"]),
        http=http,
        headers={},
    )
    assert rc == 1
    assert "无法连接 team server" in capsys.readouterr().err


def test_cli_search_traj_unconnected_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "xskill.config.get_team_client_state_path",
        lambda: tmp_path / "missing.json",
    )
    rc = cli.cmd_search_traj(_args(team=True, terms=["q"]))
    assert rc == 1
    assert "未连接 team server" in capsys.readouterr().err


# ── team HTTP ─────────────────────────────────────────────────────


def _make_team_app(tmp_path: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    traj_root = tmp_path / "team_traj"
    registry = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token=TOKEN,
        client_registry=registry,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda path, label: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app), traj_root, registry


def _register(client: TestClient, user_name: str) -> dict:
    response = client.post(
        "/api/v1/team/register",
        json={"token": TOKEN, "user_name": user_name, "hostname": "h"},
    )
    assert response.status_code == 200
    return {
        "X-Xskill-Token": TOKEN,
        "X-Xskill-Client": response.json()["client_id"],
    }


def test_team_traj_search_requires_auth(tmp_path):
    client, _traj_root, _reg = _make_team_app(tmp_path)
    assert client.get(
        "/api/v1/team/trajectories/search", params={"query": "q"},
    ).status_code == 401


def test_team_traj_search_rejects_empty_query(tmp_path):
    client, _traj_root, _reg = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    response = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "  "},
        headers=headers,
    )
    assert response.status_code == 400


def test_team_traj_search_named_dir_uses_md_bm25(tmp_path):
    client, traj_root, registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    client_id = registry.find_by_user_name("alice")
    sessions = traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    _write_traj_md(
        sessions,
        traj_id="traj_cc_alice_memleak",
        query="fix django migration conflict",
    )
    response = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "django migration", "limit": 5, "names": "alice,ghost"},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["unknown_names"] == ["ghost"]
    assert payload["count"] >= 1
    hit = payload["results"][0]
    assert hit["kind"] == "traj"
    assert hit["traj_id"] == "traj_cc_alice_memleak"
    assert hit["user"] == "alice"
    assert "atom_id" not in hit
    assert "md_path" not in hit
    dumped = json.dumps(payload)
    assert str(traj_root) not in dumped
    assert "django migration" in hit["snippet"]
    assert hit["line"] >= 1
    assert hit["context"]
    assert any(item.get("hit") and "django migration" in str(item.get("text")) for item in hit["context"])
    assert len(hit["context"]) >= 4


def test_team_traj_search_cards_and_page(tmp_path):
    client, traj_root, registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    client_id = registry.find_by_user_name("alice")
    sessions = traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    for index in range(9):
        _write_traj_md(
            sessions,
            traj_id=f"traj_cc_alice_{index:02d}",
            query="patentdagger crawl routine",
        )
    listed = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "patentdagger", "limit": 3, "page": 2},
        headers=headers,
    )
    assert listed.status_code == 200
    listing = listed.json()
    assert listing["count"] == 3
    assert listing["meta"]["total"] == 9
    assert listing["meta"]["page"] == 2
    cards = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "patentdagger", "cards": "1"},
        headers=headers,
    )
    assert cards.status_code == 200
    payload = cards.json()
    assert payload["count"] == 8
    assert payload["meta"]["total"] == 9
    text = payload["text"]
    assert "cards=8（卡片只是索引，不算精读）" in text
    assert "问: patentdagger crawl routine" in text
    assert "精读：xskill traj read " in text
    assert "还有 1 张未列出" in text
    by_id = client.get(
        "/api/v1/team/trajectories/search",
        params={"cards": "1", "ids": "traj_cc_alice_00"},
        headers=headers,
    )
    assert by_id.status_code == 200
    assert "--- traj_cc_alice_00 ---" in by_id.json()["text"]
    bad = client.get(
        "/api/v1/team/trajectories/search",
        params={"ids": "traj_cc_alice_00"},
        headers=headers,
    )
    assert bad.status_code == 400


def test_team_traj_search_unknown_names_do_not_fail(tmp_path, monkeypatch):
    client, _traj_root, _reg = _make_team_app(tmp_path)
    headers = _register(client, "alice")

    def boom(*_args, **_kwargs):
        raise AssertionError("should not search when every name is unknown")

    monkeypatch.setattr("xskill.traj_search.search_session_trajectories", boom)
    monkeypatch.setattr("xskill.traj_browse.find_query_hits", boom)
    monkeypatch.setattr("xskill.utils.search.search", boom)
    monkeypatch.setattr("xskill.utils.search.search_all", boom)
    response = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "q", "names": "ghost"},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["meta"]["unknown_names"] == ["ghost"]
    assert payload["meta"]["corpus_empty"] is False


def test_team_traj_search_reads_session_index(tmp_path):
    client, traj_root, registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    client_id = registry.find_by_user_name("alice")
    sessions = traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    _write_traj_md(
        sessions,
        traj_id="traj_cc_erin_auth_retry",
        query="retry expired oauth token",
    )
    response = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "oauth token", "limit": 5},
        headers=headers,
    )
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["traj_id"] == "traj_cc_erin_auth_retry"
    assert hit["kind"] == "traj"
    assert hit["line"] >= 1
    assert "oauth token" in hit["snippet"]
    assert "md_path" not in hit


def test_team_upload_write_through_session_index(tmp_path):
    client, _traj_root, _registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    body = (
        "# Trajectory\n\n"
        "## Initial Query\n\nretry expired oauth token\n\n"
        "## User\n\nretry expired oauth token\n\n"
        "## Assistant\n\nlooking into it.\n\n"
        "padding-line-to-pass-min-traj-bytes\n"
    )
    uploaded = client.post(
        "/api/v1/team/upload",
        headers=headers,
        json={
            "trajectories": [{
                "traj_id": "traj_cc_erin_auth_retry",
                "content": body,
                "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            }],
        },
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["accepted"] == ["traj_cc_erin_auth_retry"]
    response = client.get(
        "/api/v1/team/trajectories/search",
        params={"query": "oauth token", "limit": 5},
        headers=headers,
    )
    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["traj_id"] == "traj_cc_erin_auth_retry"
    assert hit["kind"] == "traj"
    assert "oauth token" in hit["snippet"]


def test_team_atom_search_named_dir_uses_real_hybrid_search(tmp_path, monkeypatch):
    client, traj_root, registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    client_id = registry.find_by_user_name("alice")
    sessions = traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    _seed_sessions(
        sessions,
        traj_id="traj_cc_alice_memleak",
        intent="fix django migration conflict",
        used_skills=["alembic-half"],
    )
    monkeypatch.setattr(
        "xskill.utils.search.create_embed_client", lambda _cfg=None: _FakeEmbed(),
    )
    monkeypatch.setattr("xskill.utils.search.load_config", lambda: {})
    response = client.get(
        "/api/v1/team/atoms/search",
        params={"query": "django migration", "limit": 5, "names": "alice,ghost"},
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["unknown_names"] == ["ghost"]
    hit = payload["results"][0]
    assert hit["kind"] == "atom"
    assert hit["traj_id"] == "traj_cc_alice_memleak"
    assert hit["user"] == "alice"
    assert hit["used_skills"] == ["alembic-half"]
    assert "md_path" not in hit
    dumped = json.dumps(payload)
    assert "raw session text" not in dumped
    assert str(traj_root) not in dumped


def test_bundled_skill_documents_real_traj_search_not_mock():
    from xskill.ecosystems.bundled_guide import bundled_xskill_source

    skill_md = (bundled_xskill_source() / "SKILL.md").read_text(encoding="utf-8")
    assert "xskill traj search" in skill_md
    assert "xskill atom search" in skill_md
    assert "xskill traj read" in skill_md
    assert "xskill atom read" in skill_md
    assert "xskill search traj" not in skill_md
    assert "xskill read traj" not in skill_md
    assert "--local" in skill_md
    assert "--cards" in skill_md
    assert "--page" in skill_md
    assert "grep" in skill_md
    assert "~/.xskill/cc_sessions" in skill_md
    assert "teammates" in skill_md
    assert "Claude Code" in skill_md
    assert "name: xskill-helper" in skill_md
    assert "hub.xskill.wiki" not in skill_md
    assert "dd7f641c16ced6d1db43e754055fd2c8" not in skill_md
    assert "xskill init" in skill_md
    assert "mock" not in skill_md.lower()


def test_package_has_no_bundled_mock_catalog():
    data = Path(__file__).resolve().parents[1] / "src" / "xskill" / "data"
    assert not (data / "mock_trajectories.json").exists()
