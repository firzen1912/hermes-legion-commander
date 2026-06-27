from pathlib import Path


def test_windows_bootstrap_patches_missing_runtime_architecture():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "bootstrap-hermes-legion-commander.ps1").read_text(encoding="utf-8")
    assert "function Install-CodexCli" in text
    assert 'GetProperty("OSArchitecture")' in text
    assert "PROCESSOR_ARCHITEW6432" in text
    assert "@openai/codex@latest" in text
    assert "codex-install-" in text


def test_codex_installer_runs_in_child_powershell():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "bootstrap-hermes-legion-commander.ps1").read_text(encoding="utf-8")
    assert "(Get-Process -Id $PID).Path" in text
    assert "-NoProfile -ExecutionPolicy Bypass -File $tempScript" in text
