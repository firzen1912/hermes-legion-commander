# Runnable verification — v2.0.0

Hermes Legion Commander v2.0.0 is the first packaged release of the Legion v2 provider-neutral architecture.

## Verified capabilities

- Provider/model/runtime/auth/executor separation.
- Multiple OAuth/native/API accounts per provider.
- Localhost-only account and role control panel.
- Arbitrary roles, executor affinity/pools, and campaign DAGs.
- Resource/quota doctrine and protected human gates.
- Quota-aware clean-boundary supervisor handoffs.
- Exact reviewed 86-skill baseline with Graphify 0.9.43.
- Provider-neutral repository-data egress safeguard.
- Legacy collaborating/competing/alternating workflows retained as compatibility presets.

## Validation

- `compileall`: passed.
- `pytest`: 204 passed in 6.23s.
- Wheel build: passed.
- Wheel installed in a fresh virtual environment: passed.
- Installed package reported version `2.0.0`.
- Unified CLI, `legion`, and `gui` help probes: passed.

## Source

Source commit used for the release build: `1fa769b49e7df01ba40c57b58599237059e32466`

## Wheel

`dist/hermes_legion_commander-2.0.0-py3-none-any.whl`

SHA-256: `41228f10189ada0a49233643dd4f5e5c7df48ffd42b0638a57313afd3b7c5af5`
