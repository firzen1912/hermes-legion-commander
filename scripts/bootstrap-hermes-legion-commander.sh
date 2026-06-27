#!/usr/bin/env bash
# End-to-end Linux/WSL bootstrap for Hermes Legion Commander.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=""
PROFILE="legion-supervisor"
WORKER_A="legion-worker-a"
WORKER_B="legion-worker-b"
SKIP_TOOL_INSTALL=0
SKIP_AUTH=0
NON_INTERACTIVE=0
RESET_STATE=0
ALLOW_DIRTY=0
RUN_LIVE_SMOKE=0
VERSION="0.8.5"
STAMP="$(date +%Y%m%d-%H%M%S)"

usage() {
  cat <<'EOF'
Usage:
  bootstrap-hermes-legion-commander.sh --target-repo PATH [options]

Options:
  --commander-repo PATH
  --profile NAME
  --worker-profile-a NAME
  --worker-profile-b NAME
  --skip-tool-install
  --skip-authentication
  --non-interactive
  --reset-state
  --allow-dirty-target
  --run-live-smoke-tests
EOF
}
step() { printf '\n==> %s\n' "$1"; }
ok() { printf '[OK] %s\n' "$1"; }
warn() { printf '[WARN] %s\n' "$1" >&2; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-repo) TARGET="${2:?}"; shift 2 ;;
    --commander-repo) ROOT="${2:?}"; shift 2 ;;
    --profile) PROFILE="${2:?}"; shift 2 ;;
    --worker-profile-a) WORKER_A="${2:?}"; shift 2 ;;
    --worker-profile-b) WORKER_B="${2:?}"; shift 2 ;;
    --skip-tool-install) SKIP_TOOL_INSTALL=1; shift ;;
    --skip-authentication) SKIP_AUTH=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --reset-state) RESET_STATE=1; shift ;;
    --allow-dirty-target) ALLOW_DIRTY=1; shift ;;
    --run-live-smoke-tests) RUN_LIVE_SMOKE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done
[[ -n "$TARGET" ]] || { usage; die "--target-repo is required"; }
ROOT="$(cd "$ROOT" && pwd)"
TARGET="$(cd "$TARGET" && pwd)"
export PATH="$HOME/.local/bin:$HOME/bin:$PATH"

install_if_missing() {
  local name="$1"
  local command="$2"
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name -> $(command -v "$name")"
    return
  fi
  [[ "$SKIP_TOOL_INSTALL" -eq 0 ]] || die "$name is missing and --skip-tool-install was specified"
  step "Installing $name from its official installer"
  bash -lc "$command"
  export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
  command -v "$name" >/dev/null 2>&1 || die "$name installation completed but executable is unavailable"
  ok "$name -> $(command -v "$name")"
}

step "Resolving repositories"
WHEEL="$ROOT/dist/hermes_legion_commander-${VERSION}-py3-none-any.whl"
INSTALLER="$ROOT/scripts/install-hermes-legion-commander.sh"
[[ -f "$ROOT/pyproject.toml" ]] || die "Missing Commander repository: $ROOT"
[[ -f "$WHEEL" ]] || die "Missing wheel: $WHEEL"
[[ -x "$INSTALLER" ]] || die "Missing installer: $INSTALLER"
[[ -d "$TARGET" ]] || die "Missing target repository: $TARGET"
ok "Commander repository: $ROOT"
ok "Target repository: $TARGET"

step "Installing or resolving official prerequisites"
install_if_missing uv 'curl -LsSf https://astral.sh/uv/install.sh | sh'
install_if_missing hermes 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup'
install_if_missing codex 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh'
install_if_missing claude 'curl -fsSL https://claude.ai/install.sh | bash'

step "Verifying prerequisite versions"
uv --version
hermes --version
codex --version
claude --version

step "Ensuring Python 3.11 or newer"
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  if PYTHON_BIN="$(uv python find '>=3.11,<3.14' 2>/dev/null)" && [[ -x "$PYTHON_BIN" ]]; then
    :
  else
    uv python install 3.11
    PYTHON_BIN="$(uv python find 3.11)"
  fi
fi
[[ -x "$PYTHON_BIN" ]] || die "No usable Python 3.11 or newer interpreter was found"
ok "Python: $PYTHON_BIN"

step "Installing Hermes Legion Commander $VERSION"
"$INSTALLER" \
  --wheel "$WHEEL" \
  --expected-version "$VERSION" \
  --python "$PYTHON_BIN" \
  --recreate-environment \
  --add-scripts-to-path
