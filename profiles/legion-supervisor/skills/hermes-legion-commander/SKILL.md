# Hermes Legion Commander skill

Operate `hermes-legion-commander` from the local terminal. Hermes is the harness operator; it does not perform implementation or review itself.

## Required intake

1. Resolve absolute Commander, config, and target-repository paths.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and selected roadmap section when present.
3. Build a goal contract from `GOAL-CONTRACT.md`.
4. Run worker resolution and local-only roadmap preflight.
5. Choose council, competition, or alternating mode deliberately.

## Required execution

1. Launch the exact Commander command.
2. Preserve and reuse the run ID.
3. When using Hermes Kanban workers, assign either generic profile through an explicit dispatch contract. The contract must name the role, Codex-or-Claude runtime, permission, model, effort, workspace, context, checks, and handoff requirements.
4. Never infer a worker role from `legion-worker-a` or `legion-worker-b`; both profiles are interchangeable.
5. Read status from stage `state.json`, run `result.json`, approval request files, and `shared-context/`.
6. Surface approval requests and stop for the human.
7. On interruption, resume the same run rather than restarting completed work.
8. On review failure, issue a scoped fix contract from exact findings and then re-review the delta.
9. Report `PASS`, `BLOCKED`, `NEEDS_HUMAN`, `RUNNING`, or `QUOTA_PAUSED` with evidence.

## Prohibited

- Do not edit the target repository as Hermes.
- Do not call Codex or Claude directly for normal execution.
- Do not let a reviewer silently become a builder.
- Do not treat builder self-report as completion.
- Do not merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.

Commander handles UTF-8 transport, environment sanitization, shared memory, worktrees, failover, approvals, validation, and convergence.
