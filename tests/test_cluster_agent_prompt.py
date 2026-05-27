"""cluster agent SYSTEM_PROMPT_TEMPLATE 新分档表的内容回归测试。

prompt 是模型行为的唯一指令源，硬约束（"任何分数都不允许 return"、ws=1
不开新 skill 等）必须在 prompt 里以确定字符串呈现，否则等于没说。
"""
from __future__ import annotations

from xskill.agents.task_cluster_agent import SYSTEM_PROMPT_TEMPLATE


class TestNewWeightscoreTable:
    def test_header_forbids_return(self):
        """分档表表头必须明示"任何分数都不允许直接 return"。"""
        assert "任何分数都不允许直接 return" in SYSTEM_PROMPT_TEMPLATE, (
            "新分档表必须明示不允许 silent drop"
        )

    def test_2_to_3_band_says_still_add(self):
        """2-3 分边缘 atom 必须含"仍要 add_task_to_skill"——
        防止 LLM 把"边缘"理解成"不写"。
        """
        assert "仍要 add_task_to_skill" in SYSTEM_PROMPT_TEMPLATE, (
            "2-3 分边缘 atom 必须明示仍要 add_task_to_skill"
        )

    def test_score_1_band_forbids_new_skill(self):
        """ws=1 的 atom 不能新开 skill——必须明示"≥7 才新建"门槛。"""
        # 同时校验 1 分段的关键短语 "挑 desc 最不远的那个强 add" 存在
        assert "不要为 ws=1 atom 新开 skill" in SYSTEM_PROMPT_TEMPLATE, (
            "ws=1 段必须含'不要为 ws=1 atom 新开 skill'守住新建门槛"
        )
        assert "≥7 才新建" in SYSTEM_PROMPT_TEMPLATE, (
            "ws=1 段必须复述 ≥7 才新建 这条硬门槛"
        )

    def test_score_band_examples_listed(self):
        """2-3 分典型形态四条子弹要全部列出，让模型在边缘 case 准确分类。"""
        for phrase in (
            "用户 query 里提到术语",
            "任务铺背景",
            "是什么",
            "session 中途偏题",
        ):
            assert phrase in SYSTEM_PROMPT_TEMPLATE, (
                f"2-3 分典型形态缺失: {phrase!r}"
            )

    def test_10_still_triggers_skilledit(self):
        """10 分立即触发 SkillEdit 的语义不能被新分档表丢掉。"""
        assert "立即触发 SkillEdit" in SYSTEM_PROMPT_TEMPLATE
