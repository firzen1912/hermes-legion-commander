"""Tests for subagent delegation metrics, prompt effectiveness, and OAuth auth modes."""
from hermes_legion_commander import prompt_metrics as pm
from hermes_legion_commander import prompt_contracts as pc
from hermes_legion_commander import token_cost as tc
from hermes_legion_commander import model_council as mc
from hermes_legion_commander import checkpoint_competition as cc


# --- subagent extraction -------------------------------------------------

def test_extract_subagents_explicit_count_and_tasks():
    out = """Did the work.

SUBAGENTS: 3
- formatter on gpt-5-mini
- test scaffolder on gpt-5-mini
- reference scanner on haiku
"""
    rep = pm.extract_subagent_report(out)
    assert rep["reported"] is True
    assert rep["spawned"] == 3
    assert rep["over_cap"] is False
    assert len(rep["tasks"]) == 3


def test_extract_subagents_none():
    rep = pm.extract_subagent_report("Work done.\n\nSUBAGENTS: 0\n")
    assert rep["reported"] is True
    assert rep["spawned"] == 0
    assert rep["over_cap"] is False


def test_extract_subagents_absent_block():
    rep = pm.extract_subagent_report("just some output with no subagent block")
    assert rep["reported"] is False
    assert rep["spawned"] == 0


def test_extract_subagents_over_cap_flagged():
    out = "SUBAGENTS: 7\n- a\n- b\n"
    rep = pm.extract_subagent_report(out, cap=5)
    assert rep["spawned"] == 7
    assert rep["over_cap"] is True


def test_extract_subagents_count_from_bullets_when_no_number():
    out = "SUBAGENTS:\n- one\n- two\n"
    rep = pm.extract_subagent_report(out)
    assert rep["spawned"] == 2


# --- prompt effectiveness ------------------------------------------------

def _event(stage, status, est_in, out_tokens, total, cost, spawned, over_cap=False, attempts=1, failovers=0):
    return {
        "stage": stage,
        "quality_signals": {"status": status},
        "prompt_metrics": {"estimated_tokens": est_in},
        "subagents": {"spawned": spawned, "over_cap": over_cap},
        "runtime_metadata": {
            "usage": {"output_tokens": out_tokens, "total_tokens": total, "cost_usd": cost},
            "attempts": attempts,
            "failovers": [{} for _ in range(failovers)],
        },
    }


def test_prompt_effectiveness_aggregates_per_role():
    events = [
        _event("v51/03-implementation", "PASS", 1000, 500, 1500, 0.02, 2),
        _event("v52/03-implementation", "FAIL", 1000, 1500, 2500, 0.05, 4, attempts=2, failovers=1),
        _event("v51/06-security-assurance", "PASS", 800, 200, 1000, 0.03, 0),
    ]
    report = pm.prompt_effectiveness(events)
    impl = report["by_role"]["implementation"]
    assert impl["stage_count"] == 2
    assert impl["pass_rate"] == 0.5
    assert impl["subagents_spawned"] == 6
    assert impl["retries"] == 1  # one stage had attempts=2
    assert impl["failovers"] == 1
    # output-per-input-token efficiency signal: (500+1500)/(1000+1000) = 1.0
    assert impl["output_per_input_token"] == 1.0
    sec = report["by_role"]["security-assurance"]
    assert sec["pass_rate"] == 1.0
    assert sec["avg_subagents"] == 0.0
    assert report["totals"]["stage_count"] == 3
    assert report["subagent_cap"] == 5


def test_prompt_effectiveness_cap_breach_counted():
    events = [_event("v1/03-implementation", "PASS", 100, 100, 200, 0.01, 9, over_cap=True)]
    report = pm.prompt_effectiveness(events)
    assert report["totals"]["subagent_cap_breaches"] == 1


def test_prompt_effectiveness_empty():
    report = pm.prompt_effectiveness([])
    assert report["totals"]["stage_count"] == 0
    assert report["by_role"] == {}


# --- contract injection --------------------------------------------------

def test_subagent_contract_has_cap_and_report_block():
    contract = pc.subagent_delegation_contract(cap=5)
    assert "at most 5" in contract
    assert "SUBAGENTS:" in contract
    assert "cheapest model" in contract


def test_council_implementation_prompt_includes_subagent_contract():
    prompt = mc.version_implement_prompt(51, "### v51 sensor fusion", "research", "literature", "library")
    assert "Subagent delegation for grunt work" in prompt
    assert "at most 5" in prompt


