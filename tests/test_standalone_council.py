from pathlib import Path
import json
import subprocess
import sys
import tempfile

from hermes_legion_commander import model_council as mod

def test_render_uses_direct_codex_cli_and_shared_context():
    agent = mod.Agent(
        "gpt", "prototype", "codex-cli", "openai-codex",
        ("codex", "exec", "--add-dir", "{context_dir}", "--output-last-message", "{output_file}", "-"),
        60, "", "medium", "stdin", "codex-jsonl", ("edit-files",),
    )
    command = mod.render(agent, "hello")
    assert command[0] == "codex"
    assert "shared-context" in command
    assert "last-message.txt" in command

def test_quota_detection():
    assert mod.is_quota_error("", "429 quota exceeded", 1)


def test_campaign_assignments_are_staggered():
    rows = mod.campaign_assignments(52, 54)
    assert rows[0] == {"claude_review": 52, "gpt_implement": 53, "codex_research": 54}
    assert rows[-1] == {"claude_review": 54, "gpt_implement": None, "codex_research": None}


def test_mutating_role_prompts_authorize_bounded_worktree_changes():
    literature = mod.version_literature_prompt(51, "### v51", "research", "library")
    prototype = mod.version_implement_prompt(51, "### v51", "research", "literature", "library")
    polish = mod.version_code_polish_prompt(51, "### v51", "literature", "prototype")
    security = mod.version_security_assurance_prompt(51, "### v51", "polish")
    for prompt in (literature, prototype, polish, security):
        assert "current isolated Git worktree" in prompt
        assert "add, modify, rename, or remove repository files" in prompt
        assert "Do not merge, push, deploy" in prompt


def test_extract_version_section_is_bounded():
    roadmap = "# Intro\n## v52 Review\na\n## v53 Build\nb\n## v54 Research\nc\n"
    assert "a" in mod.extract_version_section(roadmap, 52)
    assert "v53" not in mod.extract_version_section(roadmap, 52)


def test_bootstrap_roadmap_template_starts_at_001():
    text = mod.roadmap_template("Demo")
    assert "## v0.0.1" in text
    assert "## v0.0.2" not in text
    assert "Researcher" in text
    assert "Security assurance" in text


def test_role_agent_is_configurable(tmp_path):
    # Minimal object construction avoids coupling this unit test to TOML paths.
    cfg = object.__new__(mod.Config)
    object.__setattr__(cfg, "roles", {"researcher": "gpt"})
    assert mod.role_agent(cfg, "researcher") == "gpt"

def test_discovers_all_roadmap_markdown_files(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "field-roadmap.md").write_text("# v51 Field\n", encoding="utf-8")
    (docs / "ROADMAP-security.MD").write_text("# v52 Security\n", encoding="utf-8")
    (docs / "notes.md").write_text("ignore", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/missing-roadmap.md")})()
    files = mod.discover_roadmap_files(cfg)
    assert [p.name for p in files] == ["field-roadmap.md", "ROADMAP-security.MD"]
    primary, text, sources = mod.roadmap_context(cfg)
    assert primary == files[0]
    assert "v51 Field" in text and "v52 Security" in text
    assert len(sources) == 2


def test_explicit_roadmap_path_selects_specific_docs_file(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "roadmap.md").write_text("# v1 Canonical\n", encoding="utf-8")
    (docs / "target-roadmap.md").write_text("# v1 Target project\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/target-roadmap.md")})()
    files = mod.discover_roadmap_files(cfg)
    assert files[0].name == "target-roadmap.md"
    primary, _, _ = mod.roadmap_context(cfg)
    assert primary == files[0]


def test_explicit_roadmap_outside_docs_is_authoritative_primary(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "roadmap.md").write_text("# v1 Canonical\n", encoding="utf-8")
    plans = repo / "plans"
    plans.mkdir()
    sprint = plans / "sprint-42.md"
    sprint.write_text("# v1 Sprint\n", encoding="utf-8")
    # A file outside docs/ and not named *roadmap* must still win when explicit.
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("plans/sprint-42.md")})()
    files = mod.discover_roadmap_files(cfg)
    assert files[0] == sprint.resolve()
    # Existing docs roadmap is still included as secondary context.
    assert (docs / "roadmap.md").resolve() in files
    primary, _, _ = mod.roadmap_context(cfg)
    assert primary == sprint.resolve()


