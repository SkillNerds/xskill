"""Issue #405: relevance must precede pagination on every search surface."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from tests.test_traj_search import _args, _make_team_app, _register
from xskill import cli, traj_browse
from xskill.agents import agent_tools, traj_tools


def _seed(root: Path) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    # Creation order and recency both favor weak matches on the old path.
    specs = [
        ("traj_cc_project_high", 12, 5, 100),
        ("traj_cc_project_early", 3, 5, 100),
        ("traj_cc_project_alpha", 3, 9, 300),
        ("traj_cc_project_beta", 3, 9, 300),
        ("traj_cc_project_old", 3, 9, 200),
        ("traj_cc_project_weak", 1, 20, 900),
    ]
    specs.extend((f"traj_oc_other{index}_weak", 1, 25, 1000) for index in range(32))
    for name, count, first, mtime in specs:
        lines = ["# Trajectory", "", "## User", ""]
        lines += ["background context"] * (first - 5)
        lines += ["phoenix research"] * count
        lines += ["", "## Assistant", "", "Observed and verified the result."]
        lines += ["padding " * 40]  # Both search surfaces' minimum file sizes.
        path = root / f"{name}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))
    return [item[0] for item in specs[:6]] + sorted(item[0] for item in specs[6:])


def _fake_rg(args, _timeout):
    """Supply rg's count/first-line protocol in deliberately reversed order."""
    target = Path(args[-1])
    query = args[args.index("-e") + 1]
    paths = (
        sorted(target.rglob("traj_*.md"), reverse=True) if target.is_dir() else [target]
    )
    rows = []
    for path in paths:
        matches = [
            (number, line)
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            if query in line
        ]
        if not matches:
            continue
        if "-c" in args:
            rows.append(f"{path}:{len(matches)}")
        else:
            number, line = matches[0]
            rows.append(f"{number}:{line}")
    return "\n".join(rows)


@pytest.mark.parametrize("backend", ["python", "rg"])
def test_relevance_matches_generate_and_browse_before_limit(
    tmp_path, monkeypatch, backend
):
    expected = _seed(tmp_path)
    monkeypatch.setattr(
        traj_browse.shutil, "which", lambda _name: "rg" if backend == "rg" else None
    )
    monkeypatch.setattr(traj_browse, "_rg", _fake_rg)
    monkeypatch.setattr(traj_tools, "_rg", _fake_rg)
    hits = traj_browse.find_query_hits("phoenix", dataset_dirs=[("alice", tmp_path)])
    assert [hit.traj_id for hit in hits] == expected
    assert [(hit.hit_count, hit.line) for hit in hits[:6]] == [
        (12, 5),
        (3, 5),
        (3, 9),
        (3, 9),
        (3, 9),
        (1, 20),
    ]
    ctx = agent_tools.create_agent_tool_context(default_traj_root=tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        for limit in (1, 3, 8, 30):
            text = traj_tools.traj_search.entrypoint(query="phoenix", limit=limit)
            assert re.findall(r"^(traj_\S+)\t", text, re.MULTILINE) == expected[:limit]
            assert "按相关度排序" in text
            assert "同名前缀已错开" not in text


def _card_ids(text: str) -> list[str]:
    return re.findall(r"^--- (traj_\S+) ---$", text, re.MULTILINE)


@pytest.mark.performance_contract
def test_relevance_generate_only_reads_competitive_count_bands(tmp_path, monkeypatch):
    expected = _seed(tmp_path)
    monkeypatch.setattr(traj_tools.shutil, "which", lambda _name: "rg")
    first_reads = []

    def tracked_rg(args, timeout):
        if "-n" in args:
            first_reads.append(Path(args[-1]).stem)
        return _fake_rg(args, timeout)

    monkeypatch.setattr(traj_tools, "_rg", tracked_rg)
    ctx = agent_tools.create_agent_tool_context(default_traj_root=tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        text = traj_tools.traj_search.entrypoint(query="phoenix", limit=3)
    assert re.findall(r"^(traj_\S+)\t", text, re.MULTILINE) == expected[:3]
    # All count=3 ties must compete; 33 count=1 files need no second read.
    assert set(first_reads) == set(expected[:5])
    assert len(first_reads) == 5


def test_relevance_missing_file_uses_stable_tiebreaker(tmp_path):
    hits = [
        traj_browse.TrajHit(name, tmp_path / f"{name}.md", "", 5, "phoenix", 2)
        for name in ("traj_cc_beta", "traj_cc_alpha")
    ]
    assert [
        hit.traj_id for hit in sorted(hits, key=traj_browse.query_hit_sort_key)
    ] == [
        "traj_cc_alpha",
        "traj_cc_beta",
    ]


def test_relevance_local_team_pages_cards_and_explicit_ids(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(traj_browse.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli, "_maybe_bootstrap_local_traj", lambda: None)
    monkeypatch.setattr("xskill.runtime.role", lambda: "standalone")
    client, traj_root, registry = _make_team_app(tmp_path)
    headers = _register(client, "alice")
    client_id = registry.find_by_user_name("alice")
    sessions = traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    expected = _seed(sessions)
    monkeypatch.setattr(
        "xskill.traj_search.watch_session_dirs", lambda: [("alice", sessions)]
    )
    endpoint = "/api/v1/team/trajectories/search"
    with client:
        for page in (1, 2):
            assert (
                cli.cmd_search_traj(
                    _args(terms=["phoenix"], json=True, top_k=30, page=page)
                )
                == 0
            )
            local = json.loads(capsys.readouterr().out)
            response = client.get(
                endpoint,
                params={"query": "phoenix", "limit": 30, "page": page},
                headers=headers,
            )
            assert response.status_code == 200
            selected = expected[(page - 1) * 30 : page * 30]
            assert [hit["traj_id"] for hit in local] == selected
            assert [hit["traj_id"] for hit in response.json()["results"]] == selected
            assert (
                cli.cmd_search_traj(_args(terms=["phoenix"], cards=True, page=page))
                == 0
            )
            cards = capsys.readouterr().out
            response = client.get(
                endpoint,
                params={"query": "phoenix", "cards": "1", "page": page},
                headers=headers,
            )
            assert response.status_code == 200
            selected = expected[(page - 1) * 8 : page * 8]
            assert _card_ids(cards) == selected
            assert _card_ids(response.json()["text"]) == selected
        # A named member filter changes the scope, not its ordering.
        response = client.get(
            endpoint,
            params={"query": "phoenix", "names": "alice", "limit": 30},
            headers=headers,
        )
        assert [hit["traj_id"] for hit in response.json()["results"]] == expected[:30]
        chosen = [expected[-1], expected[0]]
        assert cli.cmd_search_traj(_args(terms=chosen, cards=True)) == 0
        assert _card_ids(capsys.readouterr().out) == chosen
        response = client.get(
            endpoint, params={"cards": "1", "ids": ",".join(chosen)}, headers=headers
        )
        assert _card_ids(response.json()["text"]) == chosen
    ctx = agent_tools.create_agent_tool_context(default_traj_root=sessions)
    with agent_tools.use_agent_tool_context(ctx):
        assert (
            _card_ids(traj_tools.traj_cards.entrypoint(traj_ids=",".join(chosen)))
            == chosen
        )
