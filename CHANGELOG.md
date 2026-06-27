# Changelog

## 1.7.2 — Anchored truth prompt preflight

Every Codex CLI and Claude Code stage now receives an anchored truth preflight
before the current roadmap task. Commander refreshes a prompt pack containing
the current Git state, hashed anchor-source excerpts, hard-boundary lines from
roadmaps and agent rules, repo graph context, and non-blocking GitHub/Dependabot
health when `gh` is available.

Artifacts are written under each worker context at `ANCHORED_TRUTH.md` and
`anchored-truth/`, including `anchored-truth.json`, `current-repo-state.json`,
`anchor-sources.jsonl`, and `prompt-anchor-pack.md`. The pack is injected ahead
of the stage task in all main workflows because it is wired into the shared
worker prompt builder.

## 1.7.0 — Mode commands renamed, configurable subagent cap

Renamed the three mode commands to match the modes, and made the subagent cap
configurable.

Command rename:
- The three modes are now their own top-level commands: `collaborating`
  (collaborative council), `competing` (competitive convergence), and
  `alternating` (rapid alternate). `alternating` is a first-class command rather
  than a subcommand.
- The former `council` and `checkpoint` names still work as deprecated aliases
  for `collaborating` and `competing` and print a deprecation warning; prefer the
  new names. TOML config sections are unchanged (`[council]` for
  collaborating/alternating, `[competition]` for competing).

Configurable subagent cap:
- The subagent grunt-work delegation limit, previously fixed at five, is now set
  by `subagent_cap` in the config (default 5; `0` disables delegation). It is
  honored in both modes. The configured value is injected into the worker prompt
  as a hard limit, and the same cap drives the over-cap detection in the metrics,
  so `prompt-effectiveness.json` reflects whatever value is set.

README fully updated: every CLI invocation, the architecture diagram, and the
section headers now use the new command names; the failover strategy is
disambiguated from the `alternating` command; and the configurable `subagent_cap`
is documented with an example. Full validation: 161 passing tests.

## 1.6.0 — Three named modes and the rapid-alternate handoff

Hermes Legion Commander now has three clearly named execution modes, and a new
third mode that stops at a version boundary and hands the work to the other
worker instead of auto-continuing.

The three modes:
- Collaborative council (`council ... campaign`): multiple roles collaborate per
  version; the campaign AUTO-CONTINUES across the whole version range in one run.
- Competitive convergence (`checkpoint ... run`): two independent candidates per
  version are judged and converged; also auto-continues across the range.
- Rapid alternate (`council ... alternate`): NEW. A single chosen worker
  implements ONE version, the run STOPS at the version boundary, and a structured
  handoff prompts the other worker (codex<->claude) to continue the next version.

Rapid alternate:
- `council alternate --version N --worker <agent> [--handoff-to <agent>]` runs one
  worker on one version, validates it with the configured checks, and stops. When
  exactly two agents are configured the next worker is inferred automatically (the
  ping-pong); with more agents `--handoff-to` is required.
- The run emits `HANDOFF.md` and `handoff.json`: a stop-point document with the
  exact next command, a ready-to-paste continuation prompt for the next worker,
  and a `HANDOFF:` line. At the end of the range it reports completion instead of
  a next worker.
- Unlike `--strategy alternating` (which auto-fails-over a blocked stage mid-run),
  rapid alternate is a deliberate stop-and-handoff: nothing is merged, pushed, or
  committed; the worktree is left for human review, and a non-blocking review
  request is written. The dangerous-intent approval gate still applies.
- Module and CLI help now name all three modes and state which auto-continue and
  which stops.

The handoff document builder and worker-alternation logic are pure and
unit-tested, including the ping-pong inference, the explicit-handoff override, the
ambiguous-config guard, and the final-version completion path. Full validation:
156 passing tests.

## 1.5.0 — Subagent grunt-work delegation, prompt-effectiveness metrics, OAuth verified

Make workers cheaper and faster by delegating grunt work, and measure prompting
so Hermes can optimize it.

