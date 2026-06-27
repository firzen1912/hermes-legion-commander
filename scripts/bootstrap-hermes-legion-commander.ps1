<#
.SYNOPSIS
  End-to-end Windows bootstrap for Hermes Legion Commander.

.DESCRIPTION
  Installs missing official tooling, installs Hermes Legion Commander in a
  dedicated virtual environment, creates fresh local configs, creates or repairs
  the supervisor and two generic worker profiles, checks authentication, and
  runs zero-model council and checkpoint preflights.

  Authentication remains interactive when an account login is required.

.EXAMPLE
  .\scripts\bootstrap-hermes-legion-commander.ps1 `
    -TargetRepo "C:\path\to\target-repo"

.EXAMPLE
  .\scripts\bootstrap-hermes-legion-commander.ps1 `
    -TargetRepo "C:\path\to\target-repo" `
    -ResetState `
    -RunLiveSmokeTests
#>
[CmdletBinding()]
param(
  [string]$CommanderRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
  [Parameter(Mandatory=$true)][string]$TargetRepo,
  [string]$Profile = "legion-supervisor",
  [string]$WorkerProfileA = "legion-worker-a",
  [string]$WorkerProfileB = "legion-worker-b",
  [switch]$SkipToolInstall,
  [switch]$SkipAuthentication,
  [switch]$NonInteractive,
  [switch]$ResetState,
  [switch]$AllowDirtyTarget,
  [switch]$RunLiveSmokeTests
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Version = "0.8.5"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Step([string]$Message) {
  Write-Host "`n==> $Message" -ForegroundColor Cyan
}
function Ok([string]$Message) {
  Write-Host "[OK] $Message" -ForegroundColor Green
}
function Warn([string]$Message) {
  Write-Host "[WARN] $Message" -ForegroundColor Yellow
}
function Fail([string]$Message) {
  throw "Hermes Legion Commander bootstrap stopped: $Message"
}
function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $known = @(
    "$env:USERPROFILE\.local\bin",
    "$env:LOCALAPPDATA\hermes\bin",
    "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts",
    "$env:LOCALAPPDATA\Programs\OpenAI\Codex\bin",
    "$env:LOCALAPPDATA\Programs\Claude",
    "$env:LOCALAPPDATA\Programs\Claude Code"
  )
  $parts = @($known + ($machine -split ";") + ($user -split ";") + ($env:Path -split ";")) |
    Where-Object { $_ -and (Test-Path $_) } |
    Select-Object -Unique
  $env:Path = $parts -join ";"
}
function Download-Script([string]$Url) {
  $content = Invoke-RestMethod -Uri $Url
  if (-not $content -or $content.Length -lt 100) {
    Fail "Downloaded installer was unexpectedly empty: $Url"
  }
  return [scriptblock]::Create([string]$content)
}
function Install-CodexCli {
  $url = "https://chatgpt.com/codex/install.ps1"
  $content = [string](Invoke-RestMethod -Uri $url)
  if (-not $content -or $content.Length -lt 100) {
    Fail "Downloaded Codex installer was unexpectedly empty: $url"
  }

  # Windows PowerShell 5.1 can run on a .NET Framework build where
  # RuntimeInformation.OSArchitecture does not exist. The official Codex
  # installer uses that property, so patch only that assignment when absent.
  $runtimeType = [System.Runtime.InteropServices.RuntimeInformation]
  $architectureProperty = $runtimeType.GetProperty("OSArchitecture")
  if ($null -eq $architectureProperty) {
    $rawArchitecture = if (-not [string]::IsNullOrWhiteSpace($env:PROCESSOR_ARCHITEW6432)) {
      $env:PROCESSOR_ARCHITEW6432
    } else {
      $env:PROCESSOR_ARCHITECTURE
    }
    $installerArchitecture = switch -Regex ($rawArchitecture) {
      '^(?i:ARM64|AARCH64)$' { 'Arm64'; break }
      '^(?i:AMD64|X64|X86_64)$' { 'X64'; break }
      default { Fail "Unsupported Windows architecture for Codex: $rawArchitecture" }
    }
    $pattern = '(?m)^\$architecture\s*=\s*\[System\.Runtime\.InteropServices\.RuntimeInformation\]::OSArchitecture\s*$'
    if ($content -notmatch $pattern) {
      Fail "The Codex installer no longer contains the expected architecture assignment. Refusing an unsafe patch."
    }
    $replacement = '$architecture = "' + $installerArchitecture + '"'
    $content = [regex]::Replace($content, $pattern, $replacement)
    Warn "Applied a Windows PowerShell 5.1 compatibility patch for Codex architecture detection ($installerArchitecture)."
  }

  $tempScript = Join-Path ([IO.Path]::GetTempPath()) ("codex-install-{0}.ps1" -f [guid]::NewGuid().ToString("N"))
  try {
    Write-Utf8NoBom $tempScript $content
    $hostExecutable = (Get-Process -Id $PID).Path
    $old = $env:CODEX_NON_INTERACTIVE
    try {
      $env:CODEX_NON_INTERACTIVE = "1"
      & $hostExecutable -NoProfile -ExecutionPolicy Bypass -File $tempScript
      $installerExit = $LASTEXITCODE
    } finally {
      if ($null -eq $old) { Remove-Item Env:CODEX_NON_INTERACTIVE -ErrorAction SilentlyContinue }
      else { $env:CODEX_NON_INTERACTIVE = $old }
    }
    if ($installerExit -eq 0) { return }

    Warn "The standalone Codex installer exited with code $installerExit. Trying the official npm package fallback."
    $npm = Resolve-Tool "npm"
    if (-not $npm) {
      Fail "Codex installation failed and npm is not available for the official @openai/codex fallback."
    }
    & $npm install -g "@openai/codex@latest"
    if ($LASTEXITCODE -ne 0) {
      Fail "Both the standalone Codex installer and npm fallback failed."
    }
  } finally {
    Remove-Item $tempScript -Force -ErrorAction SilentlyContinue
  }
}

