from __future__ import annotations

import os
import time
from pathlib import Path

from xskill.team.client.collector import TeamCollector


def _bridge(home_root: Path) -> Path:
    """标准 bridge 目录：<home_root>/.xskill/cc_sessions/。"""
    b = home_root / ".xskill" / "cc_sessions"
    b.mkdir(parents=True, exist_ok=True)
    return b


def test_pending_returns_quiet_unuploaded_md(tmp_path):
    home = tmp_path / "home"
    bridge = _bridge(home)
    # 一个"静默已久"的 traj_*.md
    old = bridge / "traj_cc_x_001.md"
    old.write_text("# old body", encoding="utf-8")
    old_time = time.time() - 600
    os.utime(old, (old_time, old_time))
    # 一个"刚改过"的 traj_*.md
    fresh = bridge / "traj_cc_x_002.md"
    fresh.write_text("# fresh", encoding="utf-8")

    col = TeamCollector(cursor_path=tmp_path / "cursor.json",
                        quiet_seconds=180, min_change_interval=0,
                        home_root=home)
    pending = col.pending()
    ids = {p.traj_id for p in pending}
    assert "traj_cc_x_001" in ids        # 静默够久
    assert "traj_cc_x_002" not in ids    # 太新，可能还在写


def test_mark_uploaded_excludes_next_time(tmp_path):
    home = tmp_path / "home"
    bridge = _bridge(home)
    md = bridge / "traj_cc_x_001.md"
    md.write_text("# body", encoding="utf-8")
    old = time.time() - 600
    os.utime(md, (old, old))

    col = TeamCollector(cursor_path=tmp_path / "cursor.json",
                        quiet_seconds=180, min_change_interval=0,
                        home_root=home)
    p = col.pending()[0]
    col.mark_uploaded(p.traj_id, p.sha256)
    assert col.pending() == []           # 已上传，不再吐

    # 内容变了 → 重新吐（增量）
    md.write_text("# body changed", encoding="utf-8")
    os.utime(md, (old, old))
    assert len(col.pending()) == 1


def test_pending_carries_sidecar_model(tmp_path):
    home = tmp_path / "home"
    bridge = _bridge(home)
    md = bridge / "traj_cc_x_001.md"
    md.write_text("# body", encoding="utf-8")
    # 同名 .json sidecar 带 model
    (bridge / "traj_cc_x_001.json").write_text(
        '{"model": "claude-opus-4-7", "cwd": "/secret"}', encoding="utf-8")
    old = time.time() - 600
    os.utime(md, (old, old))
    col = TeamCollector(cursor_path=tmp_path / "cursor.json",
                        quiet_seconds=180, min_change_interval=0,
                        home_root=home)
    p = col.pending()[0]
    assert p.model == "claude-opus-4-7"


def test_pending_no_sidecar_model_empty(tmp_path):
    home = tmp_path / "home"
    bridge = _bridge(home)
    md = bridge / "traj_cc_x_001.md"
    md.write_text("# body", encoding="utf-8")          # 没有 .json sidecar
    old = time.time() - 600
    os.utime(md, (old, old))
    col = TeamCollector(cursor_path=tmp_path / "cursor.json",
                        quiet_seconds=180, min_change_interval=0,
                        home_root=home)
    p = col.pending()[0]
    assert p.model == ""                                # 不报错，空串


class _Clock:
    """可手动推进的注入式时钟。"""
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def _write_changing(md: Path, body: str, clock: "_Clock"):
    """写入内容并把 mtime 设成"远早于现在"（绕开 quiet 闸,只考验去抖闸）。"""
    md.write_text(body, encoding="utf-8")
    old = clock.t - 10_000
    os.utime(md, (old, old))


class TestUploadThrottle:
    """客户端按 hash-变更去抖,10 分钟内的连续增量必须被拦截,
    只有轨迹稳定满窗口后才作为一段完整轨迹放行。"""

    def test_rapid_changes_are_intercepted(self, tmp_path):
        home = tmp_path / "home"
        bridge = _bridge(home)
        md = bridge / "traj_cc_x_001.md"
        clock = _Clock()
        col = TeamCollector(
            cursor_path=tmp_path / "cursor.json", quiet_seconds=0,
            min_change_interval=600, home_root=home, time_fn=clock)

        # 模拟代理每 30s 追加一次工具调用 → 内容(hash)频繁变更
        for i in range(20):
            _write_changing(md, f"# body\nstep {i}\n", clock)
            assert col.pending() == [], f"第 {i} 次快速变更不应放行"
            clock.advance(30)   # 30s 后又变 —— 远小于 10 分钟
        assert col.pending() == []

    def test_stable_trajectory_released_after_window(self, tmp_path):
        home = tmp_path / "home"
        bridge = _bridge(home)
        md = bridge / "traj_cc_x_001.md"
        clock = _Clock()
        col = TeamCollector(
            cursor_path=tmp_path / "cursor.json", quiet_seconds=0,
            min_change_interval=600, home_root=home, time_fn=clock)

        full = "# body\n## User\nq1\n## Assistant\na1\n## User\nq2\n## Assistant\na2\n"
        _write_changing(md, full, clock)
        assert col.pending() == []          # 首次观测：记录 hash + 起始时间
        clock.advance(599)
        assert col.pending() == []          # 还没满 10 分钟 → 继续拦
        clock.advance(2)
        out = col.pending()                 # 稳定满窗口 → 放行
        assert len(out) == 1
        assert out[0].traj_id == "traj_cc_x_001"
        # 完整性：所有 ## User 回合都在,没有半截
        assert out[0].content.count("## User") == 2
        assert "q1" in out[0].content and "q2" in out[0].content

    def test_debounce_state_persists_across_instances(self, tmp_path):
        home = tmp_path / "home"
        bridge = _bridge(home)
        md = bridge / "traj_cc_x_001.md"
        clock = _Clock()
        cur = tmp_path / "cursor.json"
        col = TeamCollector(cursor_path=cur, quiet_seconds=0,
                            min_change_interval=600, home_root=home, time_fn=clock)
        _write_changing(md, "stable body\n", clock)
        assert col.pending() == []          # 记录 since=now

        # 新进程（重启）：用同一 cursor_path 复用去抖状态,计时不从头开始
        clock.advance(601)
        col2 = TeamCollector(cursor_path=cur, quiet_seconds=0,
                             min_change_interval=600, home_root=home, time_fn=clock)
        assert len(col2.pending()) == 1     # 跨实例继承计时 → 已满窗口放行


def test_redaction_applied_to_content(tmp_path):
    home = tmp_path / "home"
    bridge = _bridge(home)
    md = bridge / "traj_cc_x_001.md"
    md.write_text('key = "sk-abcdEFGH1234567890wxyz"', encoding="utf-8")
    old = time.time() - 600
    os.utime(md, (old, old))
    col = TeamCollector(cursor_path=tmp_path / "cursor.json",
                        quiet_seconds=180, min_change_interval=0,
                        home_root=home)
    p = col.pending()[0]
    assert "sk-abcdEFGH1234567890wxyz" not in p.content
    assert "[REDACTED]" in p.content
