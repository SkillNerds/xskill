"""
test_ingest_settle_mask.py — 入库完成屏障(settle barrier) + 去壳掩码(mask_patterns)
====================================================================================

背景 bug（实证 session 03fe589e）：JsonlIngester 对 session JSONL 是
"出现即读、按 sid 去重后永不回头"——session 刚开跑文件刚出现就被整读桥接，
定格成只有题面的残骸；后续写完的解题内容无人回头重读。

覆盖：
  T1. ingest_config：配置段读取 + 显式默认 + 坏正则 fail-loud
  T2. settle barrier：mtime 距今 < settle 秒的源文件不入库；到期后入库
  T3. 续写重转换：jsonl 先写一半→扫描→补全→再扫描，最终 md 含补全内容
      （无屏障/无增长重读的旧实现下此测试必失败）
  T4. 增长重转换触发 atom 重置（复用 rebuild --traj 的 reset_trajectories）
  T5. mask_patterns：命中替换为占位符；默认空列表完全不改文本
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from xskill.ecosystems._shared import JsonlIngester, submit_trajectory
from xskill.ecosystems.claude_code import CC_SPEC


# ──────────────────────────────────────────────────────
# helpers — 造一个可增量续写的 CC session JSONL
# ──────────────────────────────────────────────────────

SID = "03fe589e-aaaa-bbbb-cccc-0123456789ab"

HARNESS_PROMPT = (
    "HARNESS_BEGIN 你是评测代理。请严格按照以下评分规则完成本题，"
    "完成后输出 SUBMIT。 HARNESS_END"
)


def _ev_user(text: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text},
        "sessionId": SID, "cwd": "/home/me/proj",
        "timestamp": "2026-06-11T10:00:00.000Z",
    })


def _ev_assistant(text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
        "sessionId": SID,
    })


def _half_session() -> str:
    """session 刚开跑：只有 harness 题面，没有解题内容。"""
    return _ev_user(HARNESS_PROMPT) + "\n"


def _full_tail() -> str:
    """session 后续补写的解题内容（含可断言的 marker）。"""
    return "\n".join([
        _ev_assistant("我先分析题目结构。"),
        _ev_assistant("SOLUTION_MARKER: 最终解法是动态规划，复杂度 O(n)。"),
    ]) + "\n"


def _backdate(p: Path, seconds: float) -> None:
    """把文件 mtime 拨回 seconds 秒前（模拟"已停止写入很久"）。"""
    t = time.time() - seconds
    os.utime(p, (t, t))


@pytest.fixture()
def cc_home(tmp_path: Path) -> Path:
    """隔离 HOME：<home>/.claude/projects/<cwd-hash>/<sid>.jsonl 结构。"""
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-home-me-proj").mkdir(parents=True)
    return home


def _session_path(home: Path) -> Path:
    return home / ".claude" / "projects" / "-home-me-proj" / f"{SID}.jsonl"


# ──────────────────────────────────────────────────────
# T1. ingest_config — 配置段读取
# ──────────────────────────────────────────────────────

class TestIngestConfig:
    def test_defaults_when_config_missing(self, tmp_path):
        from xskill.config import ingest_config
        cfg = ingest_config(path=tmp_path / "nonexistent.yaml")
        assert cfg["settle_seconds"] == 120.0
        assert cfg["mask_patterns"] == []

    def test_reads_section_from_yaml(self, tmp_path):
        from xskill.config import ingest_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "ingest:\n"
            "  settle_seconds: 7\n"
            "  mask_patterns:\n"
            "    - 'HARNESS_BEGIN.*?HARNESS_END'\n",
            encoding="utf-8",
        )
        cfg = ingest_config(path=cfg_file)
        assert cfg["settle_seconds"] == 7.0
        assert cfg["mask_patterns"] == ["HARNESS_BEGIN.*?HARNESS_END"]

    def test_bad_regex_fails_loud(self, tmp_path):
        from xskill.config import ingest_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "ingest:\n  mask_patterns:\n    - '([unclosed'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="mask_patterns"):
            ingest_config(path=cfg_file)

    def test_non_list_mask_patterns_fails_loud(self, tmp_path):
        from xskill.config import ingest_config
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "ingest:\n  mask_patterns: not-a-list\n", encoding="utf-8",
        )
        with pytest.raises(ValueError, match="mask_patterns"):
            ingest_config(path=cfg_file)

    def test_template_contains_ingest_section(self):
        """CONFIG_TEMPLATE 是配置格式唯一真源——ingest 段必须进模板。"""
        from xskill.config import CONFIG_TEMPLATE
        assert "ingest:" in CONFIG_TEMPLATE
        assert "settle_seconds" in CONFIG_TEMPLATE
        assert "mask_patterns" in CONFIG_TEMPLATE


# ──────────────────────────────────────────────────────
# T2. settle barrier — 还在写的文件不入库
# ──────────────────────────────────────────────────────

class TestSettleBarrier:
    def test_fresh_file_not_ingested(self, cc_home, tmp_path):
        """mtime 距今 < settle 秒 → 认为还在写，本轮跳过。"""
        _session_path(cc_home).write_text(_half_session(), encoding="utf-8")
        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=3600)
        out = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=set())
        assert out == []
        assert list(target.glob("traj_*.md")) == []

    def test_settled_file_ingested(self, cc_home, tmp_path):
        """mtime 距今 >= settle 秒 → 正常入库。"""
        sp = _session_path(cc_home)
        sp.write_text(_half_session() + _full_tail(), encoding="utf-8")
        _backdate(sp, 7200)
        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=3600)
        seen: set[str] = set()
        out = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert len(out) == 1
        assert SID in seen
        md = Path(out[0]["path"]).read_text(encoding="utf-8")
        assert "SOLUTION_MARKER" in md

    def test_settle_zero_disables_barrier(self, cc_home, tmp_path):
        """settle=0 只禁时间屏障；明确完成前不入 seen，完成后下一轮入库。"""
        session_path = _session_path(cc_home)
        session_path.write_text(_half_session(), encoding="utf-8")
        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=0)
        seen: set[str] = set()

        assert ing.scan_and_bridge(
            target,
            home_root=cc_home,
            seen_sessions=seen,
        ) == []
        assert SID not in seen

        with session_path.open("a", encoding="utf-8") as session_file:
            session_file.write(
                _full_tail() + json.dumps({"type": "last-prompt"}) + "\n"
            )
        completed = ing.scan_and_bridge(
            target,
            home_root=cc_home,
            seen_sessions=seen,
        )
        assert len(completed) == 1
        assert SID in seen


# ──────────────────────────────────────────────────────
# T3. 续写重转换 — 半截入库后补全，再扫描必须重读
# ──────────────────────────────────────────────────────

class TestRegrowthRebridge:
    def test_halfwritten_then_completed_is_rebridged(self, cc_home, tmp_path):
        """核心回归测试：jsonl 先写一半→扫描→补全→再扫描，
        最终入库 md 必须含补全内容。旧实现（sid 入 seen 后永不回头）必失败。"""
        sp = _session_path(cc_home)
        sp.write_text(_half_session(), encoding="utf-8")
        _backdate(sp, 7200)

        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=60)
        seen: set[str] = set()

        out1 = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert len(out1) == 1
        md_path = Path(out1[0]["path"])
        assert "SOLUTION_MARKER" not in md_path.read_text(encoding="utf-8")
        # 时间压缩模拟：首次桥接发生在"很久前"（真实时序里续写必然晚于首次转换）
        _backdate(md_path, 7000)

        # session 继续跑，补全解题内容；写完后已"停笔"超过 settle。
        sp.write_text(_half_session() + _full_tail(), encoding="utf-8")
        _backdate(sp, 90)

        out2 = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert len(out2) == 1
        assert out2[0]["rebridged"] is True
        assert Path(out2[0]["path"]) == md_path  # 覆盖同一条轨迹，不另起新文件
        final_md = md_path.read_text(encoding="utf-8")
        assert "SOLUTION_MARKER" in final_md

    def test_growth_within_settle_window_waits(self, cc_home, tmp_path):
        """已入库 + 源文件又增长，但 mtime 还在 settle 期内 → 等下一轮。"""
        sp = _session_path(cc_home)
        sp.write_text(_half_session(), encoding="utf-8")
        _backdate(sp, 7200)

        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=3600)
        seen: set[str] = set()
        ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)

        sp.write_text(_half_session() + _full_tail(), encoding="utf-8")  # mtime=now
        out = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert out == []

    def test_unchanged_session_not_rebridged(self, cc_home, tmp_path):
        """没增长的 session 不重复入库（幂等不回退）。"""
        sp = _session_path(cc_home)
        sp.write_text(_half_session() + _full_tail(), encoding="utf-8")
        _backdate(sp, 7200)

        target = tmp_path / "bridge"
        ing = JsonlIngester(CC_SPEC, settle_seconds=60)
        seen: set[str] = set()
        ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        out = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert out == []


# ──────────────────────────────────────────────────────
# T4. 增长重转换触发 atom 重置（rebuild --traj 同款重置逻辑）
# ──────────────────────────────────────────────────────

class TestRebridgeResetsAtoms:
    def test_rebridge_resets_traj_atoms_and_status(
        self, cc_home, tmp_path, monkeypatch,
    ):
        from unittest.mock import Mock

        from xskill.pipeline import registry as registry_module
        from xskill.pipeline.registry import (
            discover_trajectories, register_dir, update_traj_status,
            get_status_counts,
        )

        sp = _session_path(cc_home)
        sp.write_text(_half_session(), encoding="utf-8")
        _backdate(sp, 7200)

        target = tmp_path / "bridge"
        instance_db_path = tmp_path / "instance" / "registry.db"
        global_db_path = tmp_path / "global" / "registry.db"
        monkeypatch.setattr(
            registry_module,
            "get_registry_db_path",
            Mock(return_value=global_db_path),
        )
        ing = JsonlIngester(
            CC_SPEC,
            settle_seconds=60,
            registry_db_path=instance_db_path,
        )
        seen: set[str] = set()
        out1 = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        md_path = Path(out1[0]["path"])
        traj_id = out1[0]["traj_id"]
        # 时间压缩模拟：首次桥接发生在"很久前"
        _backdate(md_path, 7000)

        # 模拟 watcher 已经处理完这条残骸轨迹：登记 + 标 done + 拆出 atom + 建索引
        wd_id = register_dir(
            target,
            ecosystem="claude_code",
            db_path=instance_db_path,
        )
        discover_trajectories(
            wd_id, target, db_path=instance_db_path
        )
        update_traj_status(
            wd_id,
            md_path.name,
            "done",
            db_path=instance_db_path,
        )
        atom_file = target / traj_id / "tasks" / "atom_0001.json"
        atom_file.parent.mkdir(parents=True)
        atom_file.write_text("{}", encoding="utf-8")
        (target / "index.pkl").write_bytes(b"stale")

        # 源文件补全 → 重转换必须重置该轨迹的 atom / 索引 / 状态
        sp.write_text(_half_session() + _full_tail(), encoding="utf-8")
        _backdate(sp, 90)
        out2 = ing.scan_and_bridge(target, home_root=cc_home, seen_sessions=seen)
        assert len(out2) == 1

        assert not atom_file.exists()
        assert not (target / "index.pkl").exists()
        counts = get_status_counts(
            wd_id, db_path=instance_db_path
        )
        assert counts.get("discovered", 0) == 1
        assert counts.get("done", 0) == 0
        assert not global_db_path.exists()


# ──────────────────────────────────────────────────────
# T5. mask_patterns — 去壳掩码
# ──────────────────────────────────────────────────────

class TestMaskPatterns:
    def _content(self) -> str:
        return _half_session() + _full_tail()

    def test_mask_replaces_matched_segment(self, tmp_path):
        out = submit_trajectory(
            content=self._content(),
            format="claude_code_jsonl",
            traj_dir=tmp_path,
            mask_patterns=[r"HARNESS_BEGIN.*?HARNESS_END"],
        )
        md = Path(out["path"]).read_text(encoding="utf-8")
        assert "[MASKED_HARNESS_PROMPT]" in md
        assert "评分规则" not in md          # 题壳正文被剥掉
        assert "SOLUTION_MARKER" in md       # 解题内容不受影响

    def test_default_empty_patterns_keep_text_intact(self, tmp_path):
        out = submit_trajectory(
            content=self._content(),
            format="claude_code_jsonl",
            traj_dir=tmp_path,
        )
        md = Path(out["path"]).read_text(encoding="utf-8")
        assert "评分规则" in md
        assert "[MASKED_HARNESS_PROMPT]" not in md

    def test_mask_patterns_from_config(self, tmp_path, monkeypatch):
        """不显式传参时从 config 的 ingest.mask_patterns 取。"""
        monkeypatch.setattr(
            "xskill.ecosystems._shared.ingest_config",
            lambda path=None: {"settle_seconds": 0.0,
                               "mask_patterns": [r"HARNESS_BEGIN.*?HARNESS_END"]},
        )
        out = submit_trajectory(
            content=self._content(),
            format="claude_code_jsonl",
            traj_dir=tmp_path,
        )
        md = Path(out["path"]).read_text(encoding="utf-8")
        assert "[MASKED_HARNESS_PROMPT]" in md

    def test_multiline_prompt_maskable_with_inline_flag(self, tmp_path):
        """跨行题壳用内联 (?s) flag 掩掉——md 里换行已是真实换行。"""
        content = _ev_user("HARNESS_BEGIN\n第一行规则\n第二行规则\nHARNESS_END") \
            + "\n" + _full_tail()
        out = submit_trajectory(
            content=content,
            format="claude_code_jsonl",
            traj_dir=tmp_path,
            mask_patterns=[r"(?s)HARNESS_BEGIN.*?HARNESS_END"],
        )
        md = Path(out["path"]).read_text(encoding="utf-8")
        assert "[MASKED_HARNESS_PROMPT]" in md
        assert "第一行规则" not in md
