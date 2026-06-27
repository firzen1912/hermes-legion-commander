# Workflow governance

Hermes Legion Commander v1.7.4 adds a governance layer that runs before worker prompts and when PR workflow finalization occurs.

## Prompt preflight

`build_prompt_with_shared_context()` now refreshes governance artifacts after anchored truth and before prompt assembly. Worker prompts include `GOVERNANCE.md` next to `ANCHORED_TRUTH.md`, so Codex CLI and Claude Code see the current patch risk, merge readiness, evidence-diff summary, local/CI parity warnings, and regression memory before roadmap implementation.

Artifacts are written under the active context directory:

```text
GOVERNANCE.md
governance/governance-report.json
governance/merge-readiness.json
governance/merge-readiness.md
governance/changed-files.json
dashboard/index.html
```

## Manual commands

```powershell
hermes-legion-commander governance check --repo C:\path\to\repo
hermes-legion-commander governance check --repo C:\path\to\repo --base-ref origin/dev --json
hermes-legion-commander governance comment --repo C:\path\to\repo --pr 12
hermes-legion-commander governance branches list --repo C:\path\to\repo
hermes-legion-commander governance branches cleanup --repo C:\path\to\repo --older-than-days 14
hermes-legion-commander governance memory-add --context-dir C:\path\to\repo\shared-context --title "CRLF evidence" --rule "Normalize before hashing or signing text artifacts."
```

## Risk escalation

The governance layer recommends mode escalation from changed files. Security, safety, release, dependency, workflow, reference-config, and evidence changes escalate to competing or final verification. Documentation-only changes can remain alternating/collaborating.

## PR integration

When `--open-pr` is enabled, Commander adds merge-readiness details to the PR body and attempts to post a structured Legion Commander review comment. Failures to post a comment do not invalidate the finished branch; they are recorded in the PR artifact JSON.

## Ownership routing

Default path ownership rules are built in. Projects can override them with either:

```text
config/legion_ownership.toml
.legion-ownership.toml
shared-context/governance/ownership.toml
```

Example:

```toml
[ownership]
"src/security/**" = ["security_assurance", "claude", "competing"]
"docs/**" = ["documentation", "alternating"]
```
