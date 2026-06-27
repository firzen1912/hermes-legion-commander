# Checkpoint competition

## Objective

Checkpoint competition creates two independent implementations from the same Git baseline and roadmap range, then converges the strongest verified result without automatically integrating it into the target branch.

## Candidate pipeline

Codex and Claude each execute all candidate roles in separate worktrees:

1. Roadmap plan review
2. Research
3. Literature review
4. Prototype and implementation
5. Code and architecture polish
6. Security assurance
7. Validation artifacts

The two workers use the same explicit shared-memory contract but do not share hidden provider state or candidate worktrees.

## Model and effort routing

Every `(role, competitor)` pair has a `RoleProfile` containing:

- native CLI model identifier;
- effort level;
- role instructions;
- optional command override.

Base commands can include `{model_args}` and `{effort_args}`. The runtime expands these only when configured, allowing different models and reasoning effort for planning, research, implementation, security, judging, and convergence.

## Cross-validation and polish

After the first deterministic check pass, Codex reviews Claude's candidate and Claude reviews Codex's candidate. Findings are saved as JSON with severity, category, confidence, file-level evidence, recommended fix, and supervisor-derived blocking status.

Each candidate owner then runs a `cross_polisher` pass in its own worktree to fix true positives, add tests, and rebut false positives with evidence. Candidate checks and evidence are refreshed before judging. See `docs/CROSS_VALIDATION.md` for the full artifact contract.

## Evidence and judging

After cross-validation polish and deterministic checks, the supervisor publishes candidate evidence:

- binary-capable Git patch;
- changed-file and line statistics;
- Git status;
- check and benchmark results;
- stage outputs and shared-memory artifacts.

Codex and Claude independently judge both polished candidates. Each judgement scores security, correctness, maintainability, and performance, considers cross-validation findings, and may attach defined security blocker codes.

The supervisor averages both judges, applies configured weights, and rejects a candidate when deterministic checks fail or a security blocker exists.

## Convergence

The provisional winner's patch seeds a third worktree. Codex and Claude then run the `converger` role in sequence. They receive both candidate evidence packages and both judge reports through immutable shared context.

The supervisor runs deterministic checks again, publishes converged evidence, runs both workers as read-only `final_verifier` agents, and writes `converged-result.json`. The converged branch remains unmerged and requires human inspection.

## Resume and quota behavior

Every role stage stores durable status. Completed stages are reused. Recognized temporary quota errors either:

- pause and exit when `--no-wait` is supplied; or
- wait with exponential backoff when `--no-wait` is omitted.

Authentication, billing-entitlement, invalid-model, and other non-retryable failures stop immediately.

## Human gates

- `dangerous-intent`: before candidate mutation.
- `massive-diff`: after candidate evidence is measured and before judging/convergence.

Approvals never authorize merge, push, deployment, credential changes, hardware operation, publication, or release.
