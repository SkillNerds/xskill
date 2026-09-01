# xskill Xarena Algorithm Image

This is the submitter-container image for the SpreadsheetBench Xarena board.

It starts xskill, runs SpreadsheetBench training rollouts, collects graduated skills, and writes the result to the Xarena shared volume:

```text
/shared/skill/ALGO
/shared/skill/DONE
/shared/skill/skills/<name>/SKILL.md
```

Build from this directory:

```bash
TAG=main-xarena PUSH=1 LOAD_KIND=lb bash build.sh
```

Useful runtime variables:

```text
EVAL_MODEL=deepseek-v4-flash
XSKILL_WORKERS=3
XSKILL_MAX_TURNS=5
XSKILL_EPOCHS=4
XSKILL_VAL_BLOCK=true
XSKILL_VAL_BLOCK_TIMEOUT=1800
OUTPUT_DIR=/shared/out
```

`DEEPSEEK_API_KEY` and `DASHSCOPE_API_KEY` must be injected by the leaderboard job secret.
