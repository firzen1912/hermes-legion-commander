# GitHub Health Gate

Hermes Legion Commander can gate a patch on GitHub Actions workflow status and open Dependabot alerts using the GitHub CLI (`gh`). It uses the user's existing `gh auth login` OAuth/keyring session instead of storing GitHub API tokens in Commander state.

## What it checks

- GitHub CLI discovery, including the default Windows install path when `gh` is not on `PATH`.
- Matching workflow runs for the current branch and commit SHA.
- Required workflow names, such as `ci` and `release-qualification`.
- Pending or failed workflow runs.
- Open Dependabot alerts via `gh api repos/{owner}/{repo}/dependabot/alerts`.
- Blocking severities, defaulting to all open alerts: `low,medium,high,critical`.

## Basic usage

From a target repository:

```powershell
$CommanderExe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"

& $CommanderExe github-health check `
  --repo "C:\path\to\target-repo" `
  --branch dev `
  --require-workflow ci `
  --require-workflow release-qualification `
  --block-severity low,medium,high,critical `
  --json
```

To wait for the current push to finish:

```powershell
& $CommanderExe github-health wait `
  --repo "C:\path\to\target-repo" `
  --branch dev `
  --head-sha HEAD `
  --require-workflow ci `
  --require-workflow release-qualification `
  --timeout-seconds 3600 `
  --interval-seconds 30
```

`--head-sha HEAD` is resolved by Commander to the local Git `HEAD`. You may also pass a full commit SHA.

## Artifacts

The gate writes these files by default:

```text
shared-context/github-health/github-health-report.json
shared-context/github-health/github-health-summary.md
shared-context/github-health/workflow-runs.json
shared-context/github-health/dependabot-alerts.json
```

Use `--out <dir>` to choose a different output directory.

## Exit codes

- `0`: all required workflow runs completed successfully and no blocking Dependabot alerts are open.
- `1`: GitHub was reachable, but the gate failed because workflows are pending/failed/missing or alerts are open.
- `2`: local setup/API failure, such as missing `gh`, unauthenticated `gh`, unparsable GitHub remote, or API error.

## Recommended patch acceptance command

Run this after pushing a patch and before merging or declaring the patch accepted:

```powershell
& $CommanderExe github-health wait `
  --repo "C:\path\to\target-repo" `
  --require-workflow ci `
  --require-workflow release-qualification `
  --block-severity low,medium,high,critical `
  --timeout-seconds 3600
```

This is intentionally strict. For dependency cleanup branches where low/medium alerts are tolerated temporarily, narrow the gate:

```powershell
--block-severity high,critical
```
