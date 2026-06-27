<#
.SYNOPSIS
  Install or repair Hermes Legion Commander in a dedicated virtual environment.

.DESCRIPTION
  Installer v1.4.0 never modifies a uv-managed, system-managed, or externally
  managed base Python environment. The selected base interpreter is used only
  to create a dedicated virtual environment for Hermes Legion Commander.

  Default Windows install location:
    %LOCALAPPDATA%\HermesLegionCommander\venv

  The script:
  - verifies the base Python standard library;
  - creates or reuses a dedicated virtual environment;
  - bootstraps/upgrades pip inside that virtual environment;
  - removes stale Commander files only inside that environment;
  - installs from a wheel or source tree;
  - verifies that exactly one Commander distribution is installed;
  - optionally puts the environment's Scripts directory first on user PATH.

.EXAMPLE
  .\scripts\install-hermes-legion-commander.ps1 `
    -WheelPath ".\dist\hermes_legion_commander-0.8.5-py3-none-any.whl" `
    -ExpectedVersion "0.8.5" `
    -AddScriptsToUserPath

.EXAMPLE
  .\scripts\install-hermes-legion-commander.ps1 `
    -SourcePath "." `
    -ExpectedVersion "0.8.5" `
    -RecreateEnvironment `
    -AddScriptsToUserPath
#>

[CmdletBinding(DefaultParameterSetName = "Wheel")]
param(
    [Parameter(ParameterSetName = "Wheel", Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$WheelPath,

    [Parameter(ParameterSetName = "Source", Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourcePath,

    [string]$PythonPath,

    [string]$ExpectedVersion,

    [string]$InstallRoot,

    [switch]$AddScriptsToUserPath,

    [switch]$RecreateEnvironment,

    [switch]$SkipPipUpgrade
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$InstallerVersion = "1.4.0"

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$Executable,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Executable $($Arguments -join ' ')"
    }
}

function Resolve-BasePython {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        return (Resolve-Path -LiteralPath $RequestedPath -ErrorAction Stop).Path
    }

    $candidates = @()

    try {
        $pyList = & py -0p 2>$null
        foreach ($line in $pyList) {
            if ($line -match '([A-Za-z]:\\.*python(?:\.exe)?)\s*$') {
                $candidates += $Matches[1].Trim()
            }
        }
    } catch {}

    foreach ($name in @("python", "python3")) {
        try {
            $cmd = Get-Command $name -ErrorAction Stop
            if ($cmd.Source) {
                $candidates += $cmd.Source
            }
        } catch {}
    }

    $candidates = $candidates | Select-Object -Unique

    foreach ($candidate in $candidates) {
        try {
            $probe = & $candidate -c "import os,sys;print(sys.executable);print(os.__file__)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $probe.Count -ge 2) {
                return $candidate
            }
        } catch {}
    }

    throw @"
No usable Python interpreter was found.

Install a standard CPython distribution or uv Python, then rerun with:
  -PythonPath "C:\Path\To\python.exe"
"@
}

function Get-PythonInfo {
    param([string]$Python)

    $code = @'
import json
import os
import pathlib
import sys
import sysconfig

stdlib = sysconfig.get_path("stdlib")
payload = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "stdlib": stdlib,
    "purelib": sysconfig.get_path("purelib"),
    "scripts": sysconfig.get_path("scripts"),
    "os_module": getattr(os, "__file__", None),
    "stdlib_exists": bool(stdlib and pathlib.Path(stdlib).is_dir()),
}
print(json.dumps(payload))
'@

    $json = $code | & $Python -P -
    if ($LASTEXITCODE -ne 0) {
        throw "Python could not import its standard library."
    }

    $info = $json | ConvertFrom-Json
    if (-not $info.stdlib_exists) {
        throw "Python standard-library directory is missing: $($info.stdlib)"
    }
    if (-not $info.os_module -or -not (Test-Path -LiteralPath $info.os_module)) {
        throw "Python cannot locate its os module: $($info.os_module)"
    }
    return $info
}

function New-CommanderEnvironment {
    param(
        [string]$BasePython,
        [string]$VenvDirectory
    )

    $parent = Split-Path -Parent $VenvDirectory
    New-Item -ItemType Directory -Force -Path $parent | Out-Null

    Write-Step "Creating dedicated Commander virtual environment"
    & $BasePython -m venv $VenvDirectory
    if ($LASTEXITCODE -eq 0) {
        return
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Write-Host "python -m venv failed; retrying with uv venv."
        & $uv.Source venv --python $BasePython $VenvDirectory
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    throw @"
Unable to create a dedicated virtual environment.

Verify that the selected Python includes the venv module, or install uv and retry.
Base Python:
  $BasePython
"@
}

function Ensure-VenvPip {
    param(
        [string]$Python,
        [switch]$SkipUpgrade
    )

    & $Python -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "Bootstrapping pip inside the dedicated environment"
        Invoke-Checked $Python -m ensurepip --upgrade
    }

    if (-not $SkipUpgrade) {
        Write-Step "Upgrading pip, setuptools, and wheel inside the dedicated environment"
        Invoke-Checked $Python -m pip install --upgrade pip setuptools wheel
    }
}

function Remove-CommanderFromEnvironment {
    param([string]$Python)

    Write-Step "Removing stale Commander files from the dedicated environment"

    & $Python -m pip uninstall -y hermes-legion-commander legion-commander | Out-Host

    $info = Get-PythonInfo -Python $Python
    $targets = @()

    if (Test-Path -LiteralPath $info.purelib) {
        $targets += Get-ChildItem -LiteralPath $info.purelib -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -eq "hermes_legion_commander" -or
                $_.Name -like "hermes_legion_commander-*.dist-info" -or
                $_.Name -like "hermes_legion_commander*.egg-info" -or
                $_.Name -eq "legion_commander" -or
                $_.Name -like "legion_commander-*.dist-info" -or
                $_.Name -like "legion_commander*.egg-info"
            }
    }

    if (Test-Path -LiteralPath $info.scripts) {
        $targets += Get-ChildItem -LiteralPath $info.scripts -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -like "hermes-legion-commander*" -or
                $_.Name -like "hermes_legion_commander*" -or
                $_.Name -like "legion-commander*" -or
                $_.Name -like "legion_commander*"
            }
    }

    foreach ($target in $targets) {
        Write-Host "Removing $($target.FullName)"
        Remove-Item -LiteralPath $target.FullName -Recurse -Force
    }
}

function Put-ScriptsFirstOnUserPath {
    param([string]$Scripts)

    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($current) {
        $parts = $current.Split(";") |
            Where-Object { $_ -and $_ -ne $Scripts }
    }

    $newPath = (($Scripts) + $parts) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")

    if (($env:Path.Split(";")) -notcontains $Scripts) {
        $env:Path = "$Scripts;$env:Path"
    }

    Write-Host "Placed Commander Scripts first on user PATH: $Scripts" -ForegroundColor Green
}

Write-Host "Hermes Legion Commander installer v$InstallerVersion" -ForegroundColor Green

Write-Step "Clearing process-local Python and pip redirection variables"
foreach ($name in @(
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "PIP_PREFIX",
    "PIP_TARGET",
    "VIRTUAL_ENV"
)) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

# Prevent the current working directory or source checkout from shadowing the
# installed package during pip and verification subprocesses.
$env:PYTHONSAFEPATH = "1"

$BasePython = Resolve-BasePython -RequestedPath $PythonPath
Write-Host "Selected base Python: $BasePython" -ForegroundColor Green

Write-Step "Checking base Python standard-library health"
$BaseInfo = Get-PythonInfo -Python $BasePython
$BaseInfo | Format-List

if (-not $InstallRoot) {
    if ($env:LOCALAPPDATA) {
        $InstallRoot = Join-Path $env:LOCALAPPDATA "HermesLegionCommander"
    } else {
        $InstallRoot = Join-Path $HOME ".hermes-legion-commander"
    }
}

$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$VenvDirectory = Join-Path $InstallRoot "venv"
$VenvPython = Join-Path $VenvDirectory "Scripts\python.exe"

Write-Host "Installation root: $InstallRoot"
Write-Host "Virtual environment: $VenvDirectory"

if ($RecreateEnvironment -and (Test-Path -LiteralPath $VenvDirectory)) {
    Write-Step "Recreating the dedicated environment"
    Remove-Item -LiteralPath $VenvDirectory -Recurse -Force
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    New-CommanderEnvironment -BasePython $BasePython -VenvDirectory $VenvDirectory
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Virtual-environment Python was not created: $VenvPython"
}

Write-Step "Checking dedicated environment"
$VenvInfo = Get-PythonInfo -Python $VenvPython
$VenvInfo | Format-List

Ensure-VenvPip -Python $VenvPython -SkipUpgrade:$SkipPipUpgrade
Remove-CommanderFromEnvironment -Python $VenvPython

Write-Step "Installing Hermes Legion Commander into the dedicated environment"
if ($PSCmdlet.ParameterSetName -eq "Wheel") {
    $resolvedWheel = (Resolve-Path -LiteralPath $WheelPath -ErrorAction Stop).Path
    Invoke-Checked $VenvPython -m pip install --no-cache-dir --force-reinstall $resolvedWheel
} else {
    $resolvedSource = (Resolve-Path -LiteralPath $SourcePath -ErrorAction Stop).Path
    $pyproject = Join-Path $resolvedSource "pyproject.toml"
    if (-not (Test-Path -LiteralPath $pyproject)) {
        throw "SourcePath does not contain pyproject.toml: $resolvedSource"
    }
    Invoke-Checked $VenvPython -m pip install --no-cache-dir --force-reinstall $resolvedSource
}

Write-Step "Verifying installed package"
$verifyCode = @'
import importlib.metadata as md
import json
import pathlib
import sys
import sysconfig

purelib = pathlib.Path(sysconfig.get_path("purelib")).resolve()
matches = []
for dist in md.distributions(path=[str(purelib)]):
    name = (dist.metadata.get("Name") or "").lower()
    if name == "hermes-legion-commander":
        matches.append({
            "version": dist.version,
            "metadata_path": str(pathlib.Path(getattr(dist, "_path", "")).resolve()),
        })

if len(matches) != 1:
    raise SystemExit(
        f"Expected exactly one Commander distribution in {purelib}; found {len(matches)}"
    )

import hermes_legion_commander

loaded_from = pathlib.Path(hermes_legion_commander.__file__).resolve()
try:
    loaded_from.relative_to(purelib)
    loaded_from_venv = True
except ValueError:
    loaded_from_venv = False

print(json.dumps({
    "python": sys.executable,
    "purelib": str(purelib),
    "loaded_from": str(loaded_from),
    "loaded_from_venv": loaded_from_venv,
    "installed_version": matches[0]["version"],
    "matching_distributions": matches,
}))
'@

$verificationJson = $verifyCode | & $VenvPython -P -
if ($LASTEXITCODE -ne 0) {
    throw "Hermes Legion Commander verification failed."
}

$verification = $verificationJson | ConvertFrom-Json
$verification | ConvertTo-Json -Depth 5

if ($verification.matching_distributions.Count -ne 1) {
    throw "Expected exactly one Commander distribution in the dedicated environment; found $($verification.matching_distributions.Count)."
}

if (-not $verification.loaded_from_venv) {
    throw "Commander imported from outside the dedicated environment: $($verification.loaded_from)"
}

if ($ExpectedVersion -and $verification.installed_version -ne $ExpectedVersion) {
    throw "Expected version $ExpectedVersion but installed $($verification.installed_version)."
}

$CommanderScripts = $VenvInfo.scripts
$CommanderExecutable = Join-Path $CommanderScripts "hermes-legion-commander.exe"

if (-not (Test-Path -LiteralPath $CommanderExecutable)) {
    throw "Commander launcher was not created: $CommanderExecutable"
}

if ($AddScriptsToUserPath) {
    Put-ScriptsFirstOnUserPath -Scripts $CommanderScripts
}

Write-Host "`nHermes Legion Commander installation is healthy." -ForegroundColor Green
Write-Host "Dedicated Python:"
Write-Host "  $VenvPython"
Write-Host "Direct launcher:"
Write-Host "  $CommanderExecutable"
Write-Host "Module invocation:"
Write-Host "  & `"$VenvPython`" -P -m hermes_legion_commander.cli --help"

if ($AddScriptsToUserPath) {
    Write-Host "The direct command is available in this session and future terminals:"
    Write-Host "  hermes-legion-commander --help"
} else {
    Write-Host "Run with the full launcher path or rerun using -AddScriptsToUserPath."
}