Subagent delegation (cap 5):
- The generator prompts in both modes (council per-version implementation;
  checkpoint candidate roles) now authorize spawning up to five weaker, cheaper
  subagents to parallelize low-judgement grunt work -- mechanical edits across
  many files, repetitive test scaffolding, reference scanning, formatting --
  while keeping design, security, and final verification on the lead worker.
  Each subagent is told to use the cheapest capable model, so several cheap
  subagents in parallel cost less and finish faster than one strong model doing
  it all. The cap is a hard limit stated in the contract; review roles do not
  receive it.
- Workers report usage in a `SUBAGENTS:` block. `prompt_metrics.extract_subagent_report`
  parses the count and tasks, clamps nothing but flags any breach of the cap, and
  the count is recorded on every stage event.

Prompt-effectiveness metrics:
- A new `prompt_metrics.prompt_effectiveness` aggregates recorded stage events
  into per-role measurable signals: pass rate, input/output tokens, an
  output-per-input-token efficiency ratio, cost, subagent utilization and cap
  breaches, and retry/failover counts. It is written to `prompt-effectiveness.json`
  in the run's shared context after every stage, and the worker guidance now
  points at it, so the supervisor can see which prompts produce efficient,
  passing work and which waste tokens or fail, and adjust how it prompts Codex
  and Claude.

OAuth:
- Verified and locked with tests: Claude Code and Codex both authenticate via
  subscription/OAuth session, an explicit OAuth token, or an API key, and the
  worker environment sanitizer preserves native CLI credential stores so OAuth
  sessions survive into the subprocess. Auto-continue across a version range in
  council and competition mode is unchanged.

Full validation: 147 passing tests, including subagent parsing edge cases, the
per-role effectiveness aggregation, the cap-breach signal, contract injection in
both modes, and OAuth/API-key auth-mode detection.

Next: three distinctly-named modes (collaborative council, competitive
convergence, rapid alternate) and the rapid-alternate stop-at-version handoff
that prompts a chosen worker to continue.

## 1.4.0 — The loop: scheduling, run-until-met, and a budget circuit-breaker

Campaigns can now run themselves. This is loop engineering's defining move --
scheduling on the harness -- with the two guards the practice depends on.

Scheduling (`loop` and `loop-init`):
- `council loop` runs campaigns on a loop. Local mode loops in-process, sleeping
  `--interval` seconds between turns (frequency and local-file access, machine
  on). `--single-turn` runs exactly one turn and exits, so a cron/CI schedule can
  drive it with the machine off; loop state persists and resumes across runs.
- `council loop-init` emits a ready-to-commit GitHub Actions workflow that runs
  one turn per scheduled tick, commits loop state back, and opens proposals
  without auto-merging. Local buys frequency; cloud buys autonomy; the two are
  different capabilities and a mature loop uses both.

Run-until-met:
- Each turn first asks a fresh model whether the stop condition already holds in
  the repository (the /goal check from 1.3.0); if it does, the loop stops before
  doing redundant work. "Run until met" replaces a fixed version range. Between
  turns the campaign opens proposals for human review and never auto-merges, so
  the human review point -- the open door -- stays installed.

Budget circuit-breaker:
- Hard caps bound an otherwise open-ended overnight run: `--max-turns`,
  `--max-consecutive-failures` (a bug spinning in place stops the loop), and
  `--max-cost-usd`, a cumulative ceiling enforced against the offline shadow-cost
  estimate so an idle bug cannot burn an entire quota. The caps are circuit
  breakers, not budgets: they convert an open-ended risk into a bounded one.

The control logic lives in `loop_driver.py` and is fully dependency-injected --
the goal check, the turn of work, the per-turn cost read, persistence, and
sleeping are all passed in -- so every loop decision is unit-tested without a
live model. Full validation: 133 passing tests, including each circuit-breaker
cap, run-until-met, failed-turn handling, and cross-invocation resume.

## 1.3.0 — Adversarial evaluator and the /goal stop condition

Two paired loop-engineering primitives. Generation is cheap; judgment is the
scarce resource, so this release hardens the part of a loop that can say "no".

Adversarial evaluator stance:
- Both modes now carry an explicit evaluator stance in the assurance/judge
  prompts: default to doubt, assume the work is broken until the evidence proves
  otherwise, do not praise, verify by acting rather than reading, judge behavior
  against the roadmap obligation rather than stated intent, and treat a review
  that never finds a blocker as no review at all.
