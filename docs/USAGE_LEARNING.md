# Usage and accuracy learning ledger

Hermes Legion Commander records durable, provider-neutral learning data for every Codex CLI and Claude Code stage. The goal is to let later stages and later resumed runs use less context while staying aligned with the requested roadmap.

## Files produced under `shared-context/`

- `events/*.json` — one immutable event per completed stage. Each event includes runtime, model, effort, output artifact, output hash, prompt artifact, prompt hash, raw stdout/stderr hashes, Git delta, observed token/cost fields, quality signals, and roadmap snapshot metadata.
- `artifacts/<event>.md` — normalized final worker output.
- `artifacts/prompts/<event>.md` — the exact shared-context prompt sent to the native CLI.
- `learning-ledger.jsonl` — one compact row per stage for machine parsing.
- `learning-summary.json` — aggregate counts by agent and runtime: stage count, observed tokens, observed cost, prompt/output size, statuses, and efficiency ratios.
- `prompt-lessons.md` — short human/model-readable guidance injected into later prompts so workers can cite exact artifacts instead of replaying large prior context.
- `scope-routing-ledger.jsonl` — one compact row per scope-aware model/effort decision.
- `scope-routing-summary.md` — prompt-injected routing summary used by later stages.
- `routing-decisions/*.json` — full per-stage decision audit records.

## Token and cost extraction

The runtime extracts observed token and cost fields from vendor JSON output without assuming a single provider schema. Known aliases include `input_tokens`, `prompt_tokens`, `output_tokens`, `completion_tokens`, `reasoning_tokens`, `total_tokens`, `cost_usd`, and `total_cost_usd`.

These values are best-effort observability fields, not billing-grade accounting. When a CLI emits repeated cumulative usage objects, Commander keeps the largest observed value for each metric to avoid obvious double counting.

## Accuracy and roadmap alignment signals

Commander records deterministic evidence signals rather than claiming ground-truth correctness. Each event receives:

- normalized status detection such as `PASS`, `BLOCKED`, `NEEDS_HUMAN`, `QUOTA_PAUSED`, or `FAILED`;
- requested roadmap versions mentioned in the prompt;
- versions reported by the worker output;
- version overlap between prompt and output;
- whether checks/tests are mentioned;
- whether risks/blockers are mentioned;
- whether Git changes are visible after the stage;
- `quality_signal_score`, a bounded heuristic used only for routing and prompt-efficiency hints.

Reviewer verdicts, validation commands, and human approval gates remain authoritative.

## Roadmap source of truth

The campaign brief snapshots roadmap candidates in this order when present:

1. `request/roadmap.md`
2. `docs/roadmap.md`
3. `roadmap.md`
4. `docs/**/*roadmap*.md`

Workers are instructed to align claims and changes to the roadmap snapshot, especially `request/roadmap.md` when the target repository contains it.


## Scope-aware model and effort selection

Before a worker command is rendered, Commander now builds a deterministic `scope-assessment.json` from observable request facts: task type, risk flags, prompt size, requested versions, version-range span, and roadmap availability. The base effort mapping is deliberately conservative: low-risk documentation/planning can use `low`, normal implementation/testing defaults to `medium`, and security, safety-critical, destructive, release, or large multi-version work is raised to `high`.

The planner then reads previous `learning-ledger.jsonl` and `scope-routing-ledger.jsonl` files from the state directory. Candidate workers are scored using matched runtime/model/effort rows, status, quality signal, and observed token count. Council mode may select another configured worker/model when historical evidence is stronger. Checkpoint competition keeps the requested competitor lane fixed but can still adjust effort.

The router never invents model names. It can choose only the Codex/Claude models already present in the active configuration or role matrix. Every decision is auditable in the stage's `routing-decision.json`, including candidate scores and how many prior rows were considered.
