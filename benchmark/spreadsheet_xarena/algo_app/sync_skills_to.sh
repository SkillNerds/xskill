#!/usr/bin/env bash
# Sync xskill MAIN-branch skills into a target .claude/skills dir.
#   usage: sync_skills_to.sh <DEST_SKILLS_DIR> [--mirror]
# Behaviour matches Phase-3a sync_skills.sh:
#   - copies ONLY skills whose git branch == main (real promotion); baby stubs skipped
#   - skips skills the daemon already symlinked into DEST (avoids copy/symlink collision)
#   - prunes the daemon's `.<name>.replaced-by-symlink` cruft
# --mirror (used for the val home): DEST is a plain dir not touched by the daemon,
#   so we do a clean full materialise (remove stale, copy all main skills).
set -uo pipefail
# Vendored reference script. The xskill algo entrypoint uses its own collect_skills;
# this is kept for compatibility/attribution. No hardcoded host path: the xskill skill
# dir is taken from env — set XSKILL_SKILL_DIR, or XSKILL_HOME (defaults to $HOME/.xskill).
XSKILL_SKILL_DIR=${XSKILL_SKILL_DIR:-${XSKILL_HOME:-$HOME/.xskill}/skill}
DEST=${1:?need dest skills dir}
MODE=${2:-}
GIT=$(command -v git)

mkdir -p "$DEST"
# prune daemon-collision cruft so the listing stays clean
find "$DEST" -maxdepth 1 -name '.*.replaced-by-symlink' -exec rm -rf {} + 2>/dev/null

if [ "$MODE" = "--mirror" ]; then
  # fresh materialise: clear non-hidden entries first
  find "$DEST" -maxdepth 1 -mindepth 1 -exec rm -rf {} + 2>/dev/null
fi

synced=0; linked=0; skipped=0
if [ -d "$XSKILL_SKILL_DIR" ]; then
  for d in "$XSKILL_SKILL_DIR"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ "${name#.}" != "$name" ] && continue
    [ -f "$d/SKILL.md" ] || continue
    if [ -d "$d/.git" ] && [ -n "$GIT" ]; then
      br=$("$GIT" -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null)
      [ "$br" = "main" ] || { skipped=$((skipped+1)); continue; }
    fi
    if [ "$MODE" != "--mirror" ] && [ -L "$DEST/$name" ]; then linked=$((linked+1)); continue; fi
    rm -rf "$DEST/$name"
    mkdir -p "$DEST/$name"
    cp "$d/SKILL.md" "$DEST/$name/SKILL.md"
    for extra in references scripts assets; do
      [ -d "$d/$extra" ] && cp -r "$d/$extra" "$DEST/$name/$extra"
    done
    synced=$((synced+1))
  done
fi
echo "[sync->$DEST] copied=$synced daemon-linked=$linked skipped(non-main)=$skipped"
ls -1 "$DEST" 2>/dev/null | grep -v '^\.' | sed 's/^/[sync]   /'
