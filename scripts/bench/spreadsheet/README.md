# Spreadsheet 评测导入（第三步）

把榜上已经跑完的 Spreadsheet Full 评测收成四个合同文件。不训练、不调模型。

sub181 是 xskill 训练完用交出的 skill 评 280 题。sub182 是 SkillOpt：训练答题和改写走 Chat，评测走榜上的 single Claude Code exec，用的也是这次训出来的 skill。

```text
python3.11 scripts/bench/spreadsheet/import_leaderboard_eval.py \
  --output-dir runs/eval-spreadsheet-xskill-sub181-import

python3.11 scripts/bench/contract_import.py \
  --preset spreadsheet-sub182 \
  --output-dir runs/eval-spreadsheet-skillopt-sub182-import
```

榜上没有 Spreadsheet Full 280 题的 no-skill 记录，所以这一步不硬造那一趟。no-skill 的通路用 ALFWorld 的 sub86。