- Injected into council's per-version and bootstrap security-assurance prompts
  and checkpoint's cross-review, comparative-judge, and final-verification
  prompts. Combined with 1.2.0's multi-model support, the evaluator can run on a
  different model from the generator, which is where its value comes from.

The /goal stop condition (`stop_condition.py`):
- A new `goal` subcommand on both `council` and `checkpoint` evaluates a
  natural-language stop condition with a *fresh* model — one that did not produce
  the work. Completion is decided by the checker, not the maker.
- Two layers: the configured deterministic checks run first and form a hard
  floor (a failed check vetoes a "met" verdict; a model cannot talk a red test
  green), then the fresh model judges whether the condition holds given that
  evidence, defaulting to not-met.
- Council: `--judge` selects the judging agent (default: the `security_assurance`
  agent, never the generator). Checkpoint: `--judge` selects which competitor
  judges, using its `judge` role profile. `--dry-run` runs the checks and builds
  the prompt without invoking the model.
- Council `campaign --until "<condition>"` adds a post-campaign /goal gate: after
  the campaign completes, a fresh model judges the condition against the
  campaign worktree and the verdict is recorded in `result.json` and
  `stop-condition.json`. This does not stop a batch mid-flight; "run until met"
  across reruns is the scheduler's job (next release).

Full validation: 117 passing tests, including the deterministic floor (a failed
check vetoes a model "met"), the fresh-model default judge, and the stance in
both modes' evaluator prompts.

## 1.2.0 — Any provider, any model, per role

Workers are no longer restricted to Codex CLI and Claude Code. Both council and
checkpoint now accept arbitrary agents, so a role (council) or a competitor
(checkpoint) can run on any CLI-driven model — a different provider, or the same
provider with a different model and effort.

- Council: `[agents.*]` may define any number of agents with any names. Each
  needs a `runtime`, a `provider`, and a `command`; roles in `[roles]` map to any
  configured agent. Two or more agents can share a runtime/provider but use
  different models (e.g. a fast model for research, a deep model for assurance).
- Checkpoint: the two `[agents.*]` tables are the two competitors, whatever they
  are named. `cfg.competitors` derives from them, and the comparative-judge JSON
  schema, `opponent_of`, and cross-validation grouping are all driven by the
  configured pair rather than hardcoded `gpt`/`claude`.
- The runtime engine was already provider-agnostic: command templating, output
  parsing (driven by `output_format` with a plain-text fallback), quota/error
  detection, token reconciliation, and failover all work for any CLI. Only config
  validation changed.
- Safety preserved: for the two built-in runtimes (`codex-cli` → `codex`,
  `claude-code` → `claude`) the command must still launch the matching
  executable, so a typo cannot silently run the wrong tool. Custom runtimes
  accept any command — the operator owns the invocation. Every role/competitor
  must name a configured agent or loading fails with a clear message.
- This enables the generator/evaluator separation that loop engineering depends
  on: put the evaluator on a *different* model from the generator, since a model
  reviewing its own output keeps its own blind spots.
- Backwards compatible: existing two-agent `gpt`+`claude` configs load and behave
  exactly as before. Full validation: 103 passing tests.

See `config/model_council.multi-provider.example.toml` for a worked three-model
example.

## 1.1.0 — Explicit roadmap selection

- Added a `--roadmap <file.md>` flag to both `council` and `checkpoint`. It
  overrides the config roadmap (council `roadmap_path`, checkpoint `plan`) for a
  single run, so you can choose which roadmap drives a campaign from the command
  line instead of editing config.
- An explicitly selected roadmap is now authoritative: it is used as the primary
  even when it lives outside `docs/` or is not named `*roadmap*.md`. Previously a
  configured roadmap path outside `docs/` was silently ignored by discovery; that
  latent limitation is fixed for both the flag and the config field. Any other
  `docs/*roadmap*.md` files still follow as secondary context.
- `preflight` no longer hard-requires a `docs/` directory when an explicit
  roadmap is supplied, and reports a clearer message when no roadmap is found.
