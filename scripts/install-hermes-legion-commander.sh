#!/usr/bin/env bash
#
# Hermes Legion Commander installer v1.4.0
#
# Installs Commander in a dedicated virtual environment rather than modifying a
# system-managed, uv-managed, Homebrew-managed, or distro-managed base Python.

set -Eeuo pipefail

PYTHON_BIN=""
WHEEL_PATH=""
SOURCE_PATH=""
EXPECTED_VERSION=""
INSTALL_ROOT="${HOME}/.local/share/hermes-legion-commander"
ADD_SCRIPTS_TO_PATH=0
RECREATE_ENVIRONMENT=0
SKIP_PIP_UPGRADE=0

usage() {
  cat <<'EOF'
Usage:
  install-hermes-legion-commander.sh --wheel PATH [options]
  install-hermes-legion-commander.sh --source PATH [options]

Options:
  --python PATH              Base Python used only to create the virtual env
  --wheel PATH               Install from a wheel
  --source PATH              Install from a source tree with pyproject.toml
  --expected-version VERSION Verify the installed version
  --install-root PATH        Default: ~/.local/share/hermes-legion-commander
  --add-scripts-to-path      Add the venv bin directory to ~/.profile
  --recreate-environment     Delete and recreate the dedicated venv
  --skip-pip-upgrade         Do not upgrade pip/setuptools/wheel in the venv
  -h, --help                 Show help
EOF
}

step() {
  printf '\n==> %s\n' "$1"
}

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      PYTHON_BIN="${2:?Missing value for --python}"
      shift 2
      ;;
    --wheel)
      WHEEL_PATH="${2:?Missing value for --wheel}"
      shift 2
      ;;
    --source)
      SOURCE_PATH="${2:?Missing value for --source}"
      shift 2
      ;;
    --expected-version)
      EXPECTED_VERSION="${2:?Missing value for --expected-version}"
      shift 2
      ;;
    --install-root)
      INSTALL_ROOT="${2:?Missing value for --install-root}"
      shift 2
      ;;
    --add-scripts-to-path)
      ADD_SCRIPTS_TO_PATH=1
      shift
      ;;
    --recreate-environment)
      RECREATE_ENVIRONMENT=1
      shift
      ;;
    --skip-pip-upgrade)
      SKIP_PIP_UPGRADE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

[[ -z "$WHEEL_PATH" || -z "$SOURCE_PATH" ]] ||
  die "Use either --wheel or --source, not both."
[[ -n "$WHEEL_PATH" || -n "$SOURCE_PATH" ]] ||
  die "One of --wheel or --source is required."

unset PYTHONHOME PYTHONPATH PYTHONUSERBASE PIP_PREFIX PIP_TARGET VIRTUAL_ENV || true
export PYTHONSAFEPATH=1

resolve_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    [[ -x "$PYTHON_BIN" ]] || die "Python is not executable: $PYTHON_BIN"
    printf '%s\n' "$PYTHON_BIN"
    return
  fi

  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import os,sys;assert os.__file__;print(sys.executable)' >/dev/null 2>&1; then
        command -v "$candidate"
        return
      fi
    fi
  done

  die "No usable Python interpreter found. Retry with --python PATH."
}

PYTHON_BIN="$(resolve_python)"
printf 'Selected base Python: %s\n' "$PYTHON_BIN"

step "Checking base Python"
"$PYTHON_BIN" - <<'PY'
import os
import pathlib
import sys
import sysconfig

stdlib = sysconfig.get_path("stdlib")
if not stdlib or not pathlib.Path(stdlib).is_dir():
    raise SystemExit(f"Missing standard library: {stdlib!r}")
if not getattr(os, "__file__", None) or not pathlib.Path(os.__file__).is_file():
    raise SystemExit(f"Cannot locate os module: {getattr(os, '__file__', None)!r}")
print("Executable:", sys.executable)
print("Prefix:", sys.prefix)
print("Standard library:", stdlib)
PY

VENV_DIR="${INSTALL_ROOT}/venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_BIN="${VENV_DIR}/bin"

printf 'Installation root: %s\n' "$INSTALL_ROOT"
printf 'Virtual environment: %s\n' "$VENV_DIR"

if [[ "$RECREATE_ENVIRONMENT" -eq 1 && -d "$VENV_DIR" ]]; then
  step "Recreating dedicated environment"
  rm -rf "$VENV_DIR"
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  step "Creating dedicated Commander virtual environment"
  mkdir -p "$INSTALL_ROOT"
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    if command -v uv >/dev/null 2>&1; then
      uv venv --python "$PYTHON_BIN" "$VENV_DIR"
    else
      die "Could not create venv. Install the Python venv module or uv."
    fi
  fi
fi

[[ -x "$VENV_PYTHON" ]] || die "Virtual-environment Python was not created."

step "Ensuring pip inside the dedicated environment"
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
  "$VENV_PYTHON" -m ensurepip --upgrade
fi

if [[ "$SKIP_PIP_UPGRADE" -eq 0 ]]; then
  "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
fi

step "Removing stale Commander files from the dedicated environment"
"$VENV_PYTHON" -m pip uninstall -y hermes-legion-commander legion-commander || true

