# Hermes Legion Commander

Hermes Legion Commander is a repository-agnostic orchestration system for **Hermes Agent**, **Codex CLI**, and **Claude Code**.

Hermes is the operator-facing **harness operator**. Hermes Legion Commander is the deterministic execution engine. Codex CLI and Claude Code are the native workers selected per dispatch contract.

## Architecture

```text
Hermes Agent profile: legion-supervisor
├── generic Hermes profile: legion-worker-a
├── generic Hermes profile: legion-worker-b
└── hermes-legion-commander
    ├── collaborating mode (council)
    │   └── supervisor assigns each agent a role, runtime, permission, model, and effort; auto-continues the range
    ├── competing mode (convergence)
    │   ├── isolated candidate assignments
    │   ├── opponent cross-validation and owner polish assignments
    │   ├── cross-judging assignments
    │   ├── convergence assignments in a third worktree
    │   └── final read-only verification assignments
    └── alternating mode (rapid alternate)
        └── one worker implements one version, then stops and hands off to the other to continue
```

Codex and Claude do not share provider-private conversation history. Every run instead owns canonical provider-neutral memory:

```text
shared-context/
├── CONTEXT.md
├── campaign-brief.md
├── shared-memory.md
├── stage-index.jsonl
├── runtime.json
├── learning-ledger.jsonl
├── learning-summary.json
├── prompt-lessons.md
├── scope-routing-ledger.jsonl
├── scope-routing-summary.md
├── routing-decisions/
├── repo-map/
├── artifacts/
│   ├── candidates/
│   ├── cross-validation/
│   ├── converged/
│   └── final-verification/
└── events/
```

Every worker gets an immutable stage snapshot under `worker-context/`. The supervisor hashes the snapshot before and after execution and rejects unexpected mutation. Competitive cross-review and final-verification stages are also checked for unintended reviewed-worktree mutation.

## Safety invariants

Hermes Legion Commander never automatically merges, pushes, deploys, tags, publishes, releases, changes credentials, or operates hardware. Dangerous-intent, massive-diff, and roadmap-update gates require explicit human approval.

All mutable work occurs in isolated Git worktrees. Competition candidates never share a worktree. Alternating mode uses one worktree but only one writer at a time.

## Repository contents

```text
hermes-legion-commander/
├── hermes_legion_commander/
│   ├── cli.py
│   ├── supervisor.py
│   ├── model_council.py
│   ├── checkpoint_competition.py
│   ├── worker_runtime.py
│   └── roadmap.py
├── config/
│   ├── model_council.example.toml
│   ├── checkpoint_competition.example.toml
│   └── hermes_supervisor.example.toml
├── scripts/
│   ├── install-hermes-legion-commander.ps1
│   ├── install-hermes-legion-commander.sh
│   ├── setup-hermes-supervisor.ps1
│   ├── setup-hermes-supervisor.sh
│   ├── reset-hermes-legion-commander.ps1
│   ├── reset-hermes-legion-commander.sh
│   ├── repair-hermes-legion-commander.ps1
│   └── repair-hermes-legion-commander.sh
├── tests/
└── dist/hermes_legion_commander-1.7.0-py3-none-any.whl
```

## Prerequisites

Install and authenticate:

```text
hermes
codex
claude
git
Python 3.11+
```

For Windows development, WSL2 is the most predictable environment for Hermes and Git worktrees. Native PowerShell remains supported.

## Install on Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass

.\scripts\install-hermes-legion-commander.ps1 `
  -WheelPath ".\dist\hermes_legion_commander-1.7.0-py3-none-any.whl" `
  -ExpectedVersion "1.7.0" `
  -RecreateEnvironment `
  -AddScriptsToUserPath
```

The dedicated environment is:

```text
%LOCALAPPDATA%\HermesLegionCommander\venv
```

Verify:

```powershell
$CommanderExe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"
$CommanderPython = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\python.exe"

