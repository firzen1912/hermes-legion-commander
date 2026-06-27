[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ConfigPath)
$Resolved=(Resolve-Path -LiteralPath $ConfigPath).Path
$Stamp=Get-Date -Format "yyyyMMdd-HHmmss"
$Backup="$Resolved.pre-two-worker-$Stamp.bak"
Copy-Item -LiteralPath $Resolved -Destination $Backup -Force
$Text=[IO.File]::ReadAllText($Resolved).TrimStart([char]0xFEFF)
$Text=$Text -replace '(?m)^roadmap_plan_reviewer\s*=\s*"(?:google|gemini)"\s*$', 'roadmap_plan_reviewer = "gpt"'
$Text=$Text -replace '(?m)^researcher\s*=\s*"(?:google|gemini)"\s*$', 'researcher = "gpt"'
$Text=$Text -replace '(?m)^literature_reviewer\s*=\s*"gpt"\s*$', 'literature_reviewer = "claude"'
$Text=[regex]::Replace($Text,'(?ms)^\[agents\.(?:google|gemini)\]\s*.*?(?=^\[agents\.|^\[research\]|\z)','')
$Text=$Text -replace '(?m)^literature_reviewer\s*=\s*"(?:gpt|google|gemini)"\s*$', 'literature_reviewer = "claude"'
$Encoding=New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($Resolved,$Text.Trim()+"`n",$Encoding)
Write-Host "Migrated to Codex + Claude Code only: $Resolved" -ForegroundColor Green
Write-Host "Backup: $Backup"
