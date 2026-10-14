#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# SkillOpt — ALFWorld training launch script
#
# Prerequisites:
#   pip install -e ".[alfworld]"
#   pip install alfworld[full] && alfworld-download
#
# Usage:
#   bash scripts/run_alfworld.sh
#   bash scripts/run_alfworld.sh --num_epochs 2 --edit_budget 6
#   bash scripts/run_alfworld.sh --split_dir /path/to/alfworld_split
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

# ALFWorld data — uses ~/.cache/alfworld by default
export ALFWORLD_DATA="${ALFWORLD_DATA:-${HOME}/.cache/alfworld}"

if [ ! -d "${ALFWORLD_DATA}/json_2.1.1" ]; then
    echo "ERROR: ALFWorld data not found at ${ALFWORLD_DATA}/json_2.1.1"
    echo ""
    echo "To download ALFWorld data, run:"
    echo "  pip install alfworld[full]"
    echo "  alfworld-download"
    echo ""
    echo "Or set ALFWORLD_DATA to the directory containing json_2.1.1/"
    exit 1
fi

OPTIMIZER_MODEL="${OPTIMIZER_MODEL:-gpt-5.5}"
TARGET_MODEL="${TARGET_MODEL:-gpt-5.5}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEFAULT_OUT_ROOT="${PROJECT_ROOT}/outputs/skillopt_alfworld_${TARGET_MODEL}_${TIMESTAMP}"

echo "============================================================"
echo "  SkillOpt — ALFWorld Training"
echo "============================================================"
echo "  Optimizer:       ${OPTIMIZER_MODEL}"
echo "  Target:       ${TARGET_MODEL}"
echo "  ALFWORLD_DATA: ${ALFWORLD_DATA}"
echo "  Output:        ${DEFAULT_OUT_ROOT}"
echo "============================================================"

cd "${PROJECT_ROOT}"

python scripts/train.py \
    --config configs/alfworld/default.yaml \
    --optimizer_model "${OPTIMIZER_MODEL}" \
    --target_model "${TARGET_MODEL}" \
    --out_root "${DEFAULT_OUT_ROOT}" \
    "$@"

echo ""
echo "Done! Results saved to: ${DEFAULT_OUT_ROOT}"