EXE="$HOME/.local/share/hermes-legion-commander/venv/bin/hermes-legion-commander"
PY="$HOME/.local/share/hermes-legion-commander/venv/bin/python"
[[ -x "$EXE" ]] || die "Commander executable missing: $EXE"

step "Verifying target Git checkout"
command -v git >/dev/null 2>&1 || die "git is unavailable after Hermes installation"
git -C "$TARGET" rev-parse --is-inside-work-tree >/dev/null
if [[ "$ALLOW_DIRTY" -eq 0 && -n "$(git -C "$TARGET" status --short)" ]]; then
  git -C "$TARGET" status --short
  die "Target working tree is dirty. Commit/stash changes or use --allow-dirty-target."
fi
find "$TARGET/docs" -maxdepth 1 -type f -iname '*roadmap*.md' -print -quit | grep -q . ||
  die "No *roadmap*.md file exists under $TARGET/docs"

step "Archiving old configuration and optional state"
ARCHIVE="$HOME/.local/share/hermes-legion-commander-archives/$STAMP"
mkdir -p "$ARCHIVE"
CONFIG_DIR="$ROOT/config"
for file in model_council.local.toml checkpoint_competition.local.toml hermes-legion-environment.sh; do
  [[ -f "$CONFIG_DIR/$file" ]] && cp "$CONFIG_DIR/$file" "$ARCHIVE/"
done
STATE_ROOT="$HOME/.local/share/hermes-legion-commander/state"
if [[ "$RESET_STATE" -eq 1 && -d "$STATE_ROOT" ]]; then
  cp -a "$STATE_ROOT" "$ARCHIVE/"
  rm -rf "$STATE_ROOT"
fi
mkdir -p "$STATE_ROOT"
ok "Archive: $ARCHIVE"

step "Creating fresh local configurations"
COUNCIL="$CONFIG_DIR/model_council.local.toml"
CHECKPOINT="$CONFIG_DIR/checkpoint_competition.local.toml"
cp "$CONFIG_DIR/model_council.example.toml" "$COUNCIL"
cp "$CONFIG_DIR/checkpoint_competition.example.toml" "$CHECKPOINT"
"$PY" - "$COUNCIL" "$CHECKPOINT" "$TARGET" "$STATE_ROOT" <<'PY'
import pathlib, re, sys
council, checkpoint, target, state = map(pathlib.Path, sys.argv[1:])
repo = target.resolve().as_posix()
state = state.resolve().as_posix()
for path, state_name in ((council, "model-council"), (checkpoint, "checkpoint-competition")):
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r'^repo\s*=\s*".*"$', f'repo = "{repo}"', text, flags=re.M)
    text = re.sub(r'^state_dir\s*=\s*".*"$', f'state_dir = "{state}/{state_name}"', text, flags=re.M)
    text = re.sub(r'^research_dir\s*=\s*".*"$', f'research_dir = "{state}/research"', text, flags=re.M)
    text = re.sub(r'^roadmap_path\s*=\s*".*"$', 'roadmap_path = "docs/field-deployability-roadmap.md"', text, flags=re.M)
    text = re.sub(r'^plan\s*=\s*".*"$', 'plan = "docs/field-deployability-roadmap.md"', text, flags=re.M)
    text = text.replace('["python", "-m", "pytest", "-q"]', '["uv", "run", "python", "-m", "pytest", "-q"]')
    text = text.replace('["python", "-m", "ruff", "check"', '["uv", "run", "ruff", "check"')
    text = text.replace('version_test_command = ["python"', 'version_test_command = ["uv", "run", "python"')
    text = text.replace('version_experiment_command = ["python"]', 'version_experiment_command = ["uv", "run", "python"]')
    path.write_text(text, encoding="utf-8")
PY
"$PY" -P - "$COUNCIL" "$CHECKPOINT" <<'PY'
import sys, tomllib
for path in sys.argv[1:]:
    with open(path, "rb") as handle:
        tomllib.load(handle)
print("TOML valid")
PY

if [[ "$SKIP_AUTH" -eq 0 ]]; then
  step "Checking native CLI authentication"
  if ! codex login status; then
    [[ "$NON_INTERACTIVE" -eq 0 ]] || die "Codex is not authenticated in non-interactive mode"
    codex login
  fi
  if ! claude auth status; then
    [[ "$NON_INTERACTIVE" -eq 0 ]] || die "Claude Code is not authenticated in non-interactive mode"
    claude auth login
  fi
  step "Checking Hermes configuration"
  if ! hermes config check; then
    [[ "$NON_INTERACTIVE" -eq 0 ]] || die "Hermes requires setup in non-interactive mode"
    hermes setup
  fi