& $CommanderPython -P -c "import importlib.metadata as m; print(m.version('hermes-legion-commander'))"
& $CommanderExe --help
```

The old `legion-commander` executable is retained as a deprecated compatibility alias.

## Install on Linux or macOS

```bash
chmod +x scripts/install-hermes-legion-commander.sh

./scripts/install-hermes-legion-commander.sh \
  --wheel ./dist/hermes_legion_commander-1.7.0-py3-none-any.whl \
  --expected-version 1.7.0 \
  --recreate-environment \
  --add-scripts-to-path
```

## Clean reset

The reset scripts archive old `LegionCommander` and `HermesLegionCommander` state, reinstall v1.7.0, create fresh council/checkpoint configs, configure the Hermes supervisor profile, and run zero-model preflight checks.

Windows:

```powershell
.\scripts\reset-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo"
```

Linux/macOS:

```bash
./scripts/reset-hermes-legion-commander.sh /path/to/target-repo
```

## Repair

```powershell
.\scripts\repair-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo" `
  -Reinstall
```

Repair verifies the package, `hermes`, `codex`, `claude`, the supervisor profile, worker resolution, and local roadmap preflight.

## Configure the Hermes supervisor

```powershell
.\scripts\setup-hermes-supervisor.ps1 -Profile "legion-supervisor" -Force
```

The setup creates or repairs:

```text
~/.hermes/profiles/legion-supervisor/SOUL.md
~/.hermes/profiles/legion-supervisor/skills/hermes-legion-commander/SKILL.md
```

It does not copy Codex or Claude credentials into Hermes. Native CLI authentication remains separate.

Preview the exact command and Hermes prompt without invoking a model:

```powershell
& $CommanderExe supervisor `
  --repo-root $PWD `
  print-command `
  --mode alternating `
  --config .\config\model_council.local.toml `
  --repo C:\path\to\target-repo `
  --from-version 51 `
  --to-version 57
```

Ask Hermes to launch it:

```powershell
& $CommanderExe supervisor `
  --profile legion-supervisor `
  --repo-root $PWD `
  run `
  --mode alternating `
  --config .\config\model_council.local.toml `
  --repo C:\path\to\target-repo `
  --from-version 51 `
  --to-version 57
```

Hermes receives an exact Commander command and is instructed not to call Codex or Claude directly.

Read durable status without invoking a model:

```powershell
& $CommanderExe supervisor status `
  --state-dir "$env:LOCALAPPDATA\HermesLegionCommander\state\model-council" `
  --run-id $RunId
```


## Hermes SOUL and goal-contract model

The `legion-supervisor` profile is deliberately prohibited from becoming a hidden coder or reviewer. It translates operator intent into a persistent goal contract, launches Commander, maintains the durable ledger, surfaces approvals and blockers, and reports evidence.

The installed profile includes:

```text
~/.hermes/profiles/legion-supervisor/
├── SOUL.md
└── skills/hermes-legion-commander/
    ├── SKILL.md
    ├── GOAL-CONTRACT.md
    └── HANDOFF-SCHEMA.md
```

The goal contract records objective, bounded scope, constraints, acceptance criteria, forbidden actions, required checks, handoff evidence, and human gates. Builder self-report remains provisional; review returns `PASS`, `BLOCKED`, or `NEEDS_HUMAN`. A blocked review creates a scoped fix contract and a delta re-review rather than a vague restart.

Preview the generated profile without invoking a model:

```powershell
& $CommanderExe supervisor show-soul
& $CommanderExe supervisor show-skill
& $CommanderExe supervisor show-goal-contract
& $CommanderExe supervisor show-handoff-schema
```

Repository-controlled copies are under `profiles/legion-supervisor/`.

## Three execution modes

Hermes Legion Commander runs three distinct modes, each its own top-level command.
Two auto-continue across a version range in one run; the third stops at each
version and hands off.

