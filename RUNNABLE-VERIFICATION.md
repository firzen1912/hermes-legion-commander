# Runnable verification — v1.7.4

Hermes Legion Commander v1.7.4 adds integrated workflow governance for autonomous-but-reviewable model work.

## Governance features verified

- Risk escalation recommendations before worker prompts.
- PR auto-triage and merge-readiness scoring.
- Review-comment rendering/posting support through GitHub CLI.
- File ownership and specialist-routing hints with TOML override support.
- Patch-budget checks.
- Evidence-diff explanation.
- No-regression memory injection.
- Local/CI parity warnings.
- Legion Commander branch listing and cleanup.
- Static dashboard generation.

## Validation

- `compileall` clean across the package.
- 179 tests passed in split pytest runs.
- Wheel built successfully.
- Wheel installed in a fresh virtual environment.
- Installed package reported version `1.7.4`.
- CLI help exposes the `governance` workflow.

## Wheel

`dist/hermes_legion_commander-1.7.4-py3-none-any.whl`

SHA-256: `e2bf9b3a9b3ccfd08be529d4cca08610c4b252c564b15fa91bd2942b924eb755`
