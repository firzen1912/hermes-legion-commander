# Routing Context (Claude + Codex)

Hermes Legion Commander prepares a multi-model, multi-agent routing plan before
every worker prompt. This is an auditable deterministic router
seeded by anchored repo truth, workflow governance, runtime availability, and
previous learning ledgers.

The model pool is two locally authenticated CLI runtimes:

- Claude Code (`claude`, runtime `claude-code`, provider `anthropic-claude-code`)
- OpenAI Codex CLI (`codex`, runtime `codex-cli`, provider `openai-codex`)

There is no remote model provider, API key provider registry, or HTTP endpoint.
Each CLI authenticates through its own login, and the router detects
availability from PATH the same way `doctor` does.

## Runtime Setup

Install both CLIs and authenticate each with OAuth or its native subscription
session, per the CLI's own documentation. Then verify:

```bash
hermes-legion-commander doctor
codex login status
claude auth status
```

Print runtime setup notes any time:

```bash
hermes-legion-commander routing config-example
```

## Router Planning

```bash
hermes-legion-commander routing plan \
  --repo /path/to/your/repo \
  --task "Implement the next roadmap item safely" \
  --base-ref origin/dev
```

Add `--check-auth` when you want the router to spawn `codex login status` and
`claude auth status` and include authenticated/not-authenticated state.

Outputs:

```text
shared-context/ROUTING_CONTEXT.md
shared-context/routing-context/routing-context-report.json
shared-context/routing-context/model-roster.json
shared-context/routing-context/worker-roster.json
shared-context/routing-context/runtime-health.json
shared-context/routing-context/routing-policy.json
shared-context/routing-context/routing-context-pack.md
```

## Prompt Integration

Every normal worker prompt now includes:

```text
ANCHORED_TRUTH.md
GOVERNANCE.md
ROUTING_CONTEXT.md
CONTEXT.md
...
CURRENT STAGE TASK
```

The router recommends mode escalation, role assignment (Thinker/Worker/Verifier,
plus a Judge in competing mode), and runtime fallbacks between Claude and Codex.

Escalation to `competing` with cross-validation and final verification is
recommended for high-risk changes: security, release, CI/workflow, dependency
manifests (`pyproject.toml`, `requirements*.txt`, `package.json`, lockfiles,
`Cargo.toml`, `go.mod`, and similar), and evidence files. A router refresh
failure never blocks worker execution; it degrades to
`shared-context/routing-context-error.json`.

## Routing-Policy Summary

```bash
hermes-legion-commander routing train --context-dir /path/to/your/repo/shared-context
```

This writes `shared-context/routing-context/routing-policy-summary.json`: a summary
of existing learning ledgers placed next to the deterministic routing rules. It
is an observability aid, not a learned policy. The routing rules are fixed
constants that mirror the planner, and the telemetry block does not alter them.
The `train` subcommand name and the `train_policy()` alias are retained for
backwards compatibility only.
