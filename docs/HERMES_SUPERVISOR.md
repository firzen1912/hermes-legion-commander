# Hermes supervisor

Hermes Agent is the operator-facing control plane. Hermes Legion Commander is the execution engine. The `legion-supervisor` profile is a harness operator: it does not edit the target repository and does not replace Codex or Claude with Hermes-authored work.

## Installed contract

`setup-hermes-supervisor` installs:

```text
~/.hermes/profiles/legion-supervisor/
├── SOUL.md
└── skills/hermes-legion-commander/
    ├── SKILL.md
    ├── GOAL-CONTRACT.md
    └── HANDOFF-SCHEMA.md
```

The SOUL requires a bounded goal contract, local preflight, deliberate mode selection, durable run IDs, explicit approvals, independent review, scoped fix loops, and evidence-backed status using `PASS`, `BLOCKED`, `NEEDS_HUMAN`, `RUNNING`, and `QUOTA_PAUSED`.

Canonical cross-worker memory remains under each run's `shared-context/` directory. Private provider histories are never treated as shared memory.

Preview the installed content without invoking a model:

```bash
hermes-legion-commander supervisor show-soul
hermes-legion-commander supervisor show-skill
hermes-legion-commander supervisor show-goal-contract
hermes-legion-commander supervisor show-handoff-schema
```
