#!/usr/bin/env bash
set -euo pipefail
config=${1:?usage: migrate-to-codex-claude.sh CONFIG.toml}
stamp=$(date +%Y%m%d-%H%M%S)
cp "$config" "$config.pre-two-worker-$stamp.bak"
python - "$config" <<'PY2'
from pathlib import Path
import re,sys
p=Path(sys.argv[1]); t=p.read_text(encoding='utf-8-sig')
t=re.sub(r'(?m)^roadmap_plan_reviewer\s*=\s*"(?:google|gemini)"\s*$', 'roadmap_plan_reviewer = "gpt"', t)
t=re.sub(r'(?m)^researcher\s*=\s*"(?:google|gemini)"\s*$', 'researcher = "gpt"', t)
t=re.sub(r'(?m)^literature_reviewer\s*=\s*"gpt"\s*$', 'literature_reviewer = "claude"', t)
t=re.sub(r'(?ms)^\[agents\.(?:google|gemini)\]\s*.*?(?=^\[agents\.|^\[research\]|\Z)', '', t)
t=re.sub(r'(?m)^literature_reviewer\s*=\s*"(?:gpt|google|gemini)"\s*$', 'literature_reviewer = "claude"', t)
p.write_text(t.strip()+"\n",encoding='utf-8')
PY2
echo "Migrated to Codex + Claude Code only: $config"
