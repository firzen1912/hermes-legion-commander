#!/usr/bin/env bash
set -Eeuo pipefail
PROFILE="${1:-legion-supervisor}"
WORKER_A="${2:-legion-worker-a}"
WORKER_B="${3:-legion-worker-b}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXE="${HOME}/.local/share/hermes-legion-commander/venv/bin/hermes-legion-commander"
[[ -x "$EXE" ]] || { echo "Hermes Legion Commander is not installed: $EXE" >&2; exit 1; }
"$EXE" supervisor --profile "$PROFILE" --worker-profile-a "$WORKER_A" --worker-profile-b "$WORKER_B" --repo-root "$ROOT" setup
printf 'Supervisor ready: hermes -p %s chat -q "Show Hermes Legion Commander status."\n' "$PROFILE"
printf 'Generic workers: %s, %s\n' "$WORKER_A" "$WORKER_B"
