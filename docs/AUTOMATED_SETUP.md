# Automated Windows and Linux setup

Hermes Legion Commander includes idempotent bootstrap scripts for Windows
PowerShell and Linux/WSL Bash.

The scripts can:

- install missing `uv`, Hermes Agent, Codex CLI, and Claude Code using their
  official installers;
- install Python 3.11 through `uv`;
- install Commander into a dedicated virtual environment;
- verify that the target is a Git checkout with a roadmap;
- archive old local configs and optionally archive/reset run state;
- create fresh council and checkpoint configs;
- check or launch Codex, Claude, and Hermes authentication;
- archive and recreate `legion-supervisor`, `legion-worker-a`, and
  `legion-worker-b` from the configured default Hermes profile;
- run `doctor`, worker checks, and zero-model preflights;
- write a reusable environment file and JSON bootstrap report.

## Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass

.\scripts\bootstrap-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo"
```

## Linux or WSL2

```bash
chmod +x scripts/bootstrap-hermes-legion-commander.sh

./scripts/bootstrap-hermes-legion-commander.sh \
  --target-repo "$HOME/code/target-repo"
```

## Non-interactive verification

Authentication cannot be completed without credentials. In CI or unattended
environments, preconfigure credentials and use:

```powershell
.\scripts\bootstrap-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo" `
  -NonInteractive
```

```bash
./scripts/bootstrap-hermes-legion-commander.sh \
  --target-repo "$HOME/code/target-repo" \
  --non-interactive
```

Use `--skip-authentication` only when you intentionally want an installation
that is structurally valid but not yet able to call providers.

## Reset state

Windows:

```powershell
.\scripts\bootstrap-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo" `
  -ResetState
```

Linux:

```bash
./scripts/bootstrap-hermes-legion-commander.sh \
  --target-repo "$HOME/code/target-repo" \
  --reset-state
```

Old data is archived before removal.

## Live smoke tests

Live smoke tests consume a small amount of quota:

```powershell
.\scripts\bootstrap-hermes-legion-commander.ps1 `
  -TargetRepo "C:\path\to\target-repo" `
  -RunLiveSmokeTests
```

```bash
./scripts/bootstrap-hermes-legion-commander.sh \
  --target-repo "$HOME/code/target-repo" \
  --run-live-smoke-tests
```

## Doctor

```powershell
& $CommanderExe doctor `
  --repo-root $CommanderRepo `
  --target-repo $TargetRepo `
  --council-config $CouncilConfig `
  --checkpoint-config $CheckpointConfig
```

```bash
"$COMMANDER_EXE" doctor \
  --repo-root "$COMMANDER_REPO" \
  --target-repo "$TARGET_REPO" \
  --council-config "$COUNCIL_CONFIG" \
  --checkpoint-config "$CHECKPOINT_CONFIG"
```

`doctor` checks installation, tool versions, authentication, Hermes profiles,
TOML parsing, Git status, target repository, and roadmap discovery without
calling a model.

## Windows PowerShell 5.1 compatibility

The Windows bootstrap detects older Windows PowerShell/.NET Framework hosts
where `RuntimeInformation.OSArchitecture` is unavailable. It downloads the
official Codex installer, replaces only the architecture assignment with the
verified local `X64` or `Arm64` value, runs the installer in a child PowerShell
process, and deletes the temporary script afterward.

If the standalone installer still returns a nonzero exit code, the bootstrap
uses the official npm package fallback when `npm` is already available:

```powershell
npm install -g @openai/codex@latest
```

No fallback occurs silently; the bootstrap prints which path it used.
