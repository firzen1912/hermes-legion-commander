# Generic Hermes worker profiles

Hermes Legion Commander installs three Hermes profiles:

```text
legion-supervisor
legion-worker-a
legion-worker-b
```

The supervisor is the control plane. The two worker profiles are interchangeable
harnesses. Neither worker is permanently a builder, reviewer, Codex worker, or
Claude worker.

## Why two generic workers

Two profiles preserve independent Hermes sessions, memory, and Kanban task
ownership while avoiding rigid role identity. The supervisor can assign either
profile to either native CLI.

A profile assignment always includes:

- council, competition, or alternating mode;
- logical role;
- Codex CLI or Claude Code;
- read-only or workspace-write permission;
- worktree and immutable shared context;
- model and effort;
- goal contract, checks, and handoff schema.

## Default plans

### Council

The default plan favors Codex for roadmap/research/prototype work and Claude for
literature/polish/security work. These are defaults, not profile identities.

### Competition

Worker A normally operates the Codex candidate and Worker B the Claude
candidate. During judging and convergence, assignments may cross.

### Alternating

One profile/runtime pair holds the workspace lease at a time. On explicit
redispatch, either generic profile may operate either native runtime.

## Contract rule

The dispatch contract is authoritative. A worker must not infer its role from
its profile name or from a previous task.

Use:

```powershell
hermes-legion-commander supervisor assignment-plan --mode council
hermes-legion-commander supervisor show-worker-soul
hermes-legion-commander supervisor show-dispatch-contract
```

Preview a concrete dispatch without invoking a model:

```powershell
hermes-legion-commander supervisor print-dispatch `
  --worker-profile legion-worker-a `
  --mode council `
  --role security_assurance `
  --runtime claude `
  --permission read-only `
  --workspace C:\path\to\worktree `
  --context-dir C:\path\to\worker-context `
  --prompt-file C:\path\to\prompt.md `
  --output-file C:\path\to\handoff.json `
  --objective "Review the bounded change and report blocking security findings."
```