- Out-of-repo roadmap paths now display gracefully instead of raising.
- Added council and checkpoint regression tests for explicit, out-of-docs, and
  absolute roadmap selection. Full validation: 99 passing tests.

## 1.0.0 — General availability

First stable release. Promotes the `0.8.6` line (scope-aware routing, repo
knowledge graph, cross-validation, and offline token/shadow-cost preflight) to
GA after a security and capability review. No breaking changes to the CLI,
config schema, or public module surface.

Security and capability review:
- Confirmed all worker invocation goes through list-form `subprocess` calls with
  per-call timeouts; no `shell=True`, `os.system`, `eval`, `exec`, or `pickle`
  anywhere in the package.
- Confirmed the command trust boundary: worker argv is templated from operator
  config (`agent.command`, model, effort), never from worker model output.
- Confirmed shared-context isolation is enforced, not just documented: each stage
  receives a sealed read-only `worker-context/` snapshot, and council/checkpoint
  runs hash the snapshot before and after execution and hard-fail the stage if a
  worker mutates supervisor-owned context.
- Confirmed no automatic merge, push, deploy, tag, publish, release, or commit
  path exists; the only git mutations are isolated `worktree add/remove` and
  intent-to-add for diffing.

Fixes:
- `SupervisorConfig.repo_root`/`state_dir` were evaluated once at import time via
  bare `Path.cwd()`/`Path.home()` defaults, so the working-directory default
  could be wrong when the config was instantiated after a directory change. They
  now use `default_factory` and resolve per-instantiation.
- Made the campaign version/iteration-document `zip` strict so a count-invariant
  mismatch fails loudly instead of silently skipping verification for trailing
  versions.
- Removed a duplicate `worker_cli_invoked` key in the local-only audit payload,
  removed dead `signal`/`sys` imports, renamed shadowing comprehension variables,
  and added explicit grouping to the required-validation gate. No behavior change.
- Full validation: 95 passing tests, `compileall` clean, wheel import verified.

## v0.8.6-5crossvalidate

- Added mandatory competitive cross-validation in checkpoint mode: Codex reviews Claude's candidate and Claude reviews Codex's candidate before judging.
- Added owner-side `cross_polisher` passes that fix true-positive opponent findings and refresh deterministic checks/evidence.
- Added read-only `final_verifier` passes for the converged implementation with mutation detection.
- Added cross-validation/final-verification artifacts, CLI subcommands, docs, config role profiles, and regression tests.

## v0.8.6-4graphifykg

- Upgraded the repository graph into a Graphify-style local knowledge graph.
- Added `repo-map/graph.html`, `repo-map/GRAPH_REPORT.md`, `repo-map/cache/`, and `repo-map/wiki/` outputs.
- Added graph schema v2 with normalized nodes, confidence/provenance-tagged edges, communities, surprising cross-connections, and knowledge gaps.
- Added richer static extraction for Python calls/signatures, JavaScript/TypeScript relative imports, Go/Rust/Java/Kotlin/C# symbols, Markdown links/path refs, JSON/YAML keys, SQL objects, shell/PowerShell functions, Dockerfile facts, and multimodal asset metadata.
- Added `hermes-legion-commander repo-graph build/query/path` for graph-first worker navigation.
- Added query/path helper tests and raised full validation to 89 passing tests.

## v0.8.6-3repograph

- Added local repository graph generation for worker shared context.
- Added `repo-map/graph.json`, `repo-map/REPO_MAP.md`, `repo-map/repo-map-index.jsonl`, and task-specific `repo-context-pack.md` / `.json` outputs.
- Added Python symbol/import extraction, Markdown heading extraction, TOML entrypoint extraction, hotspot scoring, and task-term file selection.
- Scope-aware routing now includes repository facts so model/effort selection accounts for repo size and language mix.
- Added tests for graph generation, import edges, entrypoint detection, context-pack prompt injection, and repo-fact scope assessment.

## 0.8.6 — Scope-aware routing patch

