from __future__ import annotations

import json
from pathlib import Path

from hermes_legion_commander import profile_harness
from hermes_legion_commander import supervisor


def test_generic_worker_soul_is_role_and_runtime_neutral(tmp_path):
    soul = profile_harness.generic_worker_soul("legion-worker-a", tmp_path)
    assert "role-neutral harness operator" in soul
    assert "Codex CLI or Claude Code" in soul
    assert "Do not infer a role or native runtime from your profile name" in soul
    assert "read-only" in soul and "workspace-write" in soul


def test_assignment_plans_support_all_modes_and_runtime_swaps():
    for mode in ("council", "competition", "alternating"):
        plan = profile_harness.assignment_plan(mode)
        assert plan["mode"] == mode
        assert plan["profiles"] == ["legion-worker-a", "legion-worker-b"]
    competition = profile_harness.assignment_plan("competition")
    assert any(row.get("runtime") == "codex" for row in competition["assignments"])
    assert any(row.get("runtime") == "claude" for row in competition["assignments"])


def test_dispatch_contract_can_assign_claude_to_worker_a(tmp_path):
    payload = profile_harness.build_dispatch_contract(
        profile="legion-worker-a",
        mode="council",
        role="security_assurance",
        native_runtime="claude",
        permission="read-only",
        workspace=tmp_path / "worktree",
        shared_context=tmp_path / "context",
        prompt_file=tmp_path / "prompt.md",
        output_file=tmp_path / "handoff.json",
        objective="Review the bounded change.",
    )
    assert payload["profile"] == "legion-worker-a"
    assert payload["native_runtime"] == "claude"
    assert payload["role"] == "security_assurance"


def test_dispatch_contract_can_assign_codex_to_worker_b(tmp_path):
    payload = profile_harness.build_dispatch_contract(
        profile="legion-worker-b",
        mode="alternating",
        role="prototyper",
        native_runtime="codex",
        permission="workspace-write",
        workspace=tmp_path / "worktree",
        shared_context=tmp_path / "context",
        prompt_file=tmp_path / "prompt.md",
        output_file=tmp_path / "handoff.json",
        objective="Implement the bounded change.",
    )
    assert payload["profile"] == "legion-worker-b"
    assert payload["native_runtime"] == "codex"


def test_supervisor_setup_defaults_include_two_workers():
    parser = supervisor.parser()
    args = parser.parse_args(["setup"])
    assert args.worker_profile_a == "legion-worker-a"
    assert args.worker_profile_b == "legion-worker-b"
    assert not args.supervisor_only


def test_supervisor_parser_exposes_generic_worker_commands():
    parser = supervisor.parser()
    for command in (
        ["show-worker-soul"],
        ["show-worker-skill"],
        ["show-dispatch-contract"],
        ["assignment-plan", "--mode", "competition"],
    ):
        args = parser.parse_args(command)
        assert args.action == command[0]


def test_write_dispatch_contract(tmp_path):
    payload = profile_harness.build_dispatch_contract(
        profile="legion-worker-a",
        mode="competition",
        role="judge",
        native_runtime="claude",
        permission="read-only",
        workspace=tmp_path / "candidate",
        shared_context=tmp_path / "context",
        prompt_file=tmp_path / "prompt.md",
        output_file=tmp_path / "handoff.json",
        objective="Judge the candidate against the rubric.",
    )
    path = profile_harness.write_dispatch_contract(tmp_path / "state", payload)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["dispatch_id"] == payload["dispatch_id"]
