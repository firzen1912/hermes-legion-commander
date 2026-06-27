from pathlib import Path
import tempfile

from hermes_legion_commander import checkpoint_competition as mod

def test_checkpoint_range():
    assert mod.checkpoint_range(10) == (1, 10)
    assert mod.checkpoint_range(60) == (51, 60)


def test_dangerous_intent():
    found = mod.dangerous_intent("Change MAVLink command authorization and deploy")
    assert "mavlink command" in found
    assert "authorization" in found
    assert "deploy" in found


def test_config_requires_two_direct_vendor_clis():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo = base / "repo"
        repo.mkdir()
        (repo / "docs").mkdir()
        (repo / "docs" / "field-deployability-roadmap.md").write_text("roadmap")
        config = base / "config.toml"
        config.write_text(f'''[competition]
repo = "{repo}"
state_dir = "{base / 'state'}"
checks = [["python", "-m", "pytest", "-q"]]
[agents.gpt]
runtime="codex-cli"
provider="openai"
model=""
role="research and prototype"
prompt_transport="stdin"
output_format="codex-jsonl"
command=["codex","exec","-"]
[agents.claude]
runtime="claude-code"
provider="anthropic"
model=""
role="literature and assurance"
prompt_transport="stdin"
output_format="claude-json"
command=["claude","-p","review"]
''')
        cfg = mod.load_config(config)
        assert set(cfg.agents) == {"gpt", "claude"}
        assert {a.command[0] for a in cfg.agents.values()} == {"codex", "claude"}

