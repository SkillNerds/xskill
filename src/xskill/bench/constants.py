from __future__ import annotations

FAILURE_POLICY = (
    "timeout 标记为超时状态，计算准确率时与 fail 同样计为未通过（计入分母，不计入分子）"
)

BENCHMARKS = ("officeqa", "spreadsheet", "alfworld")
ALGOS = ("xskill", "skillopt", "noskill")

BENCHMARK_TITLES = {
    "officeqa": "OfficeQA",
    "spreadsheet": "SpreadsheetBench",
    "alfworld": "ALFWorld",
}

HARNESS_ALIASES = {
    "native_cc": "claude_code_native_skills",
    "claude_code_native_skills": "claude_code_native_skills",
    "native": "claude_code_native_skills",
    "claude_code": "claude_code_native_skills",
    "claude_code_exec": "skillopt_claude_code_exec",
    "skillopt_claude_code_exec": "skillopt_claude_code_exec",
    "exec": "skillopt_claude_code_exec",
}

HARNESS_DISPLAY = {
    "claude_code_native_skills": "native_cc",
    "skillopt_claude_code_exec": "claude_code_exec",
}

XSKILL_TOOLS = ["Read", "Bash", "Skill"]
SKILLOPT_TOOLS = ["Read", "Bash"]

HARNESS_NOTES = {
    "claude_code_native_skills": (
        "xskill 官方原生模式，skill 作为标准包安装，由 Skill 工具调用"
    ),
    "skillopt_claude_code_exec": (
        "SkillOpt 官方原生模式，skill.md 作为本地工作区说明文档直接供模型阅读"
    ),
}

OFFICEQA_SCORER = {
    "id": "skillopt.envs.officeqa.evaluator.evaluate",
    "module": "skillopt.envs.officeqa.evaluator",
    "metric": "em",
    "soft_metric": "f1",
    "commit": "02cdead11f635546f11e7669b08a96ce823d6377",
    "sha256": "49dad2a2bdede9665541102fe47ca74ab2a0cc05ad84d2f8ca047f223c1b4aef",
    "notes": "SkillOpt 官方归一化整串匹配为 hard，token F1 为 soft。不用 Databricks reward.py。",
}
SPREADSHEET_SCORER = {
    "id": "spreadsheetbench.cell_compare",
    "metric": "hard",
    "notes": "逐单元格比对作者标准答案表。",
}
ALFWORLD_SCORER = {
    "id": "alfworld.env_won",
    "metric": "hard",
    "notes": "规定步数内环境返回 won 算通过。",
}

SCORERS = {
    "officeqa": OFFICEQA_SCORER,
    "spreadsheet": SPREADSHEET_SCORER,
    "alfworld": ALFWORLD_SCORER,
}

OFFICIAL_COUNTS = {
    "officeqa": {"train": 50, "val": 24, "test": 172},
    "spreadsheet": {"train": 80, "val": 40, "test": 280},
    "alfworld": {"train": 39, "val": 18, "test": 134},
}
