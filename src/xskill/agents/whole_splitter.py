"""WholeTrajSplitter —— "整轨成 1 atom" 消融拆分器
================================================================================

消融实验（``atom.split_mode: whole`` / 环境变量 ``XSKILL_SPLIT_MODE=whole``）：
**跳过 TaskAgent 的"按用户意图切 atom + atom 聚类"拆分步**，把一条轨迹自
``last_offset`` 起的新增段 ``[last_offset, EOF)`` 构造成**恰好 1 个 AtomTask =
整条（新增）轨迹**，直接落 store，下游 index → cluster → SkillEdit 原样复用。

用来验证"atom 拆分到底有没有必要"——whole 模式下技能由整轨直接提炼，与
agentic 模式（细颗粒 atom 聚类后提炼）对照。

与 ``TaskAgent`` 的契约对齐（保证下游兼容）
============================================
- ``atom_id`` 同构命名 ``atom_<traj_id>_<NNNN>``（接 store 已有 atom 序号），
  ``cluster`` / ``SkillEdit`` 据此正常引用。
- ``offset_start`` / ``offset_end`` 为 **1-based 行号半开区间**，末段
  ``offset_end = total_lines + 1``（盖到 EOF），与 ``ReadTraj`` 行号约定一致。
- ``raw_segment`` = 该段原文（整条新增轨迹），``context_prefix`` 同 TaskAgent。
- ``source_model`` 继承 ``<traj>.json`` sidecar 的 ``model``。
- ``intent`` **纯启发式**抽该段第一条 User 请求的首行/首句（**绝不调 LLM**），
  ``summary`` 取首 ~200 字截断。

无新增内容（updated 但无新行）→ 产 0 atom（与现状一致）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xskill.agents.task_agent import (
    _extract_user_queries,
    _is_user_header,
    _sidecar_model,
)
from xskill.pipeline.atom import AtomTask, AtomTaskStore

logger = logging.getLogger("xskill.whole_splitter")


@dataclass
class WholeTrajSplitter:
    """把一条轨迹的新增段 ``[last_offset, EOF)`` 整体构造成 1 个 AtomTask。

    构造签名只取 ``store``——不需要 agno 工厂（不调 LLM）。``run`` 返回新落盘的
    atom 列表（0 或 1 个），与 ``TaskAgent.run`` 返回类型一致，让 runner 的
    ``_do_split`` 分支统一处理。
    """

    store: AtomTaskStore
    traj_root: Path | None = None

    def __post_init__(self) -> None:
        if self.traj_root is None:
            self.traj_root = Path(self.store.root)
        else:
            self.traj_root = Path(self.traj_root)

    # ── public API ────────────────────────────────────────────────

    def run(self, *, traj_id: str, traj_path: Path) -> list[AtomTask]:
        """整轨成 1 atom。无新增行返回空列表（产 0 atom，同现状）。"""
        traj_path = Path(traj_path)
        text = traj_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        source_model = _sidecar_model(traj_path)

        # 续接点：沿用 TaskAgent 约定——last_offset 为 0（首轮）时从第 1 行起。
        resume_line = self.store.last_offset(traj_id) or 1
        if resume_line > total_lines:
            return []  # updated 但无新增行 → 产 0 atom

        offset_start = resume_line
        offset_end = total_lines + 1  # 盖到 EOF（半开上界）
        raw_segment = "".join(lines[offset_start - 1:offset_end - 1])
        if not raw_segment.strip():
            return []  # 新增段全空白 → 产 0 atom

        intent = self._heuristic_intent(lines, offset_start)
        summary = self._heuristic_summary(raw_segment)

        prior_atoms = self.store.list_by_traj(traj_id)
        prior_atom = prior_atoms[-1] if prior_atoms else None
        next_idx = len(prior_atoms) + 1
        atom_id = f"atom_{traj_id}_{next_idx:04d}"

        atom = AtomTask(
            atom_id=atom_id,
            traj_id=traj_id,
            offset_start=offset_start,
            offset_end=offset_end,
            intent=intent,
            summary=summary,
            tags=[],
            used_skills=[],
            ux_score=None,
            pre_atom_id=prior_atom.atom_id if prior_atom is not None else None,
            post_atom_id=None,
            context_prefix=self._context_prefix(text, lines, offset_start),
            raw_segment=raw_segment,
            source_model=source_model,
        )

        # 续写场景：与 store 末尾 atom 链表衔接。
        if prior_atom is not None:
            prior_atom.post_atom_id = atom.atom_id
            self.store.save(prior_atom)
        self.store.save(atom)
        logger.info(
            "✓ whole-split %s → 1 atom [%d,%d) （整轨成 1 atom，未调 LLM）",
            traj_id, offset_start, offset_end,
        )
        return [atom]

    # ── 启发式抽取（零 LLM）───────────────────────────────────────

    @staticmethod
    def _heuristic_intent(lines: list[str], offset_start: int) -> str:
        """抽新增段内第一条 User 请求的首行/首句作 intent（≤80 字截断）。

        优先用 ``_extract_user_queries`` 抽到的 User 回合摘要（已含容噪过滤，
        与 agentic 模式同口径）；新增段内无真正 User 回合时退到段内首条非空、
        非 ``## ``/``###`` 标题行——保证 intent 永远非空（轨迹有内容即有 intent）。
        """
        queries = _extract_user_queries(lines)
        for ln, snip in queries:
            if ln >= offset_start and snip.strip():
                return snip.strip()[:80]
        # 段内无 User 回合摘要 → 取段内首条非空、非标题行。
        for raw in lines[offset_start - 1:]:
            t = raw.strip()
            if not t or t.startswith("## ") or _is_user_header(t):
                continue
            return t[:80]
        # 极端：整段只有 User 标题行无正文 → 用标题本身。
        for raw in lines[offset_start - 1:]:
            t = raw.strip()
            if t:
                return t[:80]
        return "whole-trajectory atom"

    @staticmethod
    def _heuristic_summary(raw_segment: str, max_chars: int = 200) -> str:
        """summary = 新增段原文首 ~200 字截断（去首尾空白）。"""
        body = raw_segment.strip()
        if len(body) <= max_chars:
            return body
        return body[:max_chars] + "…"

    @staticmethod
    def _context_prefix(text: str, lines: list[str], start_line: int) -> str:
        """与 TaskAgent._context_prefix 同口径：起始行前内容的省略表示。"""
        char_off = sum(len(ln) for ln in lines[:start_line - 1])
        if char_off <= 200:
            return text[:char_off]
        return text[:200] + f"\n\n[省略 {char_off - 200} 字符]\n\n"
