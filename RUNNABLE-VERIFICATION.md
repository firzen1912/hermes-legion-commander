# Runnable verification — v1.7.0

Hermes Legion Commander v1.7.0 is the first stable release. It carries the
supervisor/worker prompt contracts refined from manual Codex/Claude roadmap runs
and was promoted to GA after a security and capability review.

## Prompt improvements verified

- Generic worker SOUL includes version-boundary discipline.
- Dispatch contracts include commit policy, quota watermark, stop policy, generated-artifact policy, and host-side evidence policy.
- Version implementation, polish, security, and validation prompts include the roadmap execution contract.
- Supervisor goal contract includes quota and handoff policy.
- Host-side physical/HIL/field/audit/publication gates remain non-machine-awarded.

## Validation

- 161 automated tests passed.
- `compileall` clean across the package.
- Wheel built successfully.
- Wheel installed in a fresh virtual environment.
- Installed package reported version `1.7.0`.
- `show-worker-soul` exposes `Version-boundary discipline`, `HANDOFF:`, and `Host-side evidence boundary`.
- `show-goal-contract` exposes `Quota and handoff policy`.

## Wheel

`dist/hermes_legion_commander-1.7.0-py3-none-any.whl`

SHA-256: `437f73388c7fb15cced8d87172e331d48665d4e24f0c18d1effbb4fa6a75060f`
