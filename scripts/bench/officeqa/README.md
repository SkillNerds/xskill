# OfficeQA 评测导入（第二步）

这里只做一件事：把榜上已经跑完的冻结评测，收成 `benchmarks/` 里定的四个文件。不训练、不调模型、不重做题。共用实现在 `scripts/bench/contract_import.py`。

评测脚本在同目录 `run.py` / `evaluate.py`，走 `xskill bench`。和旁边轨迹拆分器无关。不要改 `scripts/bench/README.md` 和拆分器目录。

默认对着 sub200：冻结的是 sub197 交出去的主 skill，OfficeQA Full 测试集 172 题。fail 和 timeout 从榜上的 `fail_reason` 区分，`claude exited with rc=124` 记成 timeout。

本机有 kind 产物时：

```text
python3.11 scripts/bench/officeqa/import_leaderboard_eval.py \
  --from-kind-job eval-sub-200-5aa5bd \
  --output-dir runs/eval-officeqa-xskill-sub200-import
```

已经拷到本地目录时，目录里放 `results.jsonl`，可选 `meta.json`：

```text
python3.11 scripts/bench/officeqa/import_leaderboard_eval.py \
  --source-dir /path/to/export \
  --output-dir runs/eval-officeqa-xskill-sub200-import
```

写出来的 `run_config.json` 里 tools 仍是合同规定的 Read、Bash、Skill；当时实际用过 Write、Edit，写在 notes 里。这趟分不是论文对照分。`train_provenance.json` 标明沿用 sub197，并 val 按史实是 false。用量标 missing。
