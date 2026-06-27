from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hermes_legion_commander import anchored_truth
from hermes_legion_commander import worker_runtime as runtime


def init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)


def test_refresh_anchored_truth_extracts_anchor_sources_and_repo_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "AGENTS.md").write_text("# Agent Rules\n\n- Must audit before add.\n- Never bypass safety veto.\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "beta-release-roadmap.md").write_text(
        "# Beta Roadmap\n\n## Non-Negotiable Boundaries\n\n- Beta is evidence-gated.\n- Do not claim BVLOS or unattended operation.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

    monkeypatch.setattr(anchored_truth, "collect_github_state", lambda repo, repo_state, out_dir: {"available": False, "error": "test"})
    report = anchored_truth.refresh_anchored_truth(tmp_path / "ctx", repo, task_prompt="implement v1.0.1")

    assert report["repo_state"]["available"]
    assert report["summary"]["repo_dirty"] is False
    assert any(row["path"] == "AGENTS.md" and row["exists"] for row in report["anchor_sources"])
    assert any("BVLOS" in item["line"] for item in report["summary"]["boundary_lines"])
    assert (tmp_path / "ctx" / "ANCHORED_TRUTH.md").is_file()
    assert (tmp_path / "ctx" / "anchored-truth" / "anchored-truth.json").is_file()


def test_build_prompt_injects_anchored_truth_before_task(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_git_repo(repo)
    (repo / "AGENTS.md").write_text("# Agent Rules\n\n- Never bypass the safety veto.\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "beta-release-roadmap.md").write_text("# Beta\n\n- Promotion status: BLOCKED.\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

    monkeypatch.setattr(anchored_truth, "collect_github_state", lambda repo, repo_state, out_dir: {"available": False, "error": "test"})
    # worker_runtime imported refresh_anchored_truth directly; patch that binding too.
    original = anchored_truth.refresh_anchored_truth
    monkeypatch.setattr(runtime, "refresh_anchored_truth", original)

    ctx = tmp_path / "context"
    prompt = runtime.build_prompt_with_shared_context("Implement roadmap version 101", ctx, repo, 120000)

    assert "## ANCHORED_TRUTH.md" in prompt
    assert "# Anchored Truth Preflight" in prompt
    assert "Promotion status: BLOCKED" in prompt
    assert prompt.index("# Anchored Truth Preflight") < prompt.index("# CURRENT STAGE TASK")
    summary = json.loads((ctx / "anchored-truth-summary.json").read_text(encoding="utf-8"))
    assert summary["repo_branch"] in {"master", "main"}
