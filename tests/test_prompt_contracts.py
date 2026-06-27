from __future__ import annotations

from pathlib import Path

from hermes_legion_commander import model_council, profile_harness, supervisor
from hermes_legion_commander.prompt_contracts import (
    host_side_evidence_boundary,
    quota_handoff_template,
    version_execution_contract,
)


def test_prompt_contract_contains_manual_run_invariants():
    text = version_execution_contract()
    assert "Version-boundary discipline" in text
    assert "quota" in text.lower()
    assert "HANDOFF:" in text
    assert "Phantom-diff" in text


def test_generic_dispatch_contract_has_quota_and_evidence_policy(tmp_path):
    payload = profile_harness.build_dispatch_contract(
        profile="legion-worker-a",
        mode="council",
        role="prototyper",
        native_runtime="codex",
        permission="workspace-write",
        workspace=tmp_path / "worktree",
        shared_context=tmp_path / "context",
        prompt_file=tmp_path / "prompt.md",
        output_file=tmp_path / "handoff.json",
        objective="Implement the bounded version.",
    )
    assert payload["quota_watermark"] == "80%"
    assert payload["stop_policy"] == "finish_active_version_then_handoff"
    assert payload["host_side_evidence_policy"].startswith("never_machine_award")


def test_worker_soul_mentions_clean_boundary_handoff(tmp_path):
    soul = profile_harness.generic_worker_soul("legion-worker-a", tmp_path)
    assert "Version-boundary discipline" in soul
    assert "HANDOFF:" in soul
    assert "Physical field tests" in soul


def test_supervisor_goal_contract_mentions_quota_policy():
    goal = supervisor.goal_contract_template()
    assert "Quota and handoff policy" in goal
    assert "finish active version" in goal.lower()


def test_version_prompt_embeds_manual_contract():
    prompt = model_council.version_implement_prompt(92, "### v92", "", "", "")
    assert "ROADMAP VERSION EXECUTION CONTRACT" in prompt
    assert "Host-side evidence boundary" in prompt
    assert "HANDOFF:" in prompt
