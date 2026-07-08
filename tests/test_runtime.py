"""runtime.py process-liveness helpers."""
from __future__ import annotations


def test_alive_windows_uses_tasklist(monkeypatch):
    from xskill import runtime

    calls = []

    class _Result:
        stdout = '"python.exe","4242","Console","1","10,000 K"\n'

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _Result()

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    assert runtime._alive(4242) is True
    assert calls
    assert calls[0][0][:3] == ["tasklist", "/FI", "PID eq 4242"]
    assert calls[0][1]["timeout"] == 2


def test_alive_windows_does_not_match_partial_pid(monkeypatch):
    from xskill import runtime

    class _Result:
        stdout = '"python.exe","4242","Console","1","10,000 K"\n'

    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: _Result())

    assert runtime._alive(42) is False