def test_checkpoint_discovers_multiple_roadmaps(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    preferred = docs / "field-deployability-roadmap.md"
    preferred.write_text("# v51 Field\n", encoding="utf-8")
    other = docs / "security-roadmap.md"
    other.write_text("# v52 Security\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "plan": preferred})()
    files = mod.discover_roadmap_files(cfg)
    assert files[0] == preferred.resolve()
    _, text, sources = mod.roadmap_context(cfg)
    assert "v51 Field" in text and "v52 Security" in text
    assert len(sources) == 2


def test_checkpoint_explicit_plan_outside_docs_is_primary(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "roadmap.md").write_text("# v1 Canonical\n", encoding="utf-8")
    plans = repo / "plans"
    plans.mkdir()
    sprint = plans / "sprint-42.md"
    sprint.write_text("# v1 Sprint\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "plan": sprint})()
    files = mod.discover_roadmap_files(cfg)
    assert files[0] == sprint.resolve()
    assert (docs / "roadmap.md").resolve() in files


def test_checkpoint_preflight_is_local_only(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "ROADMAP.MD"
    roadmap.write_text("# Roadmap\n\n## v2 — Harden\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "plan": docs / "missing-roadmap.md"})()

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not invoke a subprocess or model")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    result = mod.roadmap_preflight(cfg, preview_chars=100, verbose=True)
    assert result["mode"] == "local-filesystem-only"
    assert result["worker_cli_invoked"] is False
    assert result["model_or_api_calls"] == 0
    assert result["primary_roadmap"] == str(roadmap.resolve())
    assert result["roadmaps"][0]["versions_detected"] == ["2"]


def test_checkpoint_excerpt_uses_only_requested_implementation_versions(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "roadmap.md"
    roadmap.write_text(
        "# Roadmap\nCurrent baseline v0.50.0\n\n"
        "## Version-by-version implementation (v51-v100)\n\n"
        "### v51 / 0.51.0 - First\nA\n\n"
        "### v52 / 0.52.0 - Second\nB\n\n"
        "### v53 / 0.53.0 - Third\nC\n\n"
        "### v100 / 1.0.0 - Release\nZ\n",
        encoding="utf-8",
    )
    cfg = type("Cfg", (), {"repo": repo, "plan": roadmap})()
    excerpt = mod.roadmap_excerpt(cfg, mod.VersionRange(51, 52))
    assert "v51" in excerpt and "v52" in excerpt
    assert "### v53 " not in excerpt and "### v100 " not in excerpt
    assert "Current baseline" not in excerpt


def test_checkpoint_preflight_handles_crlf_implementation_spine(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "field-roadmap.md"
    roadmap.write_bytes(
        (
            "# Roadmap\r\n\r\n"
            "## Version-by-version implementation (v51-v52)\r\n\r\n"
            "### v51 / 0.51.0 - First\r\nA\r\n\r\n"
            "### v52 / 0.52.0 - Second\r\nB\r\n"
        ).encode("utf-8")
    )
    cfg = type("Cfg", (), {"repo": repo, "plan": roadmap})()
    item = mod.roadmap_preflight(cfg, preview_chars=100, verbose=True)["roadmaps"][0]
    assert item["implementation_section_found"] is True
    assert item["versions_detected"] == ["51", "52"]
    assert item["campaign_version_range"] == [51, 52]


def test_checkpoint_accepts_two_custom_provider_competitors(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    # Two competitors from different providers, neither using a built-in runtime
    # name for one side. The configured pair becomes cfg.competitors.
    config.write_text(f'''[competition]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[agents.gemini]
runtime = "gemini-cli"
provider = "google"
model = "gemini-2.5-pro"
command = ["gemini", "-p", "{{prompt}}"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
''')
    cfg = mod.load_config(config)
    assert set(cfg.competitors) == {"gemini", "claude"}
    assert cfg.agents["gemini"].runtime == "gemini-cli"
    assert cfg.agents["gemini"].model == "gemini-2.5-pro"
    assert mod.opponent_of("gemini", cfg.competitors) == "claude"
    assert mod.opponent_of("claude", cfg.competitors) == "gemini"


def test_checkpoint_requires_exactly_two_competitors(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
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
[agents.gemini]
runtime = "gemini-cli"
provider = "google"
command = ["gemini", "-p", "x"]
''')
    try:
        mod.load_config(config)
    except mod.CompetitionError as exc:
        assert "exactly two" in str(exc)
    else:
        raise AssertionError("three competitors were accepted")


def _write_role_matrix_config(tmp_path, extra: str = ""):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        "[competition]\n"
        f'repo = "{repo}"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        "[agents.gpt]\n"
        'runtime = "codex-cli"\nprovider = "openai"\nmodel = "gpt-base"\neffort = "medium"\n'
        'command = ["codex", "exec", "{model_args}", "{effort_args}", "-"]\n'
        "[agents.claude]\n"
        'runtime = "claude-code"\nprovider = "anthropic"\nmodel = "claude-base"\neffort = "medium"\n'
        'command = ["claude", "-p", "x", "{model_args}", "{effort_args}"]\n'
        + extra,
        encoding="utf-8",
    )
    return config


def test_role_matrix_assigns_both_competitors_every_role(tmp_path):
    config = _write_role_matrix_config(
        tmp_path,
        "[role_matrix.security_assurance.gpt]\nmodel = \"gpt-security\"\neffort = \"high\"\n"
        "[role_matrix.security_assurance.claude]\nmodel = \"claude-security\"\neffort = \"high\"\n",
    )
    cfg = mod.load_config(config)
    assert set(cfg.role_matrix) == set(mod.ALL_ROLES)
    assert all(set(cfg.role_matrix[role]) == {"gpt", "claude"} for role in mod.ALL_ROLES)
    assert mod.role_agent(cfg, "gpt", "security_assurance").model == "gpt-security"
    assert mod.role_agent(cfg, "claude", "security_assurance").model == "claude-security"


def test_worker_summary_exposes_role_specific_model_and_effort(tmp_path):
    config = _write_role_matrix_config(
        tmp_path,
        "[role_matrix.judge.gpt]\nmodel = \"judge-gpt\"\neffort = \"high\"\n"
        "[role_matrix.judge.claude]\nmodel = \"judge-claude\"\neffort = \"medium\"\n",
    )
    cfg = mod.load_config(config)
    summary = mod.worker_summary(cfg)
    assert summary["gpt"]["roles"]["judge"]["model"] == "judge-gpt"
    assert summary["claude"]["roles"]["judge"]["effort"] == "medium"


def test_dual_judge_scoring_collects_blockers():
    cfg = type("Cfg", (), {"weights": {"security": .4, "correctness": .4, "maintainability": .1, "performance": .1}})()
    judgements = {
        "gpt": {"candidates": {"gpt": {"security": 90, "correctness": 80, "maintainability": 70, "performance": 60, "blocker_codes": []}}},
        "claude": {"candidates": {"gpt": {"security": 80, "correctness": 90, "maintainability": 80, "performance": 70, "blocker_codes": ["authority_bypass"]}}},
    }
    score = mod._candidate_score_from_judges(cfg, "gpt", judgements)
    assert score["scores"]["security"] == 85
    assert score["blockers"] == ["authority_bypass"]


def test_cross_validation_roles_are_configurable(tmp_path):
    config = _write_role_matrix_config(
        tmp_path,
        "[role_matrix.cross_reviewer.gpt]\nmodel = \"gpt-review\"\neffort = \"high\"\n"
        "[role_matrix.cross_polisher.claude]\nmodel = \"claude-polish\"\neffort = \"high\"\n"
        "[role_matrix.final_verifier.gpt]\nmodel = \"gpt-final\"\neffort = \"high\"\n",
    )
    cfg = mod.load_config(config)
    assert "cross_reviewer" in cfg.role_matrix
    assert "cross_polisher" in cfg.role_matrix
    assert "final_verifier" in cfg.role_matrix
    assert mod.role_agent(cfg, "gpt", "cross_reviewer").model == "gpt-review"
    assert mod.role_agent(cfg, "claude", "cross_polisher").model == "claude-polish"
    assert mod.role_agent(cfg, "gpt", "final_verifier").model == "gpt-final"


def test_cross_review_normalization_marks_high_security_blocking():
    payload = mod._normalize_cross_review(
        {
            "verdict": "pass",
            "findings": [
                {
                    "severity": "HIGH",
                    "category": "security",
                    "code": "Auth Bypass!",
                    "file": "src/auth.py",
                    "evidence": "missing authorization check",
                    "recommended_fix": "enforce role check",
                    "confidence": "0.9",
                }
            ],
        },
        "gpt",
        "claude",
    )
    assert payload["verdict"] == "blocker"
    assert payload["findings"][0]["blocking"] is True
    assert payload["findings"][0]["code"] == "auth_bypass"
    assert payload["must_fix_before_judging"] == ["gpt-reviews-claude-001"]


def test_cross_validation_summary_groups_blockers(tmp_path):
    root = tmp_path / "run"
    review = {
        "reviewer": "claude",
        "target": "gpt",
        "verdict": "blocker",
        "findings": [
            {"id": "claude-reviews-gpt-001", "blocking": True, "severity": "critical", "category": "correctness"},
            {"id": "claude-reviews-gpt-002", "blocking": False, "severity": "low", "category": "documentation"},
        ],
    }
    mod.atomic_json(root / "cross-validation" / "claude-reviews-gpt.json", review)
    summary = mod.cross_validation_summary(root, ("gpt", "claude"))
    assert summary["review_count"] == 1
    assert summary["total_blockers"] == 1
    assert summary["by_target"]["gpt"]["findings"] == 2
    assert summary["by_target"]["gpt"]["blockers"][0]["id"] == "claude-reviews-gpt-001"


def test_judge_prompt_requires_cross_validation_consideration(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "roadmap.md"
    roadmap.write_text("# Roadmap\n\n## Version-by-version implementation\n\n### v1 - One\nA\n", encoding="utf-8")
    cfg = type(
        "Cfg",
        (),
        {
            "repo": repo,
            "plan": roadmap,
            "competitors": ("gpt", "claude"),
            "role_matrix": {
                "judge": {
                    "gpt": type("Profile", (), {"instructions": "judge"})(),
                    "claude": type("Profile", (), {"instructions": "judge"})(),
                }
            },
        },
    )()
    prompt = mod.judge_prompt(cfg, mod.VersionRange(1, 1), "gpt")
    assert "cross-validation" in prompt
    assert "cross_validation_considered" in prompt