function Resolve-Tool([string]$Name, [string[]]$Fallbacks = @()) {
  Refresh-Path
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($path in $Fallbacks) {
    if ($path -and (Test-Path $path)) { return $path }
  }
  return $null
}
function Ensure-Tool(
  [string]$Name,
  [scriptblock]$Installer,
  [string[]]$Fallbacks = @()
) {
  $resolved = Resolve-Tool $Name $Fallbacks
  if ($resolved) {
    Ok "$Name -> $resolved"
    return $resolved
  }
  if ($SkipToolInstall) {
    Fail "$Name is missing and -SkipToolInstall was specified"
  }
  Step "Installing $Name from its official installer"
  # PowerShell functions return every object written to the success stream.
  # Installers are verbose, so capture their transcript and render it through
  # Write-Host; otherwise assigning Ensure-Tool to a variable also captures
  # the installer transcript and corrupts the executable path.
  $installerTranscript = @()
  try {
    $installerTranscript = @(& $Installer 2>&1)
  } catch {
    foreach ($line in $installerTranscript) { Write-Host $line }
    throw
  }
  foreach ($line in $installerTranscript) { Write-Host $line }
  Refresh-Path
  [string]$resolved = Resolve-Tool $Name $Fallbacks
  if (-not $resolved) { Fail "$Name installation completed but the executable is still unavailable" }
  Ok "$Name -> $resolved"
  return $resolved
}
function Archive-Path([string]$Path, [string]$ArchiveRoot) {
  if (Test-Path $Path) {
    New-Item -ItemType Directory -Force -Path $ArchiveRoot | Out-Null
    Copy-Item $Path (Join-Path $ArchiveRoot (Split-Path $Path -Leaf)) -Recurse -Force
  }
}
function Write-Utf8NoBom([string]$Path, [string]$Content) {
  [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
}

Step "Resolving repositories"
$CommanderRepo = [IO.Path]::GetFullPath($CommanderRepo)
$TargetRepo = [IO.Path]::GetFullPath($TargetRepo)
$Wheel = Join-Path $CommanderRepo "dist\hermes_legion_commander-$Version-py3-none-any.whl"
$Installer = Join-Path $CommanderRepo "scripts\install-hermes-legion-commander.ps1"
foreach ($path in @(
  (Join-Path $CommanderRepo "pyproject.toml"),
  $Wheel,
  $Installer,
  $TargetRepo
)) {
  if (-not (Test-Path $path)) { Fail "Required path is missing: $path" }
}
Ok "Commander repository: $CommanderRepo"
Ok "Target repository: $TargetRepo"

Step "Installing or resolving official prerequisites"
$uv = Ensure-Tool "uv" {
  & (Download-Script "https://astral.sh/uv/install.ps1")
}
$hermesFallbacks = @(
  "$env:LOCALAPPDATA\hermes\bin\hermes.cmd",
  "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe"
)
$hermes = Ensure-Tool "hermes" {
  & (Download-Script "https://hermes-agent.nousresearch.com/install.ps1") -SkipSetup
} $hermesFallbacks
$codex = Ensure-Tool "codex" {
  Install-CodexCli
}
$claude = Ensure-Tool "claude" {
  & (Download-Script "https://claude.ai/install.ps1")
}

Step "Verifying prerequisite versions"
foreach ($tool in @($uv, $hermes, $codex, $claude)) {
  & $tool --version
  if ($LASTEXITCODE -ne 0) { Fail "Version check failed: $tool" }
}

Step "Ensuring Python 3.11 or newer"
$Python = $null
$candidates = @()
try {
  $pyList = & py -0p 2>$null
  foreach ($line in $pyList) {
    if ($line -match '([A-Za-z]:\.*python(?:\.exe)?)\s*$') { $candidates += $Matches[1].Trim() }
  }
} catch {}
foreach ($name in @("python", "python3")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { $candidates += $cmd.Source }
}
foreach ($candidate in ($candidates | Select-Object -Unique)) {
  & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>$null
  if ($LASTEXITCODE -eq 0) { $Python = $candidate; break }
}
if (-not $Python) {
  $found = @(& $uv python find ">=3.11,<3.14" 2>$null)
  if ($LASTEXITCODE -eq 0 -and $found.Count -gt 0 -and (Test-Path $found[-1])) {
    $Python = $found[-1].Trim()
  } else {
    & $uv python install 3.11
    if ($LASTEXITCODE -ne 0) { Fail "uv could not install Python 3.11" }
    $Python = (& $uv python find 3.11).Trim()
  }
}
if (-not $Python -or -not (Test-Path $Python)) { Fail "No usable Python 3.11 or newer interpreter was found" }
Ok "Python: $Python"

Step "Installing Hermes Legion Commander $Version"
& $Installer `
  -WheelPath $Wheel `
  -ExpectedVersion $Version `
  -PythonPath $Python `
  -RecreateEnvironment `
  -AddScriptsToUserPath
if ($LASTEXITCODE -ne 0) { Fail "Commander installation failed" }
$CommanderExe = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\hermes-legion-commander.exe"
$CommanderPython = "$env:LOCALAPPDATA\HermesLegionCommander\venv\Scripts\python.exe"
if (-not (Test-Path $CommanderExe)) { Fail "Commander executable missing: $CommanderExe" }

Step "Verifying target Git checkout"
$git = Resolve-Tool "git" @(
  "$env:LOCALAPPDATA\hermes\git\cmd\git.exe",
  "$env:LOCALAPPDATA\hermes\git\bin\git.exe"
)
if (-not $git) {
  $found = Get-ChildItem "$env:LOCALAPPDATA\hermes" -Filter git.exe -File -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($found) { $git = $found.FullName }
}
if (-not $git) { Fail "git is unavailable after Hermes installation" }
& $git -C $TargetRepo rev-parse --is-inside-work-tree
if ($LASTEXITCODE -ne 0) { Fail "Target is not a Git checkout: $TargetRepo" }
$status = @(& $git -C $TargetRepo status --short)
if ($LASTEXITCODE -ne 0) { Fail "Could not read target Git status" }
if (($status -join "").Trim() -and -not $AllowDirtyTarget) {
  $status | ForEach-Object { Write-Host $_ }
  Fail "Target working tree is dirty. Commit/stash changes or use -AllowDirtyTarget."
}
$roadmaps = @(Get-ChildItem (Join-Path $TargetRepo "docs") -Filter "*roadmap*.md" -File -ErrorAction SilentlyContinue)
if ($roadmaps.Count -eq 0) { Fail "No *roadmap*.md file exists under $TargetRepo\docs" }
Ok "Roadmap: $($roadmaps[0].FullName)"

Step "Archiving old configuration and optional state"
$ArchiveRoot = Join-Path $env:LOCALAPPDATA "HermesLegionCommanderArchives\$Stamp"
New-Item -ItemType Directory -Force -Path $ArchiveRoot | Out-Null
$ConfigDir = Join-Path $CommanderRepo "config"
foreach ($name in @("model_council.local.toml", "checkpoint_competition.local.toml", "hermes-legion-environment.ps1")) {
  $path = Join-Path $ConfigDir $name
  if (Test-Path $path) { Copy-Item $path $ArchiveRoot -Force }
}
$StateRoot = Join-Path $env:LOCALAPPDATA "HermesLegionCommander\state"
if ($ResetState -and (Test-Path $StateRoot)) {
  Archive-Path $StateRoot $ArchiveRoot
  Remove-Item $StateRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
Ok "Archive: $ArchiveRoot"

Step "Creating fresh local configurations"
$Council = Join-Path $ConfigDir "model_council.local.toml"
$Checkpoint = Join-Path $ConfigDir "checkpoint_competition.local.toml"
Copy-Item (Join-Path $ConfigDir "model_council.example.toml") $Council -Force
Copy-Item (Join-Path $ConfigDir "checkpoint_competition.example.toml") $Checkpoint -Force
$RepoToml = $TargetRepo.Replace("\", "/")
$StateToml = $StateRoot.Replace("\", "/")
function Patch-Config([string]$Path, [string]$StateName) {
  $text = [IO.File]::ReadAllText($Path).TrimStart([char]0xFEFF)
  $text = [regex]::Replace($text, '(?m)^repo\s*=\s*".*"$', "repo = `"$RepoToml`"")
  $text = [regex]::Replace($text, '(?m)^state_dir\s*=\s*".*"$', "state_dir = `"$StateToml/$StateName`"")
  $text = [regex]::Replace($text, '(?m)^research_dir\s*=\s*".*"$', "research_dir = `"$StateToml/research`"")
  $text = [regex]::Replace($text, '(?m)^roadmap_path\s*=\s*".*"$', 'roadmap_path = "docs/field-deployability-roadmap.md"')
  $text = [regex]::Replace($text, '(?m)^plan\s*=\s*".*"$', 'plan = "docs/field-deployability-roadmap.md"')
  $text = $text.Replace('["python", "-m", "pytest", "-q"]', '["uv", "run", "python", "-m", "pytest", "-q"]')
  $text = $text.Replace('["python", "-m", "ruff", "check"', '["uv", "run", "ruff", "check"')
  $text = $text.Replace('version_test_command = ["python"', 'version_test_command = ["uv", "run", "python"')
  $text = $text.Replace('version_experiment_command = ["python"]', 'version_experiment_command = ["uv", "run", "python"]')
  Write-Utf8NoBom $Path $text
}
Patch-Config $Council "model-council"
Patch-Config $Checkpoint "checkpoint-competition"
& $CommanderPython -P -c "import sys,tomllib; [tomllib.load(open(p,'rb')) for p in sys.argv[1:]]; print('TOML valid')" $Council $Checkpoint
if ($LASTEXITCODE -ne 0) { Fail "Generated TOML is invalid" }

if (-not $SkipAuthentication) {
  Step "Checking native CLI authentication"
  & $codex login status
  if ($LASTEXITCODE -ne 0) {
    if ($NonInteractive) { Fail "Codex is not authenticated in non-interactive mode" }
    & $codex login
    if ($LASTEXITCODE -ne 0) { Fail "Codex login failed" }
  }
  & $claude auth status
  if ($LASTEXITCODE -ne 0) {
    if ($NonInteractive) { Fail "Claude Code is not authenticated in non-interactive mode" }
    & $claude auth login
    if ($LASTEXITCODE -ne 0) { Fail "Claude Code login failed" }
  }

  Step "Checking Hermes configuration"
  & $hermes config check
  if ($LASTEXITCODE -ne 0) {
    if ($NonInteractive) { Fail "Hermes requires setup in non-interactive mode" }
    & $hermes setup
    if ($LASTEXITCODE -ne 0) { Fail "Hermes setup failed" }
  }
}

Step "Recreating Hermes supervisor and generic worker profiles"
$ProfileArchive = Join-Path $ArchiveRoot "hermes-profiles"
New-Item -ItemType Directory -Force -Path $ProfileArchive | Out-Null
$profileList = (& $hermes profile list 2>$null) -join "`n"
foreach ($name in @($Profile, $WorkerProfileA, $WorkerProfileB)) {
  if ($profileList -match "(?m)^\s*\*?\s*$([regex]::Escape($name))\s*$") {
    & $hermes profile export $name -o (Join-Path $ProfileArchive "$name.tar.gz")
    & $hermes profile delete $name --yes
  }
}
& $CommanderExe supervisor `
  --profile $Profile `
  --worker-profile-a $WorkerProfileA `
  --worker-profile-b $WorkerProfileB `
  --repo-root $CommanderRepo `
  setup --clone --force
if ($LASTEXITCODE -ne 0) { Fail "Hermes profile setup failed" }

Step "Running zero-model diagnostics and preflights"
$DoctorArgs = @(
  "doctor",
  "--repo-root", $CommanderRepo,
  "--target-repo", $TargetRepo,
  "--council-config", $Council,
  "--checkpoint-config", $Checkpoint
)
if ($SkipAuthentication) { $DoctorArgs += "--skip-auth" }
& $CommanderExe @DoctorArgs
if ($LASTEXITCODE -ne 0) { Fail "Doctor reported failures" }
& $CommanderExe council --config $Council workers --check
if ($LASTEXITCODE -ne 0) { Fail "Council worker check failed" }
& $CommanderExe council --config $Council preflight --repo $TargetRepo --preview-chars 300
if ($LASTEXITCODE -ne 0) { Fail "Council preflight failed" }
& $CommanderExe checkpoint --config $Checkpoint --repo $TargetRepo workers
if ($LASTEXITCODE -ne 0) { Fail "Checkpoint worker check failed" }
& $CommanderExe checkpoint --config $Checkpoint --repo $TargetRepo preflight --preview-chars 300
if ($LASTEXITCODE -ne 0) { Fail "Checkpoint preflight failed" }

if ($RunLiveSmokeTests) {
  Step "Running live low-cost smoke tests"
  $codexOut = Join-Path $env:TEMP "hlc-codex-$Stamp.txt"
  "Reply with exactly CODEX_OK. Do not modify files." |
    & $codex --sandbox read-only exec --output-last-message $codexOut -
  if ($LASTEXITCODE -ne 0 -or (Get-Content $codexOut -Raw) -notmatch '\bCODEX_OK\b') {
    Fail "Codex live smoke test failed"
  }
  $claudeOut = (& $claude -p "Reply with exactly CLAUDE_OK. Do not modify files." --output-format json 2>&1) -join "`n"
  if ($LASTEXITCODE -ne 0 -or $claudeOut -notmatch '\bCLAUDE_OK\b') {
    Fail "Claude live smoke test failed"
  }
  $hermesOut = (& $hermes -p $Profile chat -q "Reply with exactly HERMES_OK. Do not use tools." 2>&1) -join "`n"
  if ($LASTEXITCODE -ne 0 -or $hermesOut -notmatch '\bHERMES_OK\b') {
    Fail "Hermes supervisor live smoke test failed"
  }
}