"$VENV_PYTHON" - <<'PY'
import pathlib
import shutil
import sysconfig

purelib = pathlib.Path(sysconfig.get_path("purelib"))
scripts = pathlib.Path(sysconfig.get_path("scripts"))
targets = []

if purelib.is_dir():
    for child in purelib.iterdir():
        name = child.name
        if (
            name == "hermes_legion_commander"
            or (name.startswith("hermes_legion_commander-") and name.endswith(".dist-info"))
            or (name.startswith("hermes_legion_commander") and name.endswith(".egg-info"))
            or name == "legion_commander"
            or (name.startswith("legion_commander-") and name.endswith(".dist-info"))
            or (name.startswith("legion_commander") and name.endswith(".egg-info"))
        ):
            targets.append(child)

if scripts.is_dir():
    for child in scripts.iterdir():
        if (child.name.startswith("hermes-legion-commander") or child.name.startswith("hermes_legion_commander")
            or child.name.startswith("legion-commander") or child.name.startswith("legion_commander")):
            targets.append(child)

for target in targets:
    print("Removing", target)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
PY

step "Installing Hermes Legion Commander"
if [[ -n "$WHEEL_PATH" ]]; then
  [[ -f "$WHEEL_PATH" ]] || die "Wheel does not exist: $WHEEL_PATH"
  "$VENV_PYTHON" -m pip install --no-cache-dir --force-reinstall "$WHEEL_PATH"
else
  [[ -f "$SOURCE_PATH/pyproject.toml" ]] ||
    die "Source directory does not contain pyproject.toml: $SOURCE_PATH"
  "$VENV_PYTHON" -m pip install --no-cache-dir --force-reinstall "$SOURCE_PATH"
fi

step "Verifying installation"
VERIFY_JSON="$("$VENV_PYTHON" - <<'PY'
import importlib.metadata as md
import json
import pathlib
import sys
import sysconfig

purelib = pathlib.Path(sysconfig.get_path("purelib")).resolve()
matches = []
for dist in md.distributions(path=[str(purelib)]):
    if (dist.metadata.get("Name") or "").lower() == "hermes-legion-commander":
        matches.append({
            "version": dist.version,
            "metadata_path": str(pathlib.Path(getattr(dist, "_path", "")).resolve()),
        })

if len(matches) != 1:
    raise SystemExit(
        f"Expected exactly one Commander distribution in {purelib}; found {len(matches)}"
    )

import hermes_legion_commander

loaded_from = pathlib.Path(hermes_legion_commander.__file__).resolve()
try:
    loaded_from.relative_to(purelib)
    loaded_from_venv = True
except ValueError:
    loaded_from_venv = False

if not loaded_from_venv:
    raise SystemExit(f"Commander imported from outside the venv: {loaded_from}")

print(json.dumps({
    "python": sys.executable,
    "purelib": str(purelib),
    "loaded_from": str(loaded_from),
    "loaded_from_venv": loaded_from_venv,
    "installed_version": matches[0]["version"],
    "matching_distributions": matches,
}))
PY
)"
printf '%s\n' "$VERIFY_JSON"

MATCH_COUNT="$("$VENV_PYTHON" -P -c 'import importlib.metadata as m,sysconfig;print(sum(1 for d in m.distributions(path=[sysconfig.get_path("purelib")]) if (d.metadata.get("Name") or "").lower()=="hermes-legion-commander"))')"
[[ "$MATCH_COUNT" == "1" ]] ||
  die "Expected one Commander distribution in the dedicated environment; found $MATCH_COUNT."

if [[ -n "$EXPECTED_VERSION" ]]; then
  INSTALLED_VERSION="$("$VENV_PYTHON" -P -c 'import importlib.metadata as m,sysconfig;d=[x for x in m.distributions(path=[sysconfig.get_path("purelib")]) if (x.metadata.get("Name") or "").lower()=="hermes-legion-commander"];print(d[0].version)')"
  [[ "$INSTALLED_VERSION" == "$EXPECTED_VERSION" ]] ||
    die "Expected version $EXPECTED_VERSION but installed $INSTALLED_VERSION."
fi

if [[ "$ADD_SCRIPTS_TO_PATH" -eq 1 ]]; then
  step "Adding the dedicated environment to PATH"
  LINE="export PATH=\"$VENV_BIN:\$PATH\""
  touch "$HOME/.profile"
  grep -Fqx "$LINE" "$HOME/.profile" || printf '\n%s\n' "$LINE" >> "$HOME/.profile"
  export PATH="$VENV_BIN:$PATH"
fi

printf '\nHermes Legion Commander installation is healthy.\n'
printf 'Dedicated Python:\n  %s\n' "$VENV_PYTHON"
printf 'Direct launcher:\n  %s/hermes-legion-commander\n' "$VENV_BIN"
printf 'Module invocation:\n  "%s" -P -m hermes_legion_commander.cli --help\n' "$VENV_PYTHON"

if [[ "$ADD_SCRIPTS_TO_PATH" -eq 1 ]]; then
  printf 'Direct command is available now and in future shells:\n  hermes-legion-commander --help\n'
else
  printf 'Use the full launcher path or rerun with --add-scripts-to-path.\n'
fi