- **`collaborating`** — collaborative council: multiple roles (research,
  literature, prototype, polish, security assurance) collaborate on each version,
  and the run auto-continues across the whole range.
- **`competing`** — competitive convergence: two independent candidates build each
  version, are judged, and converge; auto-continues across the range.
- **`alternating`** — rapid alternate: a single chosen worker implements one
  version, then the run stops and hands the baton to the other worker
  (codex↔claude) to continue the next version. A fast single-worker relay with a
  human or scheduler deciding each handoff.

```
hermes-legion-commander collaborating --config <config> campaign --from-version 51 --to-version 57
hermes-legion-commander competing     --config <config> run --from-version 51 --to-version 57
hermes-legion-commander alternating   --config <config> --version 51 --worker codex --to-version 57
```

The former command names `council` and `checkpoint` still work as deprecated
aliases for `collaborating` and `competing` (they print a deprecation warning);
prefer the new names. The TOML config sections are unchanged (`[council]` for
collaborating/alternating, `[competition]` for competing).

## Collaborating mode (council)

Create a local config:

```powershell
Copy-Item .\config\model_council.example.toml .\config\model_council.local.toml
```

Set absolute `repo`, `state_dir`, `research_dir`, and roadmap paths. Then verify:

```powershell
& $CommanderExe collaborating --config .\config\model_council.local.toml workers --check
& $CommanderExe collaborating --config .\config\model_council.local.toml preflight --repo C:\path\to\target-repo
```

Run:

```powershell
$RunId = "target-repo-council-v51-v57-" + (Get-Date -Format "yyyyMMdd-HHmmss")

& $CommanderExe collaborating `
  --config .\config\model_council.local.toml `
  campaign `
  --from-version 51 `
  --to-version 57 `
  --strategy full `
  --run-id $RunId
```

## Selecting a roadmap file

By default the roadmap is the config value (`roadmap_path` for council, `plan`
for checkpoint), and discovery also picks up any other `docs/*roadmap*.md` files
as context. To choose a specific roadmap for a single run without editing config,
pass `--roadmap`. It goes immediately after `--config`, before the subcommand:

```powershell
# Council: drive this run from a specific roadmap file
& $CommanderExe collaborating `
  --config .\config\model_council.local.toml `
  --roadmap .\docs\target-roadmap.md `
  preflight

& $CommanderExe collaborating `
  --config .\config\model_council.local.toml `
  --roadmap .\docs\target-roadmap.md `
  campaign --from-version 51 --to-version 57 --run-id $RunId
```

```powershell
# Checkpoint: same flag, same position
& $CommanderExe competing `
  --config .\config\checkpoint_competition.local.toml `
  --roadmap .\docs\target-roadmap.md `
  run --from-version 51 --to-version 57