- Added deterministic task/request scope assessment before each Codex CLI or Claude Code invocation.
- Added auditable model/effort routing decisions that use configured workers/models plus prior learning ledgers.
- Added per-stage `scope-assessment.json` and `routing-decision.json` artifacts.
- Added shared `scope-routing-ledger.jsonl`, `scope-routing-summary.md`, and canonical routing decisions for later runs.
- Council mode can select a better-evidenced configured worker/runtime; checkpoint competition preserves isolated competitor lanes while adjusting effort.
- Added regression tests for security/multi-version scope escalation, low-risk effort reduction, and learned runtime switching.

## 0.8.6

- Added manual-run-derived roadmap execution prompt contracts.
- Added quota-aware clean-boundary handoff language to supervisor and worker prompts.
- Added host-side evidence honesty and generated-artifact discipline to dispatch contracts.
- Added phantom-diff and current-version evidence policies.
- Added prompt-contract regression tests and documentation.


## 0.8.5

- Fixed Codex installation on Windows PowerShell 5.1 hosts whose .NET Framework lacks `RuntimeInformation.OSArchitecture`.
- Runs the official Codex installer in a child PowerShell process and patches only the missing architecture assignment.
- Added the official `@openai/codex` npm package as a secondary installation fallback.
- Added regression coverage for x64/ARM64 detection and child-process isolation.


## 0.8.5 — Generic Hermes worker profiles

- Added `legion-worker-a` and `legion-worker-b` as interchangeable Hermes harness profiles.
- Removed permanent builder/reviewer and Codex/Claude identity from worker SOUL files.
- Added explicit per-task dispatch contracts for mode, role, native runtime, permission, model, effort, workspace, shared context, checks, and handoff requirements.
- Added assignment-plan previews for council, competition, and alternating modes.
- Added `show-worker-soul`, `show-worker-skill`, `show-dispatch-contract`, `print-dispatch`, and `dispatch` supervisor commands.
- Supervisor setup now creates or repairs the supervisor plus both generic worker profiles by default.
- Added generic worker source templates and setup/reset/repair support.
- Preserved direct council, checkpoint competition, alternating failover, shared memory, approvals, tests, experiments, and UTF-8 transport.

## 0.8.1 — Harness-operator SOUL contracts

- Expanded the Hermes supervisor `SOUL.md` into a strict harness-operator contract.
- Added persistent goal-contract, normalized handoff, review/fix-loop, blocker, and final-report rules.
- Added profile templates under `profiles/legion-supervisor/`.
- Added `show-soul`, `show-skill`, `show-goal-contract`, and `show-handoff-schema` supervisor commands.
- Setup installs `GOAL-CONTRACT.md` and `HANDOFF-SCHEMA.md` alongside `SOUL.md` and `SKILL.md`.
- Added SOUL design and handoff documentation plus regression tests.

## 0.8.0 — Hermes supervisor, alternating failover, and rename restoration

- Renamed the primary project/package/CLI back to `hermes-legion-commander` / `hermes_legion_commander`.
- Retained `legion-commander` as a deprecated CLI alias.
- Added a Hermes Agent supervisor profile and skill installer.
- Added `supervisor run` and `supervisor print-command` for council, competition, and alternating modes.
- Added immediate compatible-worker failover on quota, entitlement, and authentication availability failures.
- Persisted requested/executed worker and complete failover history per stage.
- Added per-worker environment sanitization without implicitly deleting API keys.
- Retained canonical provider-neutral shared memory, immutable snapshots, approvals, worktrees, tests, experiments, iterations, and convergence.
- Added clean reset and repair scripts for PowerShell and Bash.
- Archived migration from the previous `LegionCommander` installation/state root.
- Retained UTF-8 subprocess transport on Windows.

## v0.7.0 - tokenpreflight

- Added offline prompt token preflight before Codex CLI and Claude Code execution.
- Added subscription/OAuth auth-mode inference for ChatGPT/Codex and Claude Code.
- Added API-equivalent USD cost estimates for OpenAI/Codex and Anthropic/Claude models while preserving native CLI subscription usage.
- Added `token-cost` CLI command for manual estimates.
- Added prompt-cost ledgers, summaries, event reconciliation, and learning summary integration.
- Ensured repo-graph facts surface across supervisor, council, checkpoint, cross-validation, and verification modes.

