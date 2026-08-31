# 实验产出放哪里

真实跑分不要默认提交进 Git。可以放在本目录，或放在 `~/.cache/xskill/officeqa/runs/<run_id>/`。

每次实验通常会有（一次实验 = 一个算法设定 + 一个做题模型）：

- `run_config.json`（或 runner 写的 `run.json`）：这一次怎么跑（含 `model`）
- 若是训练：`train_provenance.json`
- 冻结技能：`skill/` 及其 SHA-256
- `results.jsonl`：逐题结果
- `summary.json`：汇总数字

要测多个模型：每个模型单独一个目录或 `run_id`，不要混在同一份 `results.jsonl` 里。

每趟具体要统计哪些字段，见上级目录 `what-these-files-are.md` 文首清单。
