from pathlib import Path


def test_bootstrap_scripts_present_and_utf8():
    root = Path(__file__).resolve().parents[1]
    ps1 = root / "scripts" / "bootstrap-hermes-legion-commander.ps1"
    sh = root / "scripts" / "bootstrap-hermes-legion-commander.sh"
    assert ps1.is_file()
    assert sh.is_file()
    assert "https://chatgpt.com/codex/install.ps1" in ps1.read_text(encoding="utf-8")
    assert "https://claude.ai/install.sh" in sh.read_text(encoding="utf-8")
    assert "doctor" in ps1.read_text(encoding="utf-8")
    assert "doctor" in sh.read_text(encoding="utf-8")


def test_bootstrap_uses_dedicated_environment_and_profiles():
    root = Path(__file__).resolve().parents[1]
    ps1 = (root / "scripts" / "bootstrap-hermes-legion-commander.ps1").read_text(encoding="utf-8")
    sh = (root / "scripts" / "bootstrap-hermes-legion-commander.sh").read_text(encoding="utf-8")
    for text in (ps1, sh):
        assert "legion-supervisor" in text
        assert "legion-worker-a" in text
        assert "legion-worker-b" in text
        assert "RecreateEnvironment" in text or "--recreate-environment" in text
