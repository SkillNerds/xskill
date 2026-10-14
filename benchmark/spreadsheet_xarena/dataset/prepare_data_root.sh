#!/usr/bin/env bash
# Prepare SpreadsheetBench workbook data for local Docker builds.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST:-$SCRIPT_DIR/data_root}"
SOURCE_DATA_ROOT="${SOURCE_DATA_ROOT:-}"
ZIP_URL="${ZIP_URL:-https://xskill.wiki/zip/xskill-compete.zip}"
CACHE_DIR="${CACHE_DIR:-$SCRIPT_DIR/.cache}"
ZIP_PATH="${ZIP_PATH:-$CACHE_DIR/xskill-compete.zip}"

if [ -n "$SOURCE_DATA_ROOT" ]; then
  if [ ! -d "$SOURCE_DATA_ROOT" ]; then
    echo "SOURCE_DATA_ROOT does not exist: $SOURCE_DATA_ROOT" >&2
    exit 1
  fi
  rm -rf "$DEST"
  mkdir -p "$DEST"
  rsync -a --delete "$SOURCE_DATA_ROOT/" "$DEST/"
  echo "prepared data_root from $SOURCE_DATA_ROOT -> $DEST"
  exit 0
fi

mkdir -p "$CACHE_DIR"
if [ ! -f "$ZIP_PATH" ]; then
  curl -L "$ZIP_URL" -o "$ZIP_PATH"
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

unzip -q "$ZIP_PATH" 'xskill-compete-pkg/data_root/*' -d "$TMP_DIR"
rm -rf "$DEST"
mkdir -p "$DEST"
rsync -a --delete "$TMP_DIR/xskill-compete-pkg/data_root/" "$DEST/"
echo "prepared data_root from $ZIP_PATH -> $DEST"
