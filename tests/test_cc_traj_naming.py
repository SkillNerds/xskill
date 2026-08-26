"""tests/test_cc_traj_naming.py — CC bridged 轨迹的 traj_id 命名规则

之前所有桥进来的 traj 都叫 ``traj_0001.md`` / ``traj_0002.md`` 这种纯
数字编号，事后翻文件不知道是哪个项目哪个 session 来的。改成

  traj_cc_<projectname>_<sid16>.md

其中：
  - 保留 ``traj_`` 前缀让 watcher.discover_trajectories 的 glob
    ``traj_*.md`` 仍能匹配
  - ``cc`` = ecosystem 来源（CC bridged）；未来 codex / opencode 可同模式
    扩展 ``traj_codex_...``
  - ``projectname`` 取 CC JSONL ``cwd`` 字段的 basename，不安全字符替换
    为下划线；空时 fallback 到 ``unknown``
  - ``sid16`` 是 session UUID 的前 16 个可进文件名字符（2026-08 起从 8
    放宽到 16，见 issue #234：前缀型会话 id 在 8 位下只剩 4 位有效字符，
    团队合并多人轨迹时会重名；旧 8 位命名的存量文件仍被识别，见
    ``lookup_bridged_markdown``）
"""
from __future__ import annotations

import json
from pathlib import Path


from xskill.ecosystems import _cc_traj_id, _sanitize_for_filename


def _write_jsonl(tmp_path: Path, events: list[dict]) -> Path:
    f = tmp_path / "sess.jsonl"
    f.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events),
                 encoding="utf-8")
    return f


class TestSanitize:
    def test_keeps_safe_chars(self):
        assert _sanitize_for_filename("hello-world.py_v2") == "hello-world.py_v2"

    def test_replaces_slashes_and_spaces(self):
        assert _sanitize_for_filename("/home/user my proj") == "home_user_my_proj"

    def test_truncates_to_maxlen(self):
        assert _sanitize_for_filename("a" * 50, maxlen=10) == "aaaaaaaaaa"

    def test_chinese_replaced(self):
        # 中文不在白名单 [A-Za-z0-9._-] 中
        out = _sanitize_for_filename("项目test")
        assert out == "____test" or "test" in out  # 4 个中文 → 4 个 _

    def test_empty_input(self):
        assert _sanitize_for_filename("") == ""


class TestCCTrajId:
    def test_uses_cwd_basename_and_sid_short(self, tmp_path):
        jsonl = _write_jsonl(tmp_path, [
            {"type": "user", "cwd": "/home/user/dataharness", "sessionId": "..."}
        ])
        traj_id = _cc_traj_id(jsonl, "f2eb54d4-0a5c-4f2f-97ec-bacf397fbde4")
        assert traj_id == "traj_cc_dataharness_f2eb54d4-0a5c-4f"

    def test_prefixed_session_id_keeps_16_effective_chars(self, tmp_path):
        """issue #234 的核心场景：``ses_`` 前缀 id 在 8 位下只剩 4 位有效
        字符，多人语料极易撞名；16 位后保留 12 位有效字符。"""
        jsonl = _write_jsonl(tmp_path, [{"type": "user", "cwd": "/x/y"}])
        traj_id = _cc_traj_id(jsonl, "ses_abc123def456ghi789")
        assert traj_id == "traj_cc_y_ses_abc123def456"

    def test_keeps_traj_prefix_so_watcher_glob_matches(self, tmp_path):
        jsonl = _write_jsonl(tmp_path, [{"type": "user", "cwd": "/x"}])
        traj_id = _cc_traj_id(jsonl, "abc12345-...")
        # watcher.discover_trajectories 走 traj_*.md，前缀必须是 traj_
        assert traj_id.startswith("traj_")

    def test_no_cwd_fallback_unknown(self, tmp_path):
        jsonl = _write_jsonl(tmp_path, [
            {"type": "queue-operation"},  # 没 cwd 的事件
        ])
        traj_id = _cc_traj_id(jsonl, "abcdef12-...")
        assert traj_id == "traj_cc_unknown_abcdef12"

    def test_first_cwd_wins(self, tmp_path):
        """JSONL 多事件，取**第一个**带 cwd 的事件——CC 一个 session 内 cwd 不变。"""
        jsonl = _write_jsonl(tmp_path, [
            {"type": "queue-operation"},
            {"type": "user", "cwd": "/home/user/projA"},
            {"type": "assistant", "cwd": "/home/user/projA"},
        ])
        traj_id = _cc_traj_id(jsonl, "12345678-...")
        assert traj_id == "traj_cc_projA_12345678"

    def test_chinese_project_name_sanitized(self, tmp_path):
        jsonl = _write_jsonl(tmp_path, [
            {"type": "user", "cwd": "/home/user/测试项目"},
        ])
        traj_id = _cc_traj_id(jsonl, "deadbeef-...")
        # 不该有中文，会被规整为 _
        assert "测试项目" not in traj_id
        assert traj_id.startswith("traj_cc_")
        assert traj_id.endswith("_deadbeef")

    def test_special_chars_in_path(self, tmp_path):
        jsonl = _write_jsonl(tmp_path, [
            {"type": "user", "cwd": "/home/user/my project (v2)"},
        ])
        traj_id = _cc_traj_id(jsonl, "abc12345-...")
        # 空格 / 括号被替换
        assert " " not in traj_id
        assert "(" not in traj_id

    def test_short_session_id(self, tmp_path):
        """sessionId 不足 8 字符时不报错，能用多少用多少。"""
        jsonl = _write_jsonl(tmp_path, [{"type": "user", "cwd": "/x/y"}])
        traj_id = _cc_traj_id(jsonl, "abc")
        assert traj_id == "traj_cc_y_abc"

    def test_missing_jsonl_file(self, tmp_path):
        """JSONL 路径不存在不抛错——降级取 cwd=""，project=unknown。"""
        traj_id = _cc_traj_id(tmp_path / "nope.jsonl", "abcdef12-...")
        assert traj_id == "traj_cc_unknown_abcdef12"
