from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hermes_legion_commander import model_council as council
from hermes_legion_commander import supervisor
from hermes_legion_commander import worker_runtime


def test_supervisor_builds_alternating_command(tmp_path):
    command = supervisor.commander_command(
        "alternating",
        commander="hermes-legion-commander",
        config_path=tmp_path / "council.toml",
        repo=tmp_path / "repo",
        from_version=51,
        to_version=57,
        run_id="demo",
        dry_run=False,
        no_wait=False,
    )
    assert command[:2] == ["hermes-legion-commander", "council"]
    assert command[command.index("--strategy") + 1] == "alternating"
    assert "--no-wait" not in command


def test_sanitized_environment_removes_only_configured_overrides():
    agent = type("Agent", (), {"unset_env": ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")})()
    env = worker_runtime.sanitized_worker_environment(
        agent,
        {"OPENAI_BASE_URL": "bad", "ANTHROPIC_BASE_URL": "bad", "OPENAI_API_KEY": "keep"},
    )
    assert "OPENAI_BASE_URL" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["OPENAI_API_KEY"] == "keep"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_run_agent_fails_over_quota_to_other_worker(tmp_path):
    quota = tmp_path / "quota.py"
    quota.write_text(
        "import sys\nsys.stdin.buffer.read()\nsys.stderr.write('429 quota exceeded')\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    success = tmp_path / "success.py"
    success.write_text(
        "import json,sys\nsys.stdin.buffer.read()\nprint(json.dumps({'result':'fallback ok'}))\n",
        encoding="utf-8",
    )
    gpt = council.Agent(
        "gpt", "primary", "codex-cli", "openai", (sys.executable, str(quota)), 30,
        output_format="codex-jsonl",
    )
    claude = council.Agent(
        "claude", "fallback", "claude-code", "anthropic", (sys.executable, str(success)), 30,
        output_format="claude-json",
    )
    cfg = type(
        "Config",
        (),
        {
            "agents": {"gpt": gpt, "claude": claude},
            "quota_wait": False,
            "quota_retry_seconds": 60,
            "quota_max_retry_seconds": 60,
            "max_prompt_chars": 120000,
            "worker_failover": True,
            "failover_on": ("quota",),
        },
    )()
    repo = tmp_path / "repo"
    repo.mkdir()
    run = tmp_path / "run"
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo"}), encoding="utf-8")
    stage = run / "v51" / "00-roadmap-plan-review"
    output = council.run_agent(cfg, "gpt", "Review roadmap — UTF-8 ✓", repo, stage, False, False)
    assert output == "fallback ok"
    state = json.loads((stage / "state.json").read_text(encoding="utf-8"))
    assert state["requested_agent"] == "gpt"
    assert state["agent"] == "claude"
    assert state["failovers"][0]["reason"] == "quota"


def test_failure_classifier_distinguishes_entitlement_and_quota():
    assert worker_runtime.classify_worker_failure("", "429 rate limit", 1) == "quota"
    assert worker_runtime.classify_worker_failure("", "insufficient credits", 1) == "entitlement"


def test_supervisor_soul_is_harness_operator(tmp_path):
    soul = supervisor.supervisor_soul(tmp_path)
    assert "harness operator" in soul
    assert "PASS" in soul and "BLOCKED" in soul and "NEEDS_HUMAN" in soul
    assert "Do not edit implementation files" in soul
    assert "scoped fix contract" in soul


def test_supervisor_templates_include_goal_and_handoff_contracts():
    goal = supervisor.goal_contract_template()
    handoff = supervisor.handoff_schema()
    assert "## Acceptance criteria" in goal
    assert "## Forbidden actions" in goal
    assert '"status": "PASS | BLOCKED | NEEDS_HUMAN"' in handoff


def test_supervisor_parser_exposes_template_preview_commands():
    parser = supervisor.parser()
    for action in ("show-soul", "show-skill", "show-goal-contract", "show-handoff-schema"):
        args = parser.parse_args([action])
        assert args.action == action
