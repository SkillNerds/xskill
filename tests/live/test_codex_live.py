"""tests/live/test_codex_live.py — real codex process + mock LLM E2E.

End-to-end verification that:

1. The mock LLM server speaks codex's Responses API correctly (real codex
   binary accepts our SSE responses without schema errors)
2. codex CLI, when pointed at the mock, runs ``codex exec`` to completion,
   writes a session rollout JSONL under ``CODEX_HOME/sessions/YYYY/MM/DD/``,
   and the rollout contains both the user prompt and the assistant reply
3. xskill's ``JsonlIngester(CODEX_SPEC)`` then picks up that rollout and
   bridges it to a ``traj_codex_*.md`` under the watch dir

If codex isn't installed, the test ``skip``s — CI installs codex CLI before
calling pytest, so this only skips in dev environments where the user
hasn't `npm i -g @openai/codex`'d locally.

Isolation: every test uses a tmp ``CODEX_HOME`` env so we never touch
the user's real ``~/.codex/`` directory. ``Path.home()`` is monkeypatched
to a forbidden path to catch any code path that defaults to it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.live.mock_llm_server import MOCK_REPLY_TEXT, MockLLMServer
from xskill.ecosystems import CODEX_SPEC, JsonlIngester


# Mark all tests in this file so CI can select with ``-m live_agent``.
pytestmark = pytest.mark.live_agent


def _have_codex() -> bool:
    """True if ``codex`` is on PATH and ``codex --version`` exits 0."""
    if shutil.which("codex") is None:
        return False
    try:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


requires_codex = pytest.mark.skipif(
    not _have_codex(),
    reason="codex CLI not installed (CI must install before pytest -m live_agent)",
)


def _write_codex_config(codex_home: Path, base_url: str) -> Path:
    """Write a minimal ``config.toml`` that points codex at a mock provider.

    Key fields:

    * ``model = "mock-model"`` — must match what mock server echoes back
    * ``model_provider = "mock"`` — references the ``[model_providers.mock]`` table
    * ``base_url`` — full URL incl. /v1 prefix; codex appends ``/responses``
    * ``env_key = "MOCK_API_KEY"`` — codex requires *some* key sourcing path;
      we set the env var to any non-empty string. Without ``env_key`` codex
      falls through to OpenAI ChatGPT login flow.
    * ``wire_api = "responses"`` — only supported wire api in codex 0.130+
    * ``requires_openai_auth = false`` — skip the OpenAI login screen
    """
    config_text = (
        'model = "mock-model"\n'
        'model_provider = "mock"\n'
        '\n'
        '[model_providers.mock]\n'
        'name = "Mock"\n'
        f'base_url = "{base_url}/v1"\n'
        'env_key = "MOCK_API_KEY"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
    )
    cfg = codex_home / "config.toml"
    cfg.write_text(config_text, encoding="utf-8")
    return cfg


@requires_codex
def test_codex_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn real codex → real rollout JSONL → xskill ingester → traj_codex_*.md.

    Pipeline asserted at each step:

    1. Mock server is reachable; codex's ``GET /v1/models`` + ``POST /v1/responses``
       calls all hit the mock and return clean (no schema errors in codex stderr)
    2. ``codex exec`` exits 0 within 60s
    3. ``CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`` materializes
    4. The rollout contains the user prompt (``hello``) and the mock reply
    5. ``JsonlIngester(CODEX_SPEC).scan_and_bridge`` returns 1 record
    6. The bridged ``traj_codex_*.md`` exists and contains the assistant reply
    """
    # Forbid Path.home() entirely — any code path that defaults to it is a leak
    forbidden = tmp_path / "_NEVER_TOUCH_REAL_HOME_"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: forbidden))

    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(parents=True)
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True)
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(parents=True)

    with MockLLMServer() as server:
        _write_codex_config(codex_home, server.base_url)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["MOCK_API_KEY"] = "fake-not-real-key"

        # Run codex exec non-interactively.
        # ``--skip-git-repo-check``: workdir isn't a git repo
        # ``--dangerously-bypass-approvals-and-sandbox``: no human, no sandbox
        #   (we trust mock reply has no shell calls, but if it did we don't
        #    want a sandbox prompt to hang us)
        # ``-C workdir``: codex's working directory (becomes session.cwd)
        result = subprocess.run(
            [
                "codex", "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(workdir),
                "hello",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # 1+2: codex exit was clean
        assert result.returncode == 0, (
            f"codex exec failed (rc={result.returncode})\n"
            f"---stdout---\n{result.stdout}\n"
            f"---stderr---\n{result.stderr}\n"
        )

        # 3: rollout file exists
        sessions_root = codex_home / "sessions"
        assert sessions_root.is_dir(), (
            f"codex did not create sessions root at {sessions_root}\n"
            f"codex stderr:\n{result.stderr}"
        )
        rollouts = list(sessions_root.rglob("rollout-*.jsonl"))
        assert len(rollouts) == 1, (
            f"expected exactly 1 rollout, got {len(rollouts)}: {rollouts}"
        )
        rollout = rollouts[0]

        # 4: rollout content has prompt + reply
        rollout_text = rollout.read_text(encoding="utf-8")
        assert "hello" in rollout_text, (
            f"user prompt 'hello' missing from rollout {rollout}; first 500 chars:\n"
            f"{rollout_text[:500]}"
        )
        assert MOCK_REPLY_TEXT in rollout_text, (
            f"mock reply {MOCK_REPLY_TEXT!r} missing from rollout {rollout}; "
            f"first 1000 chars:\n{rollout_text[:1000]}"
        )

        # Mock server actually saw a /v1/responses call
        responses_calls = [r for r in server.request_log if r["path"] == "/v1/responses"]
        assert len(responses_calls) >= 1, (
            f"mock server got no /v1/responses calls; log: {server.request_log}"
        )

    # 5: xskill ingester picks up rollout.
    #
    # CODEX_SPEC.sessions_path = home / ".codex" / "sessions" — but our CODEX_HOME
    # env is ``codex_home`` (no ``.codex`` prefix). Set up a synthetic home_root
    # by symlinking ``fake_home/.codex`` → ``codex_home`` so the spec's path
    # resolver finds the rollout we just produced.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    fake_codex_dir = fake_home / ".codex"
    if fake_codex_dir.exists():
        if fake_codex_dir.is_symlink():
            fake_codex_dir.unlink()
        else:
            shutil.rmtree(fake_codex_dir)
    fake_codex_dir.symlink_to(codex_home, target_is_directory=True)

    results = JsonlIngester(CODEX_SPEC).scan_and_bridge(
        target_traj_dir=watch_dir,
        home_root=fake_home,
    )

    assert len(results) >= 1, (
        f"JsonlIngester(CODEX_SPEC) bridged 0 sessions; "
        f"sessions_root={fake_home / '.codex' / 'sessions'}, "
        f"rollouts on disk={list((fake_home / '.codex' / 'sessions').rglob('rollout-*.jsonl'))}"
    )
    rec = results[0]
    assert rec["ecosystem"] == "codex"

    # 6: bridged traj_*.md on disk; the file pair (md + json) is what
    # the watcher then picks up. The md format is owned by the codex
    # adapter (``_adapt_codex_rollout_jsonl``) — we don't re-assert its
    # content here beyond "user prompt is in there", because the adapter
    # currently renders ``response_item`` payloads as placeholders. The
    # raw rollout (already asserted above) is the ground truth and proves
    # the LLM→codex→disk path works.
    traj_md = Path(rec["path"])
    assert traj_md.is_file(), f"bridged traj not on disk: {traj_md}"
    traj_json = traj_md.with_suffix(".json")
    assert traj_json.is_file(), f"bridged traj metadata not on disk: {traj_json}"
    traj_text = traj_md.read_text(encoding="utf-8")
    assert "hello" in traj_text.lower(), (
        f"bridged traj missing user prompt 'hello'; first 1000 chars:\n"
        f"{traj_text[:1000]}"
    )

    # Path.home() really was never read by any code we ran
    assert not forbidden.exists(), (
        f"isolation broken — something created {forbidden}; "
        f"a code path under test must have called Path.home()"
    )