def test_explicit_absolute_roadmap_path(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "roadmap.md").write_text("# v1 Canonical\n", encoding="utf-8")
    target = docs / "release-roadmap.md"
    target.write_text("# v1 Release\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": target.resolve()})()
    files = mod.discover_roadmap_files(cfg)
    assert files[0] == target.resolve()


def test_bootstrap_prefers_existing_discovered_roadmap(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    existing = docs / "product-roadmap.md"
    existing.write_text("# Existing roadmap\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/roadmap.md")})()
    path, created = mod.ensure_bootstrap_roadmap(cfg)
    assert path == existing.resolve()
    assert created is False
    assert not (docs / "roadmap.md").exists()


def test_default_roadmap_plan_reviewer_role(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "roadmap.md").write_text("# Roadmap\n\n## v51\nScope\n", encoding="utf-8")
    assert mod.DEFAULT_ROLES["roadmap_plan_reviewer"] == "gpt"


def test_research_prompt_is_constrained_by_plan_review():
    prompt = mod.roadmap_plan_review_prompt("## v54\nSLAM scope", version="54")
    assert "prioritized research" in prompt.lower()
    assert "do not perform the research" in prompt.lower()


def test_core_feature_and_iteration_filename_follow_version_heading():
    section = "# v54 — Cooperative Mapping and Recovery (0.54.0)\n\nScope"
    feature = mod.core_feature_from_section(section, 54)
    assert feature == "Cooperative Mapping and Recovery"
    assert mod.iteration_filename(54, feature) == "54-cooperative-mapping-and-recovery.md"




def test_core_feature_strips_release_alias_from_project_heading():
    section = "### v52 / 0.52.0 — Long-running GCS, companion, and relay runtimes\n"
    feature = mod.core_feature_from_section(section, 52)
    assert feature == "Long-running GCS, companion, and relay runtimes"
    assert mod.iteration_filename(52, feature) == "52-long-running-gcs-companion-and-relay-runtimes.md"

def test_semver_iteration_filename_for_bootstrap():
    section = "## v0.0.1 — Initial supervised prototype\n"
    feature = mod.core_feature_from_section(section, "0.0.1")
    assert feature == "Initial supervised prototype"
    assert mod.iteration_filename("0.0.1", feature) == "0.0.1-initial-supervised-prototype.md"


def test_write_iteration_document_creates_index_and_is_idempotent(tmp_path):
    checkout = tmp_path / "repo"
    cfg = type("Cfg", (), {"iterations_dir": Path("docs/iterations")})()
    content = "# v54 — Cooperative Mapping\n\n## Literature review\n\nEvidence."
    first = mod.write_iteration_document(cfg, checkout, 54, "Cooperative Mapping", content)
    second = mod.write_iteration_document(cfg, checkout, 54, "Cooperative Mapping", content + "\nUpdated.")
    assert first == second
    text = first.read_text(encoding="utf-8")
    assert text.count("HERMES-LEGION-COMMANDER ITERATION v54 START") == 1
    assert "Updated." in text
    index = checkout / "docs" / "iterations" / "README.md"
    assert "54-cooperative-mapping.md" in index.read_text(encoding="utf-8")


def test_existing_manual_iteration_is_preserved(tmp_path):
    checkout = tmp_path / "repo"
    iterations = checkout / "docs" / "iterations"
    iterations.mkdir(parents=True)
    existing = iterations / "54-existing-feature.md"
    existing.write_text("# v54 — Existing Feature\n\nManual history.\n", encoding="utf-8")
    cfg = type("Cfg", (), {"iterations_dir": Path("docs/iterations")})()
    result = mod.write_iteration_document(
        cfg, checkout, 54, "New Feature",
        "# v54 — New Feature\n\n## Literature review\n\nNew evidence.",
    )
    assert result == existing
    text = existing.read_text(encoding="utf-8")
    assert "Manual history." in text
    assert "New evidence." in text


