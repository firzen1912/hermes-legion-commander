from __future__ import annotations

from pathlib import Path

from hermes_legion_commander.pr_workflow import (
    actor_from_worker,
    branch_name,
    build_pr_body,
    slugify,
)


def test_branch_name_uses_requested_legion_commander_convention() -> None:
    assert branch_name(actor="codex", mode="competitive", slug="target repo v101", stamp="20260627-120000") == (
        "legion-commander-codex-competitive/target-repo-v101-20260627-120000"
    )
    assert branch_name(actor="claude", mode="alternating", slug="repo/feature", stamp="run") == (
        "legion-commander-claude-alternating/repo-feature-run"
    )
    assert branch_name(actor="commander", mode="collaborating", slug="docs cleanup", stamp="run") == (
        "legion-commander-commander-collaborating/docs-cleanup-run"
    )


def test_actor_from_worker_detects_codex_and_claude() -> None:
    assert actor_from_worker("gpt", "codex-cli", "openai") == "codex"
    assert actor_from_worker("worker-b", "claude-code", "anthropic") == "claude"
    assert actor_from_worker("reviewer_x", "custom", "local") == "reviewer_x"


def test_slugify_keeps_git_branch_safe_segments() -> None:
    assert slugify(" v1.0.1/prebeta hygiene!!! ") == "v1.0.1-prebeta-hygiene"
    assert slugify("///") == "work"


def test_pr_body_is_concise_but_has_review_details() -> None:
    body = build_pr_body(
        mode="competitive",
        branch="legion-commander-codex-competitive/target-repo-v101-run",
        base_branch="dev",
        run_id="v101-run",
        summary="Implements the prebeta gate.",
        validation="- tests pass",
        artifacts=["state/result.json", "state/pull-request/pull-request.json"],
        extra={"range": "v101"},
    )
    assert "## Summary" in body
    assert "Implements the prebeta gate." in body
    assert "legion-commander-codex-competitive/target-repo-v101-run" in body
    assert "- tests pass" in body
    assert "state/result.json" in body
    assert "```json" in body
