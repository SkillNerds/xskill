from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from xskill.ecosystems import submit_trajectory


@pytest.mark.parametrize(
    ("content", "format"),
    [
        ("", "markdown"),
        ("  \n\t", "markdown"),
        ("", "raw"),
        ("  \n\t", "raw"),
        ("{}", "json"),
    ],
)
def test_submit_rejects_empty_trajectories_without_writing_files(
    tmp_path, content, format
):
    with pytest.raises(ValueError, match="content"):
        submit_trajectory(content=content, format=format, traj_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_submit_accepts_json_with_a_message(tmp_path):
    result = submit_trajectory(
        content='{"messages":[{"role":"user","content":"hello"}]}',
        format="json",
        traj_dir=tmp_path,
    )

    assert result["status"] == "stored"
    assert "hello" in (tmp_path / "traj_0001.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "  \n\t"])
async def test_api_rejects_empty_content_before_resolving_watch_directory(
    tmp_path, monkeypatch, content
):
    import xskill.api.app as api_app
    import xskill.ecosystems._shared as shared

    monkeypatch.setattr(api_app, "_ensure_loaded", lambda: None)
    monkeypatch.setattr(api_app, "_config", {})
    monkeypatch.setattr(api_app, "_skill_dir", tmp_path / "skill")
    def fail_get_traj_dir():
        raise AssertionError("invalid content should fail before resolving a watch directory")

    monkeypatch.setattr(shared, "get_traj_dir", fail_get_traj_dir)

    app = api_app.create_app(home_root=tmp_path)
    route = next(
        route for route in app.routes
        if getattr(route, "path", None) == "/api/v1/trajectories/submit"
    )

    with pytest.raises(HTTPException) as exc_info:
        await route.endpoint(SimpleNamespace(
            content=content,
            format="markdown",
            metadata=None,
            traj_id=None,
        ))

    assert exc_info.value.status_code == 400
    assert list(tmp_path.iterdir()) == []
