# Operational scripts

- `install-hermes-legion-commander.ps1` / `.sh`: install or repair v1.7.0 in a dedicated virtual environment. The installer removes stale `hermes-legion-commander` and legacy `legion-commander` package files from that environment.
- `setup-hermes-supervisor.ps1` / `.sh`: create or repair `legion-supervisor` plus the role-neutral `legion-worker-a` and `legion-worker-b` profiles, SOUL files, and skills.
- `reset-hermes-legion-commander.ps1` / `.sh`: archive previous state and configs, reinstall, recreate clean council/checkpoint configs, configure the supervisor, and run zero-model checks.
- `repair-hermes-legion-commander.ps1` / `.sh`: verify or reinstall the package, native worker CLIs, supervisor profile, configs, and roadmap preflight.

The scripts never delete the target repository. Reset archives old state before clearing it.
