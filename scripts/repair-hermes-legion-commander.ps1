[CmdletBinding()]
param(
  [string]$CommanderRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [string]$TargetRepo,
  [string]$Profile = "legion-supervisor",
  [string]$WorkerProfileA = "legion-worker-a",
  [string]$WorkerProfileB = "legion-worker-b",
  [switch]$Reinstall
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Wheel = Join-Path $CommanderRepo "dist\hermes_legion_commander-0.8.5-py3-none-any.whl"
$Installer = Join-Path $CommanderRepo "scripts\install-hermes-legion-commander.ps1"
$Exe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"
if ($Reinstall -or -not (Test-Path $Exe)) { & $Installer -WheelPath $Wheel -ExpectedVersion "0.8.5" -RecreateEnvironment -AddScriptsToUserPath }
foreach ($Name in @("hermes","codex","claude","git")) { if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing command: $Name" } }
& $Exe supervisor --profile $Profile --worker-profile-a $WorkerProfileA --worker-profile-b $WorkerProfileB --repo-root $CommanderRepo setup --force
$Council = Join-Path $CommanderRepo "config\model_council.local.toml"
$Checkpoint = Join-Path $CommanderRepo "config\checkpoint_competition.local.toml"
if (Test-Path $Council) { & $Exe council --config $Council workers --check; if ($TargetRepo) { & $Exe council --config $Council preflight --repo $TargetRepo --preview-chars 120 } }
if ((Test-Path $Checkpoint) -and $TargetRepo) { & $Exe checkpoint --config $Checkpoint --repo $TargetRepo workers }
Write-Host "Repair checks completed." -ForegroundColor Green
