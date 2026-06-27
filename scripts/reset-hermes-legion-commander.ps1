<#[.SYNOPSIS] Archive old state, reinstall v0.8.5, create fresh configs, and repair the Hermes supervisor. #>
[CmdletBinding()]
param(
  [string]$CommanderRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [Parameter(Mandatory=$true)][string]$TargetRepo,
  [string]$Profile = "legion-supervisor",
  [string]$WorkerProfileA = "legion-worker-a",
  [string]$WorkerProfileB = "legion-worker-b",
  [switch]$SkipInstall,
  [switch]$RunModelSmokeTests
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$ArchiveRoot = Join-Path $env:LOCALAPPDATA "HermesLegionCommanderArchives\$Stamp"
New-Item -ItemType Directory -Force -Path $ArchiveRoot | Out-Null
foreach ($Root in @(
  (Join-Path $env:LOCALAPPDATA "LegionCommander"),
  (Join-Path $env:LOCALAPPDATA "HermesLegionCommander")
)) {
  if (Test-Path $Root) {
    $Name = Split-Path $Root -Leaf
    Copy-Item $Root (Join-Path $ArchiveRoot $Name) -Recurse -Force
    if ($Name -eq "LegionCommander") {
      Remove-Item $Root -Recurse -Force
    } elseif ($Name -eq "HermesLegionCommander") {
      foreach ($Child in @("state", "venv")) {
        $ChildPath = Join-Path $Root $Child
        if (Test-Path $ChildPath) { Remove-Item $ChildPath -Recurse -Force }
      }
    }
  }
}
$ConfigDir = Join-Path $CommanderRepo "config"
foreach ($Name in @("model_council.local.toml","checkpoint_competition.local.toml")) {
  $Path = Join-Path $ConfigDir $Name
  if (Test-Path $Path) { Copy-Item $Path (Join-Path $ArchiveRoot $Name) -Force; Remove-Item $Path -Force }
}
$Wheel = Join-Path $CommanderRepo "dist\hermes_legion_commander-0.8.5-py3-none-any.whl"
$Installer = Join-Path $CommanderRepo "scripts\install-hermes-legion-commander.ps1"
if (-not $SkipInstall) {
  & $Installer -WheelPath $Wheel -ExpectedVersion "0.8.5" -RecreateEnvironment -AddScriptsToUserPath
  if ($LASTEXITCODE -ne 0) { throw "Installation failed" }
}
$Council = Join-Path $ConfigDir "model_council.local.toml"
$Checkpoint = Join-Path $ConfigDir "checkpoint_competition.local.toml"
Copy-Item (Join-Path $ConfigDir "model_council.example.toml") $Council -Force
Copy-Item (Join-Path $ConfigDir "checkpoint_competition.example.toml") $Checkpoint -Force
$RepoToml = ([IO.Path]::GetFullPath($TargetRepo)).Replace('\','/')
$StateToml = (Join-Path $env:LOCALAPPDATA "HermesLegionCommander\state").Replace('\','/')
function Patch([string]$Path) {
  $Text = [IO.File]::ReadAllText($Path).TrimStart([char]0xFEFF)
  $Text = [regex]::Replace($Text,'(?m)^repo\s*=\s*".*"$',"repo = `"$RepoToml`"")
  $StateName = if ([IO.Path]::GetFileName($Path) -like "model_council*") { "model-council" } else { "checkpoint-competition" }
  $Text = [regex]::Replace($Text,'(?m)^state_dir\s*=\s*".*"$',"state_dir = `"$StateToml/$StateName`"")
  $Text = [regex]::Replace($Text,'(?m)^research_dir\s*=\s*".*"$',"research_dir = `"$StateToml/research`"")
  $Text = [regex]::Replace($Text,'(?m)^roadmap_path\s*=\s*".*"$','roadmap_path = "docs/field-deployability-roadmap.md"')
  $Text = [regex]::Replace($Text,'(?m)^plan\s*=\s*".*"$','plan = "docs/field-deployability-roadmap.md"')
  [IO.File]::WriteAllText($Path,$Text,(New-Object Text.UTF8Encoding($false)))
}
Patch $Council; Patch $Checkpoint
$Exe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"
$Py = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\python.exe"
& $Py -P -c "import sys,tomllib;[tomllib.load(open(x,'rb')) for x in sys.argv[1:]];print('TOML valid')" $Council $Checkpoint
if ($LASTEXITCODE -ne 0) { throw "TOML validation failed" }
& $Exe supervisor --profile $Profile --worker-profile-a $WorkerProfileA --worker-profile-b $WorkerProfileB --repo-root $CommanderRepo setup --force
& $Exe council --config $Council workers --check
& $Exe council --config $Council preflight --repo $TargetRepo --preview-chars 200
& $Exe checkpoint --config $Checkpoint --repo $TargetRepo workers
& $Exe checkpoint --config $Checkpoint --repo $TargetRepo preflight --preview-chars 200
if ($RunModelSmokeTests) {
  "Reply with exactly CODEX_OK" | codex --sandbox read-only exec --output-last-message "$env:TEMP\codex-ok.txt" -
  claude -p "Reply with exactly CLAUDE_OK" --output-format json
}
$EnvFile = Join-Path $ConfigDir "hermes-legion-environment.ps1"
@"
`$CommanderRepo = "$CommanderRepo"
`$TargetRepo = "$TargetRepo"
`$CouncilConfig = "$Council"
`$CheckpointConfig = "$Checkpoint"
`$CommanderExe = "$Exe"
`$CommanderPython = "$Py"
`$HermesProfile = "$Profile"
`$HermesWorkerProfileA = "$WorkerProfileA"
`$HermesWorkerProfileB = "$WorkerProfileB"
"@ | ForEach-Object { [IO.File]::WriteAllText($EnvFile,$_ ,(New-Object Text.UTF8Encoding($false))) }
Write-Host "Reset complete. Archive: $ArchiveRoot" -ForegroundColor Green
Write-Host ". `"$EnvFile`""