def test_iteration_prompt_requires_literature_and_core_feature_sections():
    prompt = mod.iteration_record_prompt(
        54, "Cooperative Mapping", "# v54", "plan", "research", "literature", "implementation", "assurance"
    )
    assert "## Literature review" in prompt
    assert "## Core feature" in prompt
    assert "Never invent citations" in prompt


def test_path_reference_supports_external_literature_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "state" / "review.md"
    external.parent.mkdir()
    external.write_text("review", encoding="utf-8")
    assert mod.path_reference(external, repo) == str(external.resolve())
    assert mod.resolve_reference(str(external.resolve()), repo) == external.resolve()


def test_roadmap_preflight_is_local_only(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "field-roadmap.md"
    roadmap.write_text("# Product Roadmap\n\n## v1 — Bootstrap\nScope\n", encoding="utf-8")
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/roadmap.md")})()

    def forbidden(*args, **kwargs):
        raise AssertionError("preflight must not invoke a subprocess or model")

    monkeypatch.setattr(mod.subprocess, "run", forbidden)
    result = mod.roadmap_preflight(cfg, preview_chars=200, verbose=True)
    assert result["mode"] == "local-filesystem-only"
    assert result["worker_cli_invoked"] is False
    assert result["model_or_api_calls"] == 0
    assert result["primary_roadmap"] == str(roadmap.resolve())
    assert result["roadmaps"][0]["versions_detected"] == ["1"]
    assert "Product Roadmap" in result["roadmaps"][0]["preview"]
    assert len(result["roadmaps"][0]["sha256"]) == 64


def test_preflight_scopes_version_detection_to_implementation_section(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "field-roadmap.md"
    roadmap.write_text(
        "# Product Roadmap\n\n"
        "**Current baseline:** v0.50.0\n\n"
        "## Version-by-version implementation (v12-v100)\n\n"
        "### v12 / 0.12.0 - Bootstrap\nScope\n\n"
        "### v28a / 0.28.0 - Rig\nScope\n\n"
        "### v28b / 0.28.0 - Bench\nScope\n\n"
        "### v40.1 / 0.40.1 - Patch\nScope\n\n"
        "### v100 / 1.0.0 — Release\nScope\n\n"
        "## Appendix\nTarget is 1.0.\n",
        encoding="utf-8",
    )
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/roadmap.md")})()
    result = mod.roadmap_preflight(cfg, preview_chars=120, verbose=True)
    item = result["roadmaps"][0]
    assert item["roadmap_scope"] == "version-by-version-implementation"
    assert item["implementation_section_heading"] == "Version-by-version implementation (v12-v100)"
    assert item["versions_detected"] == ["12", "28a", "28b", "40.1", "100"]
    assert item["campaign_versions_detected"] == [12, 28, 40, 100]
    assert item["release_versions_detected"] == ["0.12.0", "0.28.0", "0.28.0", "0.40.1", "1.0.0"]
    assert "0.50" not in item["versions_detected"]
    assert "1.0" not in item["versions_detected"]
    assert item["preview"].startswith("## Version-by-version implementation")


def test_extract_integer_version_groups_lettered_variants():
    roadmap = (
        "## Version-by-version implementation (v12-v100)\n\n"
        "### v28a / 0.28.0 - Rig\nA\n\n"
        "### v28b / 0.28.0 - Bench\nB\n\n"
        "### v29 / 0.29.0 - Next\nC\n"
    )
    section = mod.extract_version_section(roadmap, 28)
    assert "v28a" in section
    assert "v28b" in section
    assert "v29" not in section


def test_preflight_handles_crlf_and_only_direct_child_version_headings(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "field-roadmap.md"
    content = (
        "# Product Roadmap\r\n\r\n"
        "**Current baseline:** v0.50.0\r\n\r\n"
        "## Version-by-version implementation (v12-v100)\r\n\r\n"
        "### v12 / 0.12.0 - Bootstrap\r\nScope\r\n\r\n"
        "#### v999 / 9.99.9 - Nested example, not a phase\r\nIgnore\r\n\r\n"
        "### v40.1 / 0.40.1 - Patch\r\nScope\r\n\r\n"
        "### v100 / 1.0.0 - Release\r\nScope\r\n\r\n"
        "## Appendix\r\nTarget is 1.0.\r\n"
    )
    roadmap.write_bytes(content.encode("utf-8"))
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/roadmap.md")})()

    result = mod.roadmap_preflight(cfg, preview_chars=160)
    item = result["roadmaps"][0]
    assert item["implementation_section_found"] is True
    assert item["implementation_section_heading"] == "Version-by-version implementation (v12-v100)"
    assert item["phase_sample"] == ["12", "40.1", "100"]
    assert item["special_versions_detected"] == ["40.1"]
    assert item["campaign_version_range"] == [12, 100]
    assert "document_headings" not in item
    assert "versions_detected" not in item
    assert "campaign_versions_detected" not in item
    assert "release_versions_detected" not in item
    assert item["preview"].startswith("## Version-by-version implementation")


def test_preflight_verbose_includes_release_aliases_and_document_headings(tmp_path):
    repo = tmp_path / "repo"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    roadmap = docs / "roadmap.md"
    roadmap.write_text(
        "# Roadmap\n\n## Version-by-version implementation (v1-v2)\n\n"
        "### v1 / 0.1.0 - Start\nA\n\n### v2 / 0.2.0 - Finish\nB\n",
        encoding="utf-8",
    )
    cfg = type("Cfg", (), {"repo": repo, "roadmap_path": Path("docs/roadmap.md")})()
    item = mod.roadmap_preflight(cfg, preview_chars=0, verbose=True)["roadmaps"][0]
    assert item["release_versions_detected"] == ["0.1.0", "0.2.0"]
    assert item["version_entries"][0]["version"] == "1"
    assert "Roadmap" in item["document_headings"]


def test_version_validation_requirements_are_roadmap_bounded():
    integration = mod.version_validation_requirements(
        53,
        "### v53 — Live simulator integration\nBenchmark SITL interoperability and fault recovery.",
        True,
    )
    assert integration["tests_required"] is True
    assert integration["experiments_required"] is True
    assert integration["experiment_deferred"] is False

    field_only = mod.version_validation_requirements(
        55,
        "### v55 — Physical field operation\nUse real hardware and a powered rover bench.",
        True,
    )
    assert field_only["tests_required"] is True
    assert field_only["experiments_required"] is False
    assert field_only["experiment_deferred"] is True

    research_only = mod.version_validation_requirements(
        54,
        "### v54 — Security research",
        False,
    )
    assert research_only["tests_required"] is False
    assert research_only["experiments_required"] is False


def test_validation_artifact_prompt_enforces_versioned_paths():
    requirements = mod.version_validation_requirements(
        53, "### v53 — Integration benchmark", True
    )
    prompt = mod.validation_artifact_prompt(
        53,
        "### v53 — Integration benchmark",
        requirements,
        ["src/example.py"],
        Path("tests"),
        Path("experiments"),
        Path("results/iterations"),
    )
    assert "test_v53_" in prompt
    assert "run_v53_" in prompt
    assert "results/iterations/v53" in prompt
    assert "no live actuation" in prompt.lower()


def test_run_version_validation_executes_tests_experiment_and_gathers_results(tmp_path):
    checkout = tmp_path / "repo"
    tests = checkout / "tests"
    experiments = checkout / "experiments"
    tests.mkdir(parents=True)
    experiments.mkdir(parents=True)

    (tests / "test_v53_feature.py").write_text(
        "def test_feature():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    (experiments / "run_v53_feature.py").write_text(
        "from pathlib import Path\n"
        "import json\n"
        "root = Path('results/iterations/v53')\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "(root / 'experiment.json').write_text(json.dumps({'ok': True}) + '\\n')\n"
        "(root / 'experiment.md').write_text('# Experiment\\n\\nPASS\\n')\n",
        encoding="utf-8",
    )

    cfg = type(
        "Cfg",
        (),
        {
            "tests_dir": Path("tests"),
            "experiments_dir": Path("experiments"),
            "results_dir": Path("results/iterations"),
            "version_test_command": (sys.executable, "-m", "pytest", "-q"),
            "version_experiment_command": (sys.executable,),
            "version_validation_timeout_seconds": 60,
        },
    )()
    requirements = mod.version_validation_requirements(
        53, "### v53 — Integration benchmark and evidence", True
    )
    run_dir = tmp_path / "state"
    result = mod.run_version_validation(
        cfg,
        checkout,
        run_dir,
        53,
        requirements,
        ["src/example.py"],
        False,
    )
    assert result["passed"] is True
    assert result["tests"]["execution"]["status"] == "passed"
    assert result["experiments"]["executions"][0]["status"] == "passed"
    assert "results/iterations/v53/experiment.json" in result["result_files"]
    assert (run_dir / "v53" / "07-validation" / "result.json").exists()


def test_write_version_result_and_iteration_validation_are_idempotent(tmp_path):
    checkout = tmp_path / "repo"
    cfg = type("Cfg", (), {"results_dir": Path("results/iterations")})()
    validation = {
        "version": 53,
        "status": "passed",
        "passed": True,
        "requirements": {},
        "changed_paths": ["src/example.py"],
        "tests": {
            "paths": ["tests/test_v53_feature.py"],
            "missing_required": False,
            "execution": {"status": "passed"},
        },
        "experiments": {
            "paths": ["experiments/run_v53_feature.py"],
            "missing_required": False,
            "executions": [{"status": "passed"}],
            "deferred": False,
        },
        "result_files": ["results/iterations/v53/experiment.json"],
    }
    json_path, markdown_path = mod.write_version_result_summary(
        cfg, checkout, 53, "Feature", validation, True
    )
    assert json.loads(json_path.read_text())["global_checks_passed"] is True
    assert "Focused test status" in markdown_path.read_text()

    iteration = checkout / "docs" / "iterations" / "53-feature.md"
    iteration.parent.mkdir(parents=True)
    iteration.write_text("# v53 — Feature\n", encoding="utf-8")
    mod.append_iteration_validation(
        iteration, 53, validation, validation["result_files"]
    )
    mod.append_iteration_validation(
        iteration, 53, validation, validation["result_files"]
    )
    content = iteration.read_text()
    assert content.count("HERMES-LEGION-COMMANDER VALIDATION v53 START") == 1
    assert "tests/test_v53_feature.py" in content


def test_dry_campaign_writes_iteration_and_result_records_for_every_version(tmp_path):
    repository = tmp_path / "target"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    docs = repository / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text(
        "# Roadmap\n\n"
        "## Version-by-version implementation (v52-v54)\n\n"
        "### v52 / 0.52.0 — Runtime integration\nIntegration and topology evidence.\n\n"
        "### v53 / 0.53.0 — Simulator interoperability\nSITL benchmark and fault recovery.\n\n"
        "### v54 / 0.54.0 — Security research\nReview secure transport.\n",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)

    agents = {
        name: mod.Agent(
            name,
            name,
            {"gpt": "codex-cli", "claude": "claude-code"}[name],
            name,
            ({"gpt": "codex", "claude": "claude"}[name], "--prompt", "{prompt}"),
            60,
            name,
            "medium",
            "argument",
            "text",
            (),
        )
        for name in ("gpt", "claude")
    }
    cfg = mod.Config(
        repo=repository,
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
        max_prompt_chars=120000,
        checks=((sys.executable, "-c", "print('ok')"),),
        agents=agents,
        lookback_days=30,
        max_findings=5,
        topics=("robotics",),
        pdf_dir=tmp_path / "pdfs",
        review_dir=tmp_path / "reviews",
        library_manifest=tmp_path / "manifest.jsonl",
        literature_reviewer="claude",
        max_pdf_chars=20000,
        default_budget="balanced",
        campaign_strategy="full",
        literature_validation="balanced",
        quota_retry_seconds=60,
        quota_max_retry_seconds=60,
        quota_wait=False,
        massive_files=1000,
        massive_lines=100000,
        roadmap_path=Path("docs/roadmap.md"),
        iterations_dir=Path("docs/iterations"),
        tests_dir=Path("tests"),
        experiments_dir=Path("experiments"),
        results_dir=Path("results/iterations"),
        version_test_command=(sys.executable, "-m", "pytest", "-q"),
        version_experiment_command=(sys.executable,),
        version_validation_timeout_seconds=60,
        roles=dict(mod.DEFAULT_ROLES),
    )
    run_dir = mod.run_campaign(cfg, 52, 54, True, run_id="dry-campaign", wait_for_quota=False)
    result = json.loads((run_dir / "result.json").read_text())
    assert set(result["version_results"]) == {"52", "53", "54"}
    assert len(result["iteration_documents"]) == 3
    worktree = Path(result["worktree"])
    for version in (52, 53, 54):
        assert list((worktree / "docs" / "iterations").glob(f"{version}-*.md"))
        assert (worktree / "results" / "iterations" / f"v{version}" / "campaign-result.json").exists()
    assert (run_dir / "campaign-summary.json").exists()
    assert (run_dir / "campaign-summary.md").exists()


def test_live_campaign_correction_stage_creates_and_executes_version_artifacts(tmp_path, monkeypatch):
    repository = tmp_path / "target-live"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    docs = repository / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text(
        "# Roadmap\n\n"
        "## Version-by-version implementation (v1-v1)\n\n"
        "### v1 / 0.1.0 — Runtime integration evidence\n"
        "Implement a host-safe integration benchmark and gather evidence.\n",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("target\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)

    agents = {
        name: mod.Agent(
            name,
            name,
            {"gpt": "codex-cli", "claude": "claude-code"}[name],
            name,
            ({"gpt": "codex", "claude": "claude"}[name], "--prompt", "{prompt}"),
            60,
            name,
            "medium",
            "argument",
            "text",
            (),
        )
        for name in ("gpt", "claude")
    }
    cfg = mod.Config(
        repo=repository,
        state_dir=tmp_path / "state-live",
        research_dir=tmp_path / "research-live",
        max_prompt_chars=120000,
        checks=((sys.executable, "-c", "print('global checks pass')"),),
        agents=agents,
        lookback_days=30,
        max_findings=5,
        topics=("robotics",),
        pdf_dir=tmp_path / "pdfs-live",
        review_dir=tmp_path / "reviews-live",
        library_manifest=tmp_path / "manifest-live.jsonl",
        literature_reviewer="claude",
        max_pdf_chars=20000,
        default_budget="balanced",
        campaign_strategy="full",
        literature_validation="balanced",
        quota_retry_seconds=60,
        quota_max_retry_seconds=60,
        quota_wait=False,
        massive_files=1000,
        massive_lines=100000,
        roadmap_path=Path("docs/roadmap.md"),
        iterations_dir=Path("docs/iterations"),
        tests_dir=Path("tests"),
        experiments_dir=Path("experiments"),
        results_dir=Path("results/iterations"),
        version_test_command=(sys.executable, "-m", "pytest", "-q"),
        version_experiment_command=(sys.executable,),
        version_validation_timeout_seconds=60,
        roles=dict(mod.DEFAULT_ROLES),
    )

    def fake_run_agent(config, agent_name, prompt, cwd, stage_dir, dry_run, wait_for_quota=None):
        stage_dir.mkdir(parents=True, exist_ok=True)
        if stage_dir.name == "02-literature-review":
            notes = cwd / "docs" / "research"
            notes.mkdir(parents=True, exist_ok=True)
            (notes / "v1-literature.md").write_text("# v1 literature\n", encoding="utf-8")
        if stage_dir.name == "03-prototype":
            source = cwd / "src"
            source.mkdir(parents=True, exist_ok=True)
            (source / "v1_runtime.py").write_text("READY = True\n", encoding="utf-8")
        if stage_dir.name == "04-code-polish":
            (cwd / "README.md").write_text("target\n\nPolished for v1.\n", encoding="utf-8")
        if stage_dir.name == "05-security-assurance":
            security = cwd / "docs" / "security"
            security.mkdir(parents=True, exist_ok=True)
            (security / "v1-assurance.md").write_text("# v1 assurance\n", encoding="utf-8")
        if stage_dir.name == "06-validation-artifacts":
            tests = cwd / "tests"
            experiments = cwd / "experiments"
            tests.mkdir(parents=True, exist_ok=True)
            experiments.mkdir(parents=True, exist_ok=True)
            (tests / "test_v1_runtime.py").write_text(
                "def test_runtime_contract():\n    assert True\n",
                encoding="utf-8",
            )
            (experiments / "run_v1_runtime.py").write_text(
                "from pathlib import Path\n"
                "import json\n"
                "out = Path('results/iterations/v1')\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(out / 'runtime-evidence.json').write_text(json.dumps({'passed': True}) + '\\n')\n"
                "(out / 'runtime-evidence.md').write_text('# Runtime evidence\\n\\nPASS\\n')\n",
                encoding="utf-8",
            )
        if stage_dir.name == "08-iteration-record":
            return (
                "# v1 — Runtime integration evidence\n\n"
                "## Roadmap alignment\n\nAligned.\n\n"
                "## Literature review\n\nReviewed.\n\n"
                "## Core feature\n\nRuntime evidence.\n\n"
                "## Items\n\n- [DONE] Validation.\n\n"
                "## Security and quality assurance\n\nReviewed.\n\n"
                "## Verification\n\nSupervisor captured.\n\n"
                "## Acceptance criteria — status\n\nPassed.\n\n"
                "## Honest scope\n\nHost safe.\n\n"
                "## Files changed\n\nRecorded.\n\n"
                "## Next\n\nReview.\n\n"
                "## References\n\nNone.\n"
            )
        return f"{agent_name} completed {stage_dir.name}"

    monkeypatch.setattr(mod, "run_agent", fake_run_agent)
    run_dir = mod.run_campaign(
        cfg, 1, 1, False, run_id="live-campaign", wait_for_quota=False
    )
    result = json.loads((run_dir / "result.json").read_text())
    assert result["checks_passed"] is True
    assert result["strategy"] == "full"
    mutations = result["stage_mutations"]["1"]
    assert "docs/research/v1-literature.md" in mutations
    assert "src/v1_runtime.py" in mutations
    assert "README.md" in mutations
    assert "docs/security/v1-assurance.md" in mutations
    version = result["version_results"]["1"]
    assert version["tests"]["execution"]["status"] == "passed"
    assert version["experiments"]["executions"][0]["status"] == "passed"
    worktree = Path(result["worktree"])
    assert (worktree / "tests" / "test_v1_runtime.py").exists()
    assert (worktree / "experiments" / "run_v1_runtime.py").exists()
    assert (worktree / "results" / "iterations" / "v1" / "runtime-evidence.json").exists()
    iteration = next((worktree / "docs" / "iterations").glob("1-*.md"))
    text = iteration.read_text()
    assert "Supervisor-captured version validation" in text
    assert "test_v1_runtime.py" in text


def test_example_config_uses_direct_native_clis():
    root = Path(__file__).resolve().parents[1]
    cfg = mod.load_config(root / "config" / "model_council.example.toml")
    assert set(cfg.agents) == {"gpt", "claude"}
    assert cfg.roles["roadmap_plan_reviewer"] == "gpt"
    assert cfg.roles["literature_reviewer"] == "claude"
    assert cfg.agents["gpt"].runtime == "codex-cli"
    assert cfg.agents["gpt"].command[0] == "codex"
    assert "exec" in cfg.agents["gpt"].command
    assert cfg.agents["claude"].runtime == "claude-code"
    assert cfg.agents["claude"].command[0] == "claude"
    assert "--input-format" in cfg.agents["claude"].command


def test_example_config_accepts_utf8_bom(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = (root / "config" / "model_council.example.toml").read_text(encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("\ufeff" + source, encoding="utf-8")
    loaded = mod.load_config(config)
    assert loaded.agents["gpt"].runtime == "codex-cli"


def test_model_council_accepts_custom_provider_runtime(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[roles]
roadmap_plan_reviewer = "gemini"
researcher = "gemini"
literature_reviewer = "claude"
prototyper = "gemini"
code_polisher = "claude"
security_assurance = "claude"
[agents.gemini]
runtime = "gemini-cli"
provider = "google"
model = "gemini-2.5-pro"
command = ["gemini", "-p", "{{prompt}}"]
output_format = "text"
[agents.gpt]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
pdf_dir = "{tmp_path / 'pdfs'}"
review_dir = "{tmp_path / 'reviews'}"
library_manifest = "{tmp_path / 'manifest.jsonl'}"
''')
    loaded = mod.load_config(config)
    # A custom-runtime agent from another provider loads and can fill roles.
    assert loaded.agents["gemini"].runtime == "gemini-cli"
    assert loaded.agents["gemini"].provider == "google"
    assert loaded.agents["gemini"].model == "gemini-2.5-pro"
    assert loaded.roles["researcher"] == "gemini"
    assert loaded.roles["literature_reviewer"] == "claude"


def test_model_council_same_provider_different_models_per_role(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[roles]
roadmap_plan_reviewer = "codex_fast"
researcher = "codex_fast"
literature_reviewer = "codex_deep"
prototyper = "codex_fast"
code_polisher = "codex_deep"
security_assurance = "codex_deep"
[agents.codex_fast]
runtime = "codex-cli"
provider = "openai"
model = "gpt-5-mini"
effort = "low"
command = ["codex", "exec", "-"]
[agents.codex_deep]
runtime = "codex-cli"
provider = "openai"
model = "gpt-5.5"
effort = "high"
command = ["codex", "exec", "-"]
[research]
topics = ["x"]
literature_reviewer = "codex_deep"
pdf_dir = "{tmp_path / 'pdfs'}"
review_dir = "{tmp_path / 'reviews'}"
library_manifest = "{tmp_path / 'manifest.jsonl'}"
''')
    loaded = mod.load_config(config)
    # Same provider/runtime, two different models, mapped to different roles.
    assert loaded.agents["codex_fast"].model == "gpt-5-mini"
    assert loaded.agents["codex_deep"].model == "gpt-5.5"
    assert loaded.roles["researcher"] == "codex_fast"
    assert loaded.roles["security_assurance"] == "codex_deep"


def test_model_council_role_pointing_at_missing_agent_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[roles]
roadmap_plan_reviewer = "ghost"
researcher = "codex_fast"
literature_reviewer = "codex_fast"
prototyper = "codex_fast"
code_polisher = "codex_fast"
security_assurance = "codex_fast"
[agents.codex_fast]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[research]
topics = ["x"]
pdf_dir = "{tmp_path / 'pdfs'}"
review_dir = "{tmp_path / 'reviews'}"
library_manifest = "{tmp_path / 'manifest.jsonl'}"
''')
    try:
        mod.load_config(config)
    except mod.CouncilError as exc:
        assert "must name a configured agent" in str(exc)
    else:
        raise AssertionError("role pointing at a missing agent was accepted")


def test_model_council_builtin_runtime_executable_mismatch_is_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    # codex-cli is a built-in runtime, so the command must launch 'codex'.
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
[agents.gpt]
runtime = "codex-cli"
provider = "openai"
command = ["not-codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
pdf_dir = "{tmp_path / 'pdfs'}"
review_dir = "{tmp_path / 'reviews'}"
library_manifest = "{tmp_path / 'manifest.jsonl'}"
''')
    try:
        mod.load_config(config)
    except mod.CouncilError as exc:
        assert "must launch codex" in str(exc)
    else:
        raise AssertionError("built-in runtime executable mismatch was accepted")
