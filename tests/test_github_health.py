from __future__ import annotations

import json
from pathlib import Path

from hermes_legion_commander import cli, github_health


def test_owner_repo_from_remote_variants():
    assert github_health.owner_repo_from_remote("https://github.com/example-owner/example-repo.git") == "example-owner/example-repo"
    assert github_health.owner_repo_from_remote("git@github.com:example-owner/example-repo.git") == "example-owner/example-repo"
    assert github_health.owner_repo_from_remote("example-owner/example-repo") == "example-owner/example-repo"
    assert github_health.owner_repo_from_remote("https://example.invalid/x/y.git") is None


def test_find_gh_falls_back_to_windows_location(tmp_path, monkeypatch):
    fake = tmp_path / "GitHub CLI" / "gh.exe"
    fake.parent.mkdir(parents=True)
    fake.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(github_health.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert github_health.find_gh() == fake


def test_workflow_gate_rejects_pending_failed_and_missing():
    runs = [
        {"name": "ci", "status": "completed", "conclusion": "success"},
        {"name": "release-qualification", "status": "in_progress", "conclusion": None},
        {"name": "docs", "status": "completed", "conclusion": "failure"},
    ]
    gate = github_health.workflow_gate(runs, require_workflows=("ci", "release-qualification", "security"))
    assert not gate["ok"]
    assert gate["missing_required_workflows"] == ["security"]
    assert len(gate["pending_runs"]) == 1
    assert len(gate["failed_runs"]) == 1


def test_workflow_gate_accepts_required_successes():
    runs = [
        {"name": "ci", "status": "completed", "conclusion": "success"},
        {"name": "release-qualification", "status": "completed", "conclusion": "success"},
    ]
    gate = github_health.workflow_gate(runs, require_workflows=("ci", "release-qualification"))
    assert gate["ok"]


def test_dependabot_gate_blocks_configured_severities():
    alerts = [
        {
            "number": 7,
            "state": "open",
            "security_advisory": {"severity": "high"},
            "dependency": {"package": {"name": "cryptography", "ecosystem": "pip"}, "manifest_path": "pyproject.toml"},
            "html_url": "https://github.com/o/r/security/dependabot/7",
        },
        {
            "number": 8,
            "state": "open",
            "security_advisory": {"severity": "low"},
            "dependency": {"package": {"name": "pillow", "ecosystem": "pip"}, "manifest_path": "requirements.txt"},
        },
    ]
    gate = github_health.dependabot_gate(alerts, block_severities=("high", "critical"))
    assert not gate["ok"]
    assert gate["counts_by_severity"]["high"] == 1
    assert gate["counts_by_severity"]["low"] == 1
    assert [a["package"] for a in gate["blocking_alerts"]] == ["cryptography"]


def test_check_health_uses_gh_and_writes_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    calls: list[list[str]] = []

    monkeypatch.setattr(github_health, "find_gh", lambda explicit=None: Path("/bin/gh"))
    monkeypatch.setattr(github_health, "resolve_owner_repo", lambda repo, explicit=None, remote="origin": "owner/repo")
    monkeypatch.setattr(github_health, "current_branch", lambda repo: "dev")
    monkeypatch.setattr(github_health, "current_head", lambda repo: "abc123456789")

    def fake_run_gh(gh, args, *, cwd=None, timeout=60):
        calls.append(args)
        if args[:2] == ["run", "list"]:
            return github_health.GitHubCommandResult(tuple(args), 0, json.dumps([
                {"name": "ci", "workflowName": "ci", "status": "completed", "conclusion": "success", "headSha": "abc123456789", "url": "u1"},
                {"name": "release-qualification", "workflowName": "release-qualification", "status": "completed", "conclusion": "success", "headSha": "abc123456789", "url": "u2"},
            ]), "")
        if args and args[0] == "api":
            return github_health.GitHubCommandResult(tuple(args), 0, "[]", "")
        if args == ["--version"]:
            return github_health.GitHubCommandResult(tuple(args), 0, "gh version 2.95.0\n", "")
        if args[:2] == ["auth", "status"]:
            return github_health.GitHubCommandResult(tuple(args), 0, "Logged in\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(github_health, "run_gh", fake_run_gh)
    report = github_health.check_health(
        repo=repo,
        require_workflows=("ci", "release-qualification"),
        out_dir=out,
    )
    assert report["ok"]
    assert (out / "github-health-report.json").is_file()
    assert (out / "github-health-summary.md").read_text(encoding="utf-8").startswith("# GitHub Health Gate — PASS")
    assert any(args[:2] == ["run", "list"] for args in calls)
    assert any(args and args[0] == "api" for args in calls)


def test_cli_parser_exposes_github_health():
    names = {action.dest for action in cli.parser()._actions}
    assert "workflow" in names
    parsed = github_health.parser().parse_args(["check", "--repo", ".", "--require-workflow", "ci"])
    assert parsed.command == "check"
    assert parsed.require_workflow == ["ci"]
