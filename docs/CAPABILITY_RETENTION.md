# Capability retention in the Codex + Claude runtime

Hermes Legion Commander v1.7.0 uses Codex CLI and Claude Code directly. The orchestration and safety surface remains owned by Hermes Legion Commander.

| Capability | v1.7.0 behavior |
|---|---|
| Roadmap discovery | Scans `docs/**/*roadmap*.md`, selects a primary roadmap, and extracts bounded version sections. |
| Roadmap-plan review | Codex reviews each bounded phase before research. |
| Current research | Codex performs roadmap-scoped evidence discovery and final reconciliation. |
| PDF literature library | Claude creates rigorous reviews; Codex audits claims and metadata; Claude performs final technical/security proofread. |
| Prototyping | Codex runs with workspace-write access in the isolated worktree. |
| Code polish | Claude Code edits the same isolated worktree. |
| Security assurance | Claude receives canonical context, current diff, prior outputs, and bounded security prompts. |
| Shared memory | Every stage receives the same immutable provider-neutral context snapshot. |
| Durable resume | Completed stages are reused; interrupted or quota-paused stages resume by run ID. |
| Quota handling | Temporary quota/rate-limit failures back off; billing/authentication/entitlement failures stop immediately. |
| Human approval | Dangerous intent, massive diffs, and roadmap application remain separately gated. |
| Worktree isolation | Mutable work occurs in dedicated Git worktrees and branches. |
| Tests and experiments | Versioned tests and host-safe experiments are generated and executed when required. |
| Result gathering | Exact commands, output, exit status, durations, and artifacts are retained. |
| Iteration records | `docs/iterations/<version>-<feature>.md` remains supported. |
| Checkpoint competition | Codex and Claude retain independent candidates, cross-review, benchmarks, and evaluation. |
| Dry run and preflight | Local preflight and dry prompt generation make zero model/API calls. |
| Release boundary | No automatic merge, push, deployment, tag, publication, release, credential change, or hardware operation. |

## Checkpoint competition retention

Checkpoint mode does not specialize one provider into a single role. Codex and Claude each perform planning, research, literature review, implementation, code polish, security assurance, validation-artifact creation, comparative judging, and convergence. Role-specific model and effort profiles preserve efficient routing while keeping two independent implementations available for comparison.

The supervisor retains deterministic checks, security blocker vetoes, weighted scoring, candidate patches, dual judge reports, a converged worktree, quota-aware resume, and human approval boundaries.
