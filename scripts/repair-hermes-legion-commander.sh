#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-}"
EXE="$HOME/.local/share/hermes-legion-commander/venv/bin/hermes-legion-commander"
if [[ ! -x "$EXE" ]]; then
  "$ROOT/scripts/install-hermes-legion-commander.sh" --wheel "$ROOT/dist/hermes_legion_commander-0.8.5-py3-none-any.whl" --expected-version 0.8.5 --recreate-environment --add-scripts-to-path
fi
for command in hermes codex claude git; do command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }; done
"$EXE" supervisor --repo-root "$ROOT" setup --force
[[ -f "$ROOT/config/model_council.local.toml" ]] && "$EXE" council --config "$ROOT/config/model_council.local.toml" workers --check
if [[ -n "$TARGET" && -f "$ROOT/config/model_council.local.toml" ]]; then
  "$EXE" council --config "$ROOT/config/model_council.local.toml" preflight --repo "$TARGET" --preview-chars 120
fi
printf 'Repair checks completed.\n'
