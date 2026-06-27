from pathlib import Path


def test_powershell_bootstrap_does_not_return_installer_transcript_as_tool_path():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "bootstrap-hermes-legion-commander.ps1").read_text(encoding="utf-8")
    assert "$installerTranscript = @(& $Installer 2>&1)" in text
    assert "foreach ($line in $installerTranscript) { Write-Host $line }" in text
    assert "[string]$resolved = Resolve-Tool $Name $Fallbacks" in text
    ensure = text.split("function Ensure-Tool", 1)[1].split("function Archive-Path", 1)[0]
    assert "\n  & $Installer\n" not in ensure


def test_powershell_bootstrap_refreshes_official_codex_install_path():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "bootstrap-hermes-legion-commander.ps1").read_text(encoding="utf-8")
    assert "$env:LOCALAPPDATA\\Programs\\OpenAI\\Codex\\bin" in text
