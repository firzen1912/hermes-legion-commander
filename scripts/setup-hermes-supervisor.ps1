[CmdletBinding()]
param(
  [string]$Profile = "legion-supervisor",
  [string]$WorkerProfileA = "legion-worker-a",
  [string]$WorkerProfileB = "legion-worker-b",
  [string]$CommanderRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [switch]$Clone,
  [switch]$Force
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Exe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"
if (-not (Test-Path $Exe)) { throw "Hermes Legion Commander is not installed: $Exe" }
$args = @(
  "supervisor",
  "--profile", $Profile,
  "--worker-profile-a", $WorkerProfileA,
  "--worker-profile-b", $WorkerProfileB,
  "--repo-root", $CommanderRepo,
  "setup"
)
if ($Clone) { $args += "--clone" }
if ($Force) { $args += "--force" }
& $Exe @args
if ($LASTEXITCODE -ne 0) { throw "Hermes supervisor setup failed" }
Write-Host "Supervisor and generic workers ready." -ForegroundColor Green
Write-Host "Supervisor: hermes -p $Profile chat -q 'Show Hermes Legion Commander status.'"
Write-Host "Workers: $WorkerProfileA, $WorkerProfileB"
