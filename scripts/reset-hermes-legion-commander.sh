#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:?usage: reset-hermes-legion-commander.sh /path/to/target-repo}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$HOME/.local/share/hermes-legion-commander-archives/$STAMP"
mkdir -p "$ARCHIVE"
for dir in "$HOME/.local/share/legion-commander" "$HOME/.local/share/hermes-legion-commander"; do
  if [[ -d "$dir" ]]; then
    cp -a "$dir" "$ARCHIVE/"
    [[ "$dir" == *"/legion-commander" ]] && rm -rf "$dir"
  fi
done
rm -rf "$HOME/.local/share/hermes-legion-commander/state" "$HOME/.local/share/hermes-legion-commander/venv"
"$ROOT/scripts/install-hermes-legion-commander.sh" --wheel "$ROOT/dist/hermes_legion_commander-0.8.5-py3-none-any.whl" --expected-version 0.8.5 --recreate-environment --add-scripts-to-path
cp "$ROOT/config/model_council.example.toml" "$ROOT/config/model_council.local.toml"
cp "$ROOT/config/checkpoint_competition.example.toml" "$ROOT/config/checkpoint_competition.local.toml"
python3 - "$ROOT/config/model_council.local.toml" "$ROOT/config/checkpoint_competition.local.toml" "$TARGET" <<'PY'
import pathlib,re,sys
for file in sys.argv[1:3]:
 p=pathlib.Path(file); t=p.read_text(); repo=pathlib.Path(sys.argv[3]).resolve().as_posix()
 t=re.sub(r'^repo\s*=\s*".*"$',f'repo = "{repo}"',t,flags=re.M)
 t=re.sub(r'^roadmap_path\s*=\s*".*"$','roadmap_path = "docs/field-deployability-roadmap.md"',t,flags=re.M)
 t=re.sub(r'^plan\s*=\s*".*"$','plan = "docs/field-deployability-roadmap.md"',t,flags=re.M)
 p.write_text(t,encoding='utf-8')
PY
EXE="$HOME/.local/share/hermes-legion-commander/venv/bin/hermes-legion-commander"
"$EXE" supervisor --repo-root "$ROOT" setup --force
"$EXE" council --config "$ROOT/config/model_council.local.toml" preflight --repo "$TARGET" --preview-chars 120
printf 'Reset complete. Archive: %s\n' "$ARCHIVE"