fi

step "Recreating Hermes supervisor and generic worker profiles"
PROFILE_ARCHIVE="$ARCHIVE/hermes-profiles"
mkdir -p "$PROFILE_ARCHIVE"
for name in "$PROFILE" "$WORKER_A" "$WORKER_B"; do
  if hermes profile list 2>/dev/null | sed 's/^[*[:space:]]*//' | grep -Fxq "$name"; then
    hermes profile export "$name" -o "$PROFILE_ARCHIVE/$name.tar.gz" || true
    hermes profile delete "$name" --yes
  fi
done
"$EXE" supervisor \
  --profile "$PROFILE" \
  --worker-profile-a "$WORKER_A" \
  --worker-profile-b "$WORKER_B" \
  --repo-root "$ROOT" \
  setup --clone --force

step "Running zero-model diagnostics and preflights"
doctor_args=(
  doctor
  --repo-root "$ROOT"
  --target-repo "$TARGET"
  --council-config "$COUNCIL"
  --checkpoint-config "$CHECKPOINT"
)
[[ "$SKIP_AUTH" -eq 1 ]] && doctor_args+=(--skip-auth)
"$EXE" "${doctor_args[@]}"
"$EXE" council --config "$COUNCIL" workers --check
"$EXE" council --config "$COUNCIL" preflight --repo "$TARGET" --preview-chars 300
"$EXE" checkpoint --config "$CHECKPOINT" --repo "$TARGET" workers
"$EXE" checkpoint --config "$CHECKPOINT" --repo "$TARGET" preflight --preview-chars 300

if [[ "$RUN_LIVE_SMOKE" -eq 1 ]]; then
  step "Running live low-cost smoke tests"
  CODEX_OUT="$(mktemp)"
  printf '%s\n' 'Reply with exactly CODEX_OK. Do not modify files.' |
    codex --sandbox read-only exec --output-last-message "$CODEX_OUT" -
  grep -Eq '\bCODEX_OK\b' "$CODEX_OUT" || die "Codex live smoke test failed"
  claude -p 'Reply with exactly CLAUDE_OK. Do not modify files.' --output-format json |
    grep -Eq '\bCLAUDE_OK\b' || die "Claude live smoke test failed"
  hermes -p "$PROFILE" chat -q 'Reply with exactly HERMES_OK. Do not use tools.' |
    grep -Eq '\bHERMES_OK\b' || die "Hermes supervisor live smoke test failed"
fi

step "Writing reusable environment and report"
ENV_FILE="$CONFIG_DIR/hermes-legion-environment.sh"
cat >"$ENV_FILE" <<EOF
export COMMANDER_REPO="$ROOT"
export TARGET_REPO="$TARGET"
export COUNCIL_CONFIG="$COUNCIL"
export CHECKPOINT_CONFIG="$CHECKPOINT"
export COMMANDER_EXE="$EXE"
export COMMANDER_PYTHON="$PY"
export HERMES_PROFILE="$PROFILE"
export HERMES_WORKER_PROFILE_A="$WORKER_A"
export HERMES_WORKER_PROFILE_B="$WORKER_B"
EOF
chmod +x "$ENV_FILE"
"$PY" - "$CONFIG_DIR/bootstrap-report.json" "$ROOT" "$TARGET" "$COUNCIL" "$CHECKPOINT" "$ARCHIVE" <<'PY'
import datetime, json, pathlib, sys
path, root, target, council, checkpoint, archive = sys.argv[1:]
payload = {
    "ready": True,
    "version": "0.8.5",
    "platform": "linux",
    "commander_repo": root,
    "target_repo": target,
    "council_config": council,
    "checkpoint_config": checkpoint,
    "archive": archive,
    "profiles": ["legion-supervisor", "legion-worker-a", "legion-worker-b"],
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
pathlib.Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

printf '\nHermes Legion Commander is ready.\n'
printf 'Load environment: source %q\n' "$ENV_FILE"
printf 'Doctor: %q doctor --repo-root %q --target-repo %q --council-config %q --checkpoint-config %q\n' \
  "$EXE" "$ROOT" "$TARGET" "$COUNCIL" "$CHECKPOINT"
