# Spreadsheet Xarena Benchmark

This directory packages the xskill SpreadsheetBench submission for Xarena.

## Layout

```text
benchmark/spreadsheet_xarena/
  algo_app/                  # submitter-container image source
  dataset/train_split/        # lightweight train/val/test task split files
  dataset/prepare_data_root.sh # downloads or copies workbook data before build
  third_party/SkillOpt/       # rollout harness used during training
```

`algo_app` is the algorithm image. It trains xskill inside the Xarena job and writes the skill package to the shared volume:

```text
/shared/skill/ALGO
/shared/skill/DONE
/shared/skill/skills/<skill-name>/SKILL.md
```

The evaluator image is still owned by the leaderboard board. `third_party/SkillOpt` is only used as the rollout harness for training trajectories; it is not the evaluator container.

## Build

From this directory:

```bash
cd algo_app
TAG=main-xarena PUSH=1 LOAD_KIND=lb bash build.sh
```

By default the image name is:

```text
localhost:5000/p_user1/algo-xskill:<TAG>
```

`build.sh` copies the current repository checkout into the Docker build context, so changes on this MR branch are included in the image. It stages `third_party/SkillOpt` and a prepared `dataset/data_root` into `_ctx`.

The workbook data is not committed. If `dataset/data_root` is missing, `build.sh` calls `dataset/prepare_data_root.sh`, which downloads `https://xskill.wiki/zip/xskill-compete.zip` and extracts the 100-task SpreadsheetBench package. To use an existing local dataset instead:

```bash
DATA_ROOT=/path/to/data_root TAG=main-xarena bash algo_app/build.sh
```

## Submit

Submit the built image to the Spreadsheet leaderboard board with Xarena env vars similar to:

```text
EVAL_MODEL=deepseek-v4-flash
XSKILL_WORKERS=3
XSKILL_MAX_TURNS=5
XSKILL_EPOCHS=4
XSKILL_VAL_BLOCK=true
XSKILL_VAL_BLOCK_TIMEOUT=1800
OUTPUT_DIR=/shared/out
```

The API keys should come from the leaderboard Kubernetes secret:

```text
DEEPSEEK_API_KEY
DASHSCOPE_API_KEY
```

Do not put API keys into this repository or into `env_text`.
