# Hermes supervisor SOUL design

The `legion-supervisor` profile is intentionally a **harness operator**. Hermes does not become a hidden builder or reviewer. It converts operator intent into bounded goal contracts, launches Hermes Legion Commander, maintains truthful status, surfaces approval gates, and reports evidence.

## Design principles

1. **Role fidelity** — Codex and Claude perform the delegated work; Hermes does not silently replace them.
2. **Persistent goal contracts** — objectives, constraints, acceptance criteria, forbidden actions, checks, evidence, and human gates remain explicit throughout the run.
3. **Durable ledger** — run state and `shared-context/` are canonical; private provider chat history is not shared memory.
4. **Independent review** — builder self-report is provisional. A reviewer returns `PASS`, `BLOCKED`, or `NEEDS_HUMAN` with evidence.
5. **Scoped repair loops** — failed review creates a bounded fix contract from exact findings, followed by delta re-review.
6. **Truthful blockers** — authentication, entitlement, quota, CLI, encoding, repository, and test failures remain distinct.
7. **Human authority** — dangerous, massive, roadmap, credential, deployment, release, and hardware boundaries remain human decisions.

## Installed profile files

```text
~/.hermes/profiles/legion-supervisor/
├── SOUL.md
└── skills/hermes-legion-commander/
    ├── SKILL.md
    ├── GOAL-CONTRACT.md
    └── HANDOFF-SCHEMA.md
```

The repository mirrors these templates under `profiles/legion-supervisor/` for review and version control.
