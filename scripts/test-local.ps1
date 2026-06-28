[CmdletBinding()]
param(
  [string]$VenvPath,
  [string]$Python,
  [switch]$NoInstall,
  [string[]]$PytestArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $VenvPath) {
  $VenvPath = Join-Path $Repo "state\test-venv"
}
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

function Resolve-HostPython {
  if ($Python) {
    return $Python
  }
  foreach ($Command in @(
      @("py", "-3.11"),
      @("py", "-3"),
      @("python"),
      @("python3")
    )) {
    $Name = $Command[0]
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
      continue
    }
    try {
      $ExtraArgs = @()
      if ($Command.Count -gt 1) {
        $ExtraArgs = $Command[1..($Command.Count - 1)]
      }
      $Probe = & $Name @ExtraArgs -c "import sys; print(sys.executable)"
      if ($LASTEXITCODE -eq 0 -and $Probe) {
        return ([string]::Join("", $Probe)).Trim()
      }
    } catch {
      continue
    }
  }
  throw "No Python interpreter found. Pass -Python with an absolute path to python.exe."
}

if (-not (Test-Path $VenvPython)) {
  $HostPython = Resolve-HostPython
  & $HostPython -m venv $VenvPath
}

if (-not $NoInstall) {
  & $VenvPython -m pip install --disable-pip-version-check pytest
}

$TempRoot = Join-Path $Repo "state\temp"
$PytestTemp = Join-Path $Repo "state\pytest-tmp"
New-Item -ItemType Directory -Force $TempRoot, $PytestTemp | Out-Null
$env:TEMP = $TempRoot
$env:TMP = $TempRoot

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
  $PytestArgs = @("tests")
}
if (-not ($PytestArgs | Where-Object { $_ -eq "--basetemp" -or $_.StartsWith("--basetemp=") })) {
  $PytestArgs += @("--basetemp", $PytestTemp)
}

& $VenvPython -m pytest @PytestArgs
