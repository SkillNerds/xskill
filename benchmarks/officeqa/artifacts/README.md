# 实验产出存放说明

真实的实验跑分数据请勿直接提交到 Git 仓库中。建议存放在本目录，或保存在本地缓存路径 `~/.cache/xskill/officeqa/runs/<run_id>/` 下。

每次实验通常包含以下文件（一次实验对应一个算法设定与一个做题模型）：

- `run_config.json`（或评测程序输出的 `run.json`）：记录本次实验的具体配置（包含 `model`）
- `train_provenance.json`（若包含训练流程）：记录训练来源与数据设定
- `skill/` 目录及其 SHA-256 哈希值：训练完成后固定并用于评测的技能包
- `results.jsonl`：逐题评测明细
- `summary.json`：汇总统计指标

如果需要测试多个模型，请为每个模型建立独立的目录或配置独立的 `run_id`，不要将不同模型的结果混在同一个 `results.jsonl` 中。

每次实验具体需要记录的字段清单，请参考上级目录中的 `what-these-files-are.md`。