Step "Writing reusable environment and report"
$EnvFile = Join-Path $ConfigDir "hermes-legion-environment.ps1"
Write-Utf8NoBom $EnvFile @"
`$CommanderRepo = "$CommanderRepo"
`$TargetRepo = "$TargetRepo"
`$CouncilConfig = "$Council"
`$CheckpointConfig = "$Checkpoint"
`$CommanderExe = "$CommanderExe"
`$CommanderPython = "$CommanderPython"
`$HermesProfile = "$Profile"
`$HermesWorkerProfileA = "$WorkerProfileA"
`$HermesWorkerProfileB = "$WorkerProfileB"
"@
$Report = [ordered]@{
  ready = $true
  version = $Version
  platform = "windows"
  commander_repo = $CommanderRepo
  target_repo = $TargetRepo
  council_config = $Council
  checkpoint_config = $Checkpoint
  archive = $ArchiveRoot
  profiles = @($Profile, $WorkerProfileA, $WorkerProfileB)
  completed_at = [DateTimeOffset]::Now.ToString("o")
}
$ReportJson = $Report | ConvertTo-Json -Depth 8
Write-Utf8NoBom (Join-Path $ConfigDir "bootstrap-report.json") $ReportJson

Write-Host "`nHermes Legion Commander is ready." -ForegroundColor Green
Write-Host "Load environment: . `"$EnvFile`""
Write-Host "Doctor: & `"$CommanderExe`" doctor --repo-root `"$CommanderRepo`" --target-repo `"$TargetRepo`" --council-config `"$Council`" --checkpoint-config `"$Checkpoint`""
