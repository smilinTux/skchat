#!/usr/bin/env bash
# Install repo-tracked git hooks into .git/hooks/ as symlinks.
# Re-run any time hooks are added or after a fresh clone.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
HOOK_SRC_DIR="$ROOT/scripts/hooks"
HOOK_DST_DIR="$ROOT/.git/hooks"

if [[ ! -d "$HOOK_DST_DIR" ]]; then
  echo "no .git/hooks dir at $HOOK_DST_DIR — is this a git checkout?" >&2
  exit 1
fi

for src in "$HOOK_SRC_DIR"/*; do
  name="$(basename "$src")"
  [[ "$name" == "install.sh" ]] && continue
  [[ "$name" == "README"* ]] && continue
  chmod +x "$src"
  ln -sfn "../../scripts/hooks/$name" "$HOOK_DST_DIR/$name"
  echo "linked $name"
done
