"""Tests for the /goal stop-condition primitive and the adversarial evaluator stance."""
from pathlib import Path

from hermes_legion_commander import stop_condition as sc
from hermes_legion_commander import model_council as mc
from hermes_legion_commander import checkpoint_competition as cc


def test_run_deterministic_checks_pass_and_fail(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",), ("false",)))
    assert len(results) == 2
    assert results[0].passed is True and results[0].returncode == 0
    assert results[1].passed is False and results[1].returncode == 1
    assert sc.deterministic_all_passed(results) is False


def test_run_deterministic_checks_all_pass(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",),))
    assert sc.deterministic_all_passed(results) is True


def test_run_deterministic_checks_missing_executable_is_failed_not_raised(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("definitely-not-a-real-binary-xyz",),))
    assert len(results) == 1
    assert results[0].passed is False
    assert "could not run" in results[0].output


def test_deterministic_all_passed_empty_is_false():
    assert sc.deterministic_all_passed(()) is False


def test_stop_condition_prompt_includes_condition_and_evidence(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",), ("false",)))
    prompt = sc.stop_condition_prompt("all auth tests pass", results)
    assert "all auth tests pass" in prompt
    assert "FRESH evaluator" in prompt
    assert "Default to NOT met" in prompt
    assert "PASSED" in prompt and "FAILED" in prompt
    assert '"met"' in prompt


def test_parse_verdict_met_requires_model_yes_and_checks_green(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",),))
    verdict = sc.parse_stop_verdict('{"met": true, "reasons": ["all green"], "unmet": []}', results)
    assert verdict["met"] is True
    assert verdict["model_met"] is True
    assert verdict["deterministic_all_passed"] is True
    assert verdict["reasons"] == ["all green"]


def test_parse_verdict_failed_check_vetoes_model_yes(tmp_path):
    # The model claims met, but a deterministic check failed -> floor forces not met.
    results = sc.run_deterministic_checks(tmp_path, (("false",),))
    verdict = sc.parse_stop_verdict('{"met": true, "reasons": ["looks done"], "unmet": []}', results)
    assert verdict["met"] is False
    assert verdict["model_met"] is True
    assert verdict["deterministic_all_passed"] is False
    assert any("deterministic check failed" in u for u in verdict["unmet"])


def test_parse_verdict_model_no_stays_not_met(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",),))
    verdict = sc.parse_stop_verdict('{"met": false, "reasons": [], "unmet": ["auth endpoint missing"]}', results)
    assert verdict["met"] is False
    assert verdict["unmet"] == ["auth endpoint missing"]


def test_parse_verdict_unparseable_output_is_not_met(tmp_path):
    results = sc.run_deterministic_checks(tmp_path, (("true",),))
    verdict = sc.parse_stop_verdict("the model rambled without json", results)
    assert verdict["met"] is False
    assert verdict["verdict_parsed"] is False


def test_council_evaluate_goal_dry_run(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    docs = repo / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
checks = [["true"], ["false"]]
[roles]
roadmap_plan_reviewer = "gen"
researcher = "gen"
literature_reviewer = "judge"
prototyper = "gen"
code_polisher = "judge"
security_assurance = "judge"
[agents.gen]
runtime = "codex-cli"
provider = "openai"
model = "gpt-5-mini"
command = ["codex", "exec", "-"]
[agents.judge]
runtime = "claude-code"
provider = "anthropic"
model = "claude-opus-4-8"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
literature_reviewer = "judge"
''')
    cfg = mc.load_config(config)
    out = mc.evaluate_goal(cfg, "all tests pass", dry_run=True)
    # Default judge is the security_assurance agent (the evaluator), not the generator.
    assert out["judge"] == "judge"
    assert out["judge_model"] == "claude-opus-4-8"
    assert out["deterministic_all_passed"] is False
    assert out["dry_run"] is True
    # An explicit --judge override is honored.
    out2 = mc.evaluate_goal(cfg, "all tests pass", judge_agent="gen", dry_run=True)
    assert out2["judge"] == "gen"


def test_council_evaluate_goal_rejects_unknown_judge(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
checks = [["true"]]
[roles]
roadmap_plan_reviewer = "gen"
researcher = "gen"
literature_reviewer = "judge"
prototyper = "gen"
code_polisher = "judge"
security_assurance = "judge"
[agents.gen]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.judge]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
literature_reviewer = "judge"
''')
    cfg = mc.load_config(config)
    try:
        mc.evaluate_goal(cfg, "x", judge_agent="ghost", dry_run=True)
    except mc.CouncilError as exc:
        assert "must name a configured agent" in str(exc)
    else:
        raise AssertionError("unknown judge was accepted")


def test_council_evaluator_prompt_has_adversarial_stance():
    prompt = mc.version_security_assurance_prompt(51, "### v51 sensor fusion", "polish summary")
    assert "ADVERSARIAL EVALUATOR STANCE" in prompt
    assert "BROKEN until" in prompt
    assert "Do not praise" in prompt
    assert "Verify by acting" in prompt


def test_checkpoint_judge_prompt_has_adversarial_stance(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    cfg = type(
        "Cfg",
        (),
        {
            "repo": repo,
            "competitors": ("gpt", "claude"),
            "role_matrix": {"judge": {
                "gpt": type("P", (), {"instructions": "judge"})(),
                "claude": type("P", (), {"instructions": "judge"})(),
            }},
        },
    )()
    prompt = cc.judge_prompt(cfg, cc.VersionRange(51, 51), "gpt")
    assert "ADVERSARIAL EVALUATOR STANCE" in prompt
    assert "BROKEN until" in prompt


def test_council_evaluate_goal_checkout_override(tmp_path):
    # The campaign passes its worktree as `checkout`; checks/judge must target it.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    worktree = tmp_path / "wt"
    (worktree / ".git").mkdir(parents=True)
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
checks = [["true"]]
[roles]
roadmap_plan_reviewer = "gen"
researcher = "gen"
literature_reviewer = "judge"
prototyper = "gen"
code_polisher = "judge"
security_assurance = "judge"
[agents.gen]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.judge]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
literature_reviewer = "judge"
''')
    cfg = mc.load_config(config)
    out = mc.evaluate_goal(cfg, "all tests pass", dry_run=True, checkout=worktree)
    assert out["dry_run"] is True
    assert out["deterministic_all_passed"] is True