def test_checkpoint_candidate_prompt_includes_subagent_contract(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text("# Roadmap\n\n## Implementation\n### v51 — sensor fusion\nDo it.\n", encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text(f'''[competition]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[agents.gpt]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
''')
    cfg = cc.load_config(config)
    candidate_role = cc.CANDIDATE_ROLES[0]
    p = cc.role_prompt(cfg, cc.VersionRange(51, 51), candidate_role, "gpt")
    assert "Subagent delegation for grunt work" in p
    # A non-candidate (review) role must NOT carry the delegation contract.
    review = cc.role_prompt(cfg, cc.VersionRange(51, 51), "cross_reviewer", "gpt") if "cross_reviewer" in cfg.role_matrix else ""
    if review:
        assert "Subagent delegation for grunt work" not in review


# --- OAuth / auth modes --------------------------------------------------

def test_oauth_subscription_modes_for_both_providers():
    # Claude Code with no API key -> subscription/OAuth session.
    claude = tc.infer_auth_mode("claude-code", "anthropic", env={})
    assert claude["uses_subscription_or_oauth"] is True
    assert claude["mode"] == "claude_subscription_oauth_or_cli_session"
    # Explicit OAuth token is recognized as subscription auth.
    claude_oauth = tc.infer_auth_mode("claude-code", "anthropic", env={"CLAUDE_CODE_OAUTH_TOKEN": "x"})
    assert claude_oauth["uses_subscription_or_oauth"] is True
    # Codex with no API key -> ChatGPT OAuth/CLI session.
    codex = tc.infer_auth_mode("codex-cli", "openai", env={})
    assert codex["uses_subscription_or_oauth"] is True
    assert codex["mode"] == "chatgpt_oauth_or_cli_session"


def test_api_key_overrides_oauth_detection():
    claude = tc.infer_auth_mode("claude-code", "anthropic", env={"ANTHROPIC_API_KEY": "sk-x"})
    assert claude["uses_subscription_or_oauth"] is False
    assert claude["mode"] == "anthropic_api_key"
    codex = tc.infer_auth_mode("codex-cli", "openai", env={"OPENAI_API_KEY": "sk-x"})
    assert codex["uses_subscription_or_oauth"] is False


def test_env_sanitization_preserves_oauth_credentials():
    from hermes_legion_commander import worker_runtime as wr
    agent = type("A", (), {"unset_env": ("OPENAI_API_KEY",)})()
    base = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "CODEX_ACCESS_TOKEN": "tok2", "OPENAI_API_KEY": "k"}
    env = wr.sanitized_worker_environment(agent, base)
    # OAuth credential stores survive; only the explicitly-unset var is removed.
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok"
    assert env.get("CODEX_ACCESS_TOKEN") == "tok2"
    assert "OPENAI_API_KEY" not in env


# --- configurable subagent cap ------------------------------------------

def test_subagent_cap_configurable_in_council(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
subagent_cap = 8
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
    assert cfg.subagent_cap == 8
    prompt = mc.version_implement_prompt(1, "### v1 x", "", "", "", cfg.subagent_cap)
    assert "at most 8" in prompt
    assert "at most 5" not in prompt


def test_subagent_cap_default_is_five():
    contract = pc.subagent_delegation_contract()
    assert "at most 5" in contract


def test_subagent_cap_configurable_in_checkpoint(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n### v1 — x\nDo it.\n", encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text(f'''[competition]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
subagent_cap = 3
[agents.gpt]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
''')
    cfg = cc.load_config(config)
    assert cfg.subagent_cap == 3
    p = cc.role_prompt(cfg, cc.VersionRange(1, 1), cc.CANDIDATE_ROLES[0], "gpt")
    assert "at most 3" in p


def test_prompt_effectiveness_reflects_configured_cap_from_events():
    events = [{"stage": "v1/03-implementation", "quality_signals": {"status": "PASS"},
              "prompt_metrics": {"estimated_tokens": 100},
              "subagents": {"spawned": 6, "over_cap": False, "cap": 8},
              "runtime_metadata": {"usage": {"output_tokens": 50, "total_tokens": 150}}}]
    report = pm.prompt_effectiveness(events)
    assert report["subagent_cap"] == 8


def test_cli_routes_alternating_and_legacy_aliases(tmp_path, capsys):
    from hermes_legion_commander import cli
    # alternating routes to rapid-alternate
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n### v1 — x\nDo it.\n### v2 — y\nDo it.\n", encoding="utf-8")
    import subprocess
    for args in (["git", "init"], ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-m", "i"]):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
roadmap_path = "docs/roadmap.md"
checks = [["true"]]
[roles]
roadmap_plan_reviewer = "codex"
researcher = "codex"
literature_reviewer = "claude"
prototyper = "codex"
code_polisher = "claude"
security_assurance = "claude"
[agents.codex]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
literature_reviewer = "claude"
''')
    rc = cli.main(["alternating", "--config", str(config), "--version", "1",
                   "--worker", "codex", "--to-version", "2", "--dry-run", "--run-id", "clitest"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"mode": "rapid-alternate"' in out
