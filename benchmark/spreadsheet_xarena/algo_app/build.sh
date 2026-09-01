#!/usr/bin/env bash
# Build the Xarena-compatible xskill SpreadsheetBench algorithm image.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

REG="${REG:-localhost:5000}"
IMAGE_REPO="${IMAGE_REPO:-p_user1/algo-xskill}"
TAG="${TAG:-main-xarena}"
IMG="$REG/$IMAGE_REPO:$TAG"

DATA_ROOT="${DATA_ROOT:-$BENCH_DIR/dataset/data_root}"
SKILLOPT_SRC="${SKILLOPT_SRC:-$BENCH_DIR/third_party/SkillOpt}"
PUSH="${PUSH:-0}"
LOAD_KIND="${LOAD_KIND:-}"
PREPARE_DATA="${PREPARE_DATA:-1}"

require_dir() {
  local path=$1
  local label=$2
  if [ ! -d "$path" ]; then
    echo "missing $label: $path" >&2
    exit 1
  fi
}

if [ ! -d "$DATA_ROOT" ] && [ "$PREPARE_DATA" = "1" ]; then
  bash "$BENCH_DIR/dataset/prepare_data_root.sh"
fi

require_dir "$DATA_ROOT" "SpreadsheetBench dataset"
require_dir "$SKILLOPT_SRC" "SkillOpt rollout harness"

cd "$SCRIPT_DIR"
rm -rf _ctx
mkdir -p _ctx

rsync -a --delete \
  --exclude .git \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude .pytest_cache \
  --exclude .mypy_cache \
  --exclude .ruff_cache \
  --exclude .key \
  --exclude '.env' \
  --exclude '*.pem' \
  --exclude '*.key' \
  --exclude 'benchmark/spreadsheet_xarena/algo_app/_ctx' \
  --exclude 'benchmark/spreadsheet_xarena/dataset' \
  --exclude 'benchmark/spreadsheet_xarena/third_party' \
  "$REPO_ROOT/" _ctx/xskill/

rsync -a --delete \
  --exclude .git \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  --exclude .pytest_cache \
  --exclude '.env' \
  "$SKILLOPT_SRC/" _ctx/SkillOpt/

rsync -a --delete "$DATA_ROOT/" _ctx/data_root/
cp "$SCRIPT_DIR/sync_skills_to.sh" _ctx/sync_skills_to.sh

docker build -t "$IMG" .

if [ "$PUSH" = "1" ]; then
  docker push "$IMG"
fi

if [ -n "$LOAD_KIND" ]; then
  kind load docker-image "$IMG" --name "$LOAD_KIND"
fi

echo "built $IMG"
