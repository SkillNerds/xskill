# SpreadsheetBench

电子表格清洗与计算。划分 80、40、280，名单见 `manifests/spreadsheet_skillopt_id_split.json`。

做题：在 Claude Code 里用 Read 看表、Bash 跑脚本，再和作者标准答案表比对单元格。SkillOpt 答题端必须是 `claude_code_exec` 配 `env.mode=single`。

正式 xskill 训练把 val 的 40 题并进 train，共 120 题。