```

The path may be relative to the target repo or absolute. The selected file is
used as the primary roadmap even if it lives outside `docs/` or is not named
`*roadmap*.md`; any other `docs/*roadmap*.md` files still follow as secondary
context. Run `preflight` first to confirm `primary_roadmap` points where you
expect.

## Failover strategy (within collaborating)

Note: this is a failover *strategy* within the `collaborating` mode, not the
`alternating` command. The failover strategy preserves the collaborating role
plan but immediately tries the other worker when the assigned worker is blocked
by a configured availability class.

```toml
[council]
worker_failover = true
failover_on = ["quota", "entitlement", "authentication"]
```

Run:

```powershell
& $CommanderExe collaborating `
  --config .\config\model_council.local.toml `
  campaign `
  --from-version 51 `
  --to-version 57 `
  --strategy alternating `
  --run-id $RunId
```

Stage state records:

Commander also writes provider-neutral learning and scope-routing ledgers under each run's `shared-context/` directory. The learning ledger captures prompt/output artifacts and hashes, observed Codex CLI / Claude Code token and cost fields, status and roadmap-version alignment signals, and aggregate efficiency summaries. The scope router writes `scope-assessment.json` and `routing-decision.json` per stage, then uses deterministic request scope plus prior ledger outcomes to select the configured worker/model and low/medium/high effort for future stages. See `docs/USAGE_LEARNING.md`.


```json
{
  "requested_agent": "gpt",
  "agent": "claude",
  "failovers": [
    {"from": "gpt", "to": "claude", "reason": "quota"}
  ]
}
```

If both workers are unavailable, Commander preserves state and waits according to `quota_retry_seconds` and `quota_max_retry_seconds`. `--no-wait` converts that condition into a resumable pause.

Alternating mode cannot guarantee zero downtime when both workers are unavailable, a human approval is pending, or the next task cannot safely be substituted.

## Rapid alternate mode

Rapid alternate implements one version with one worker, then stops at the version
boundary and hands the work to the other worker to continue. It is a deliberate
stop-and-handoff, not the auto-failover strategy above: nothing is merged,
pushed, or committed, and the worktree is left for review.

```powershell
# codex implements v51, then the run stops and hands off to claude for v52
& $CommanderExe alternating --config $CouncilConfig `
  --version 51 --worker codex --to-version 57
```

With exactly two configured agents the next worker is inferred (the ping-pong);
with more agents pass `--handoff-to`. Each turn writes `HANDOFF.md` and
`handoff.json` containing the exact next command, a ready-to-paste continuation
prompt for the next worker, and a `HANDOFF:` line. The next turn is simply:

```powershell
& $CommanderExe alternating --config $CouncilConfig `
  --version 52 --worker claude --handoff-to codex --to-version 57
```

At the end of the range the handoff reports completion instead of a next worker.
The dangerous-intent approval gate still applies before a risky version runs.

## Competing mode (convergence)

Create a local config:

```powershell
Copy-Item .\config\checkpoint_competition.example.toml .\config\checkpoint_competition.local.toml
```

Both workers execute every role using role-specific model and effort values. Each candidate runs in a separate worktree. Both workers judge both candidates. The winner seeds a third convergence worktree, then Codex and Claude improve it sequentially.

```powershell
& $CommanderExe competing `
  --config .\config\checkpoint_competition.local.toml `
  --repo C:\path\to\target-repo `
  run `
  --from-version 51 `
  --to-version 57
```

## Multiple providers and models

Workers are not limited to Codex CLI and Claude Code. Any CLI-driven model can
fill a council role or be a checkpoint competitor — a different provider, or the
same provider with a different model and effort.

Each `[agents.<name>]` table needs a `runtime`, a `provider`, and a `command`.
For the two built-in runtimes the command must still launch the matching
executable (`codex-cli` → `codex`, `claude-code` → `claude`), so a typo cannot
silently run the wrong tool. Any other `runtime` id is treated as a custom
runtime: no executable check is enforced, output is parsed by `output_format`
(use `text` unless the CLI emits `codex-jsonl`/`claude-json`), and the command
template can interpolate `{model}`, `{prompt}`, `{context_dir}`, and
`{output_file}`.

Council maps roles to agents, so the same provider can appear twice with
different models:

```toml
[roles]
researcher         = "codex_fast"   # gpt-5-mini, effort=low
security_assurance = "codex_deep"   # gpt-5.5, effort=high

[agents.codex_fast]
runtime = "codex-cli"
provider = "openai"
model = "gpt-5-mini"
effort = "low"
command = ["codex", "{model_args}", "{effort_args}", "exec", "-"]

[agents.codex_deep]
runtime = "codex-cli"
provider = "openai"
model = "gpt-5.5"
effort = "high"
command = ["codex", "{model_args}", "{effort_args}", "exec", "-"]
```

Checkpoint takes exactly two `[agents.*]` tables; whatever they are named becomes
the two competitors, so a cross-provider tournament is just two agents with
different runtimes. A full worked council example is in
`config/model_council.multi-provider.example.toml`.

Loop-engineering note: the generator/evaluator split is the floor of a reliable
loop, and a model reviewing its own output keeps its own blind spots. Put the
evaluator role (`security_assurance` in council; the judge/cross-reviewer in
checkpoint) on a *different* model from the generator. Multi-provider support is
what makes that separation a config change rather than a code change. Run
`workers` to confirm each role resolves to the model you intend before launching
a campaign.

## Stop conditions (`/goal`)

A loop's floor is its evaluator, so completion should be decided by a fresh model
— one that did not produce the work — not by the generator grading its own
homework. The `goal` command implements this maker-checker check.

```powershell
# Council: a fresh model judges whether the condition holds over the configured checks
& $CommanderExe collaborating `
  --config .\config\model_council.local.toml `
  goal --condition "all tests in tests/ pass and the lint step is clean"

# Pick the judging model explicitly (should differ from the generator)
& $CommanderExe collaborating --config $CouncilConfig goal --condition "..." --judge claude_deep

# See what would be evaluated without calling the model
& $CommanderExe collaborating --config $CouncilConfig goal --condition "..." --dry-run
```

```powershell
# Checkpoint: the judge is one of the two competitors (default: the first)
& $CommanderExe competing --config $CheckpointConfig goal --condition "..." --judge gemini
```

Evaluation has two layers. The configured `checks` run first and form a hard
floor: if any check fails, the condition is not met no matter what the model
says — a model cannot talk a red test into being green. Then a fresh model reads
that evidence and decides whether the natural-language condition holds,
defaulting to not-met. The verdict is JSON: `met`, `model_met`,
`deterministic_all_passed`, `reasons`, `unmet`, and the raw check results.

The default council judge is the `security_assurance` agent (the evaluator, not
the generator); `--judge` overrides it. Pair this with multi-model support so the
judge runs on a different model from the generator.

`campaign --until "<condition>"` runs the same /goal gate automatically after a
campaign, judging the condition against the campaign worktree and recording the
verdict in the run's `result.json` and `stop-condition.json`. It does not halt a
batch mid-flight; rerunning until the condition is met is a scheduling concern.

## Loops: running until met

`council loop` schedules campaigns into a loop with the two guards loop
engineering depends on: it runs until a stop condition is met, and a budget
circuit-breaker bounds the run.

```powershell
# Local loop: run a campaign turn, re-check the goal, sleep, repeat -- until met
# or a cap trips. Machine must stay on.
& $CommanderExe collaborating --config $CouncilConfig loop `
  --condition "all tests pass and the v51-57 field-deployability items are done" `
  --from-version 51 --to-version 57 `
  --max-turns 8 --max-consecutive-failures 3 --max-cost-usd 40 --interval 3600
```

Each turn first asks a fresh model whether the condition already holds in the
repository; if it does, the loop stops without doing more work. Otherwise it runs
one campaign turn, which opens proposals for human review and never auto-merges
-- so the human review point stays installed, and merged work carries into the
next turn's goal check. The circuit-breaker caps (`--max-turns`,
`--max-consecutive-failures`, `--max-cost-usd`) convert an open-ended overnight
run into a bounded one; the cost ceiling is enforced against the offline
shadow-cost estimate.

For machine-off autonomy, run one turn per scheduled tick from CI. `loop-init`
emits a ready-to-commit GitHub Actions workflow:

```bash
hermes-legion-commander collaborating --config config/model_council.toml \
  loop-init --condition "all tests pass and lint is clean" \
  --from-version 51 --to-version 57 --cron "0 6 * * *" \
  --out .github/workflows/hermes-legion-loop.yml
```

The cron schedule is the scheduler; `--single-turn` runs one turn per invocation
and the committed loop state resumes the next run. Local scheduling buys
frequency and local-file access at the cost of keeping the machine on; cloud
scheduling buys true autonomy at the cost of a coarser interval and a fresh
checkout each run. A mature loop often uses both.

## Subagent delegation and prompt-effectiveness metrics

To keep token and API usage low, the generator prompts in both modes authorize
the lead worker to spawn weaker, cheaper subagents for parallelizable grunt work
-- mechanical edits across many files, repetitive test scaffolding, reference
scanning, formatting -- while design, security, and final verification stay on
the lead. Each subagent is directed to the cheapest capable model, so several
cheap subagents running in parallel cost less and finish sooner than one strong
model doing everything. Review and judge roles do not receive the delegation
contract.

The cap defaults to five but is configurable per run. Set `subagent_cap` in the
config (`[council]` for collaborating/alternating, `[competition]` for competing)
to raise or lower it:

```toml
[council]
subagent_cap = 8   # allow up to 8 subagents; default is 5, set 0 to disable delegation
```

The configured number is injected into the worker prompt as a hard limit, and the
same cap drives the over-cap detection in the metrics below, so the
`prompt-effectiveness.json` report reflects whatever value you set.

Workers report what they spawned in a `SUBAGENTS:` block, which is parsed and
recorded on every stage event. Those events aggregate into
`prompt-effectiveness.json` in the run's shared context, with per-role signals
the supervisor optimizes against: pass rate, input/output tokens, an
output-per-input-token efficiency ratio, average cost, subagent utilization and
any cap breaches, and retry/failover counts. Reading this is how Hermes learns
which prompts produce efficient, passing work and which waste tokens or fail, and
tunes how it prompts Codex and Claude.

Authentication is unchanged: Codex and Claude Code work with a subscription/OAuth
session, an explicit OAuth token, or an API key; the worker environment sanitizer
preserves native CLI credential stores so an OAuth session is available inside
the subprocess.

## Shared memory and handoffs

Every completed stage records:

- normalized output;
- worker and runtime;
- provider session ID when available;
- file changes and Git diff summary;
- tests and experiments;
- unresolved risks;
- artifact paths and hashes;
- requested and executed worker;
- failover history.

Provider-private histories are never treated as shared memory.

## Environment sanitization

Each worker may define `unset_env`. Only listed variables are removed from its subprocess environment. API keys are not removed implicitly. The examples remove cross-provider endpoint overrides that commonly leak through parent orchestrators.

## Approvals and resume

Approve a blocked council run:

```powershell
& $CommanderExe collaborating --config $CouncilConfig approve --run-id $RunId --phase dangerous-intent --note "Reviewed isolated scope"
& $CommanderExe collaborating --config $CouncilConfig resume --run-id $RunId
```

Checkpoint uses the same explicit approval model through its own `approve` and `resume` commands.

## Validation outputs

Per-version campaigns can produce:

```text
docs/iterations/<version>-<feature>.md
tests/test_v<version>_*.py
experiments/run_v<version>_*.py
results/iterations/v<version>/campaign-result.json
results/iterations/v<version>/campaign-result.md
```

## Package verification

```bash
python -m pytest -q
python -m compileall -q hermes_legion_commander
python -m build
```

## Validation

- **59 automated tests passed**
- Isolated wheel installation passed
- Supervisor profile setup created all four contract files
- No live provider calls were used for release verification

Wheel SHA-256: `f0fa53655b07e0ef367b0a01137b3281a43780ecea551290cd3a8eb0e4d3c684`


## Generic Hermes worker profiles

The Hermes setup now creates three profiles:

```text
legion-supervisor
legion-worker-a
legion-worker-b
```

`legion-worker-a` and `legion-worker-b` are interchangeable harness workers.
Neither is permanently assigned to Codex, Claude, building, or reviewing. For
every task, the supervisor supplies an explicit dispatch contract naming the
mode, logical role, native runtime, permission, model, effort, workspace,
shared-context snapshot, objective, checks, and required handoff.

This supports:

- council assignments with role-specialized stages;
- competition assignments with isolated candidates and cross-judging;
- alternating assignments where either profile may operate either runtime after
  an explicit handoff or failover decision.

Inspect the generated policy without invoking a model:

```powershell
& $CommanderExe supervisor show-worker-soul
& $CommanderExe supervisor show-worker-skill
& $CommanderExe supervisor show-dispatch-contract
& $CommanderExe supervisor assignment-plan --mode council
& $CommanderExe supervisor assignment-plan --mode competition
& $CommanderExe supervisor assignment-plan --mode alternating
```

See `docs/GENERIC_WORKER_PROFILES.md`.


## Manual-run-derived prompt hardening

The supervisor and worker prompts now include a reusable roadmap execution contract derived from successful manual Codex/Claude target-repository runs:

- work version-by-version;
- finish the active version before quota/context handoff;
- use exact handoff lines with HEAD/version/branch/tree state;
- commit only when the dispatch policy permits it;
- commit only current-version evidence;
- never machine-award host-side physical, HIL, field, audit, publication, tag, or release gates;
- ignore phantom Git diffs when the worktree hash equals the HEAD blob.

See `docs/PROMPT_IMPROVEMENTS_FROM_MANUAL_RUNS.md`.

## Automated setup on Windows and Linux

The fastest supported installation path is now the platform bootstrap script.

Windows:

```powershell
Set-ExecutionPolicy -Scope Process Bypass

.\scripts\bootstrap-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo"
```

Linux or WSL2:

```bash
./scripts/bootstrap-hermes-legion-commander.sh \
  --target-repo "$HOME/code/target-repo"
```

The bootstrap installs missing official prerequisites, installs Commander in a
dedicated environment, creates fresh configs and all three Hermes profiles,
checks authentication, and runs zero-model council and checkpoint preflights.

Afterward, use the built-in diagnostic:

```text
hermes-legion-commander doctor
```

See `docs/AUTOMATED_SETUP.md`.

### Windows PowerShell 5.1 Codex installation

The Windows bootstrap is compatible with older PowerShell 5.1/.NET Framework
hosts that do not expose `RuntimeInformation.OSArchitecture`. It patches only
the official installer’s architecture assignment, runs the installer in a
child PowerShell process, and uses the official `@openai/codex` npm package as
a secondary fallback when npm is available.

## Repository graph navigation

Every run now builds a local Graphify-style repository knowledge graph under `shared-context/repo-map/` and injects a task-specific `repo-context-pack.md` into worker prompts. This gives Codex CLI and Claude Code a compact start-here map of likely files, symbols, calls, imports, entrypoints, docs, tests, schema/config files, communities, hotspots, and graph neighbors before they perform broad repository reads. The graph exports `graph.json`, `graph.html`, `GRAPH_REPORT.md`, `REPO_MAP.md`, `repo-map-index.jsonl`, `cache/`, and `wiki/`, and the CLI can query paths directly with `hermes-legion-commander repo-graph query` or `path`. Scope-aware routing also records `repo_facts` so model and effort selection can account for repository size, language mix, and multimodal assets.

See `docs/REPO_GRAPH.md` for output formats, commands, and limits.



### Prompt token and shadow API cost preflight

Every Codex CLI / Claude Code stage now records a local preflight estimate before the worker is invoked. It works with ChatGPT/Codex OAuth and Claude subscription OAuth because it does not require API calls. Outputs include `prompt-preflight.json`, `prompt-cost-estimate.json`, `shared-context/prompt-preflight-ledger.jsonl`, and `shared-context/prompt-cost-summary.md`.

Manual estimate:

```powershell
hermes-legion-commander token-cost --runtime codex-cli --model gpt-5.3-codex --prompt-file .\prompt.md
hermes-legion-commander token-cost --runtime claude-code --model claude-sonnet-4-6 --prompt-file .\prompt.md
```

- `docs/GITHUB_HEALTH.md` — GitHub Actions and Dependabot acceptance gate.
