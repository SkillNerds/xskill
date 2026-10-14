#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# SkillOpt — SpreadsheetBench training launch script
#
# Usage:
#   bash scripts/run_spreadsheetbench.sh --split_dir /path/to/split --data_root /path/to/data
#   bash scripts/run_spreadsheetbench.sh --num_epochs 2 --edit_budget 6
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

OPTIMIZER_MODEL="${OPTIMIZER_MODEL:-gpt-5.5}"
TARGET_MODEL="${TARGET_MODEL:-gpt-5.5}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEFAULT_OUT_ROOT="${PROJECT_ROOT}/outputs/skillopt_spreadsheetbench_${TARGET_MODEL}_${TIMESTAMP}"

echo "============================================================"
echo "  SkillOpt — SpreadsheetBench Training"
echo "============================================================"
echo "  Optimizer:  ${OPTIMIZER_MODEL}"
echo "  Target:  ${TARGET_MODEL}"
echo "============================================================"

cd "${PROJECT_ROOT}"

python scripts/train.py \
    --config configs/spreadsheetbench/default.yaml \
    --optimizer_model "${OPTIMIZER_MODEL}" \
    --target_model "${TARGET_MODEL}" \
    --out_root "${DEFAULT_OUT_ROOT}" \
    "$@"

echo ""
echo "Done! Results saved to: ${DEFAULT_OUT_ROOT}"
