from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hermes_legion_commander import workflow_governance as wg
from hermes_legion_commander.worker_runtime import build_prompt_with_shared_context


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    assert cp.returncode == 0, cp.stderr or cp.stdout
    return cp.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Rules\nDo not bypass gates.\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "beta-release-roadmap.md").write_text("# Roadmap\nStatus: Planned\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "dev")
    return repo


def test_risk_escalates_security_and_release_files() -> None:
    report = wg.classify_risk(["src/security/keyring.py", "docs/README.md"])
    assert report["risk_level"] == "high"
    assert report["recommended_mode"] == "competing"
    report2 = wg.classify_risk(["src/release/provenance.py"])
    assert report2["risk_level"] == "critical"
    assert "final-verify" in report2["required_gates"]


def test_patch_budget_and_evidence_explainer() -> None:
    paths = ["evidence/beta/manifest.sig", "results/evidence/iterations/99/results.json"]
    diff = {"lines": 20}
    budget = wg.patch_budget(paths, diff, budget={"max_files": 1})
    assert not budget["ok"]
    evidence = wg.evidence_diff(Path.cwd(), paths)
    assert evidence["requires_explanation"]
    assert evidence["count"] == 2
    assert any(row["kind"] == "signature-or-key-churn" for row in evidence["changed_evidence_files"])


def test_refresh_governance_writes_artifacts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "Target project").mkdir()
    (repo / "Target project" / "security").mkdir(parents=True)
    (repo / "Target project" / "security" / "keyring.py").write_text("# change\n", encoding="utf-8")
    context = tmp_path / "ctx"
    report = wg.refresh_governance(context, repo, include_github=False)
    assert report["risk"]["recommended_mode"] == "competing"
    assert (context / "GOVERNANCE.md").is_file()
    assert (context / "governance" / "merge-readiness.json").is_file()
    assert (context / "dashboard" / "index.html").is_file()
    saved = json.loads((context / "governance" / "governance-report.json").read_text())
    assert saved["changed_files"] == ["src/security/keyring.py"]


def test_regression_memory_appends_prompt_lessons(tmp_path: Path) -> None:
    row = wg.append_regression_memory(tmp_path, title="CRLF evidence", rule="Normalize before signing", evidence="ci run")
    assert row["title"] == "CRLF evidence"
    memory = wg.regression_memory(tmp_path, "line-ending failure", ["evidence/beta/manifest.json"])
    assert memory["rule_count"] >= 2
    assert (tmp_path / "prompt-lessons.md").read_text(encoding="utf-8").count("Normalize") == 1


def test_prompt_builder_injects_governance_pack(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tools").mkdir()
    (repo / "tools" / "x.py").write_text("print('x')\n", encoding="utf-8")
    context = tmp_path / "worker-context"
    prompt = build_prompt_with_shared_context("Fix tooling", context, repo, 60000, include_git_snapshot=False)
    assert "## GOVERNANCE.md" in prompt
    assert "Legion Commander Governance" in prompt
    assert (context / "governance" / "merge-readiness.md").is_file()
