# ALFWorld 评测导入（第三步）

把榜上已经跑完的 ALFWorld Full 评测收成四个合同文件。不训练、不调模型。这一步先承认评测还是 Chat ReAct，只把四个文件收齐。改成 Native Claude Code 是第五步。

```text
python3.11 scripts/bench/alfworld/import_leaderboard_eval.py \
  --output-dir runs/eval-alfworld-xskill-sub81-import

python3.11 scripts/bench/contract_import.py \
  --preset alfworld-sub80 \
  --output-dir runs/eval-alfworld-skillopt-sub80-import

python3.11 scripts/bench/contract_import.py \
  --preset alfworld-sub86 \
  --output-dir runs/eval-alfworld-noskill-sub86-import
```

sub81 是 xskill，sub80 是 SkillOpt，sub86 是 no-skill。这三趟都在同一张 134 关试卷上。
