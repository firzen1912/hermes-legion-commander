from __future__ import annotations

import json
from pathlib import Path

from hermes_legion_commander import routing_context
from hermes_legion_commander.routing_context import (
    classify_task,
    model_roster,
    refresh_routing_context,
    route_plan,
    summarize_routing_policy,
    train_policy,
)
from hermes_legion_commander.worker_runtime import build_prompt_with_shared_context


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "branch", "dev")
    _git(repo, "branch", "-M", "dev")
    return repo


def test_classify_task_escalates_security_keywords() -> None:
    task = classify_task("Patch Dependabot vulnerability in security key signing", ["hiveas/security/keyring.py"])
    assert task["primary"] == "security"
    assert task["scores"]["security"] >= 3


def test_dependency_manifest_change_escalates_to_competing(tmp_path: Path) -> None:
    for fname in ("pyproject.toml", "requirements.txt", "constraints-dev.txt", "package.json", "Cargo.lock"):
        task = classify_task("Update project metadata", [fname])
        assert task["primary"] == "dependency", fname

    plan = route_plan(
        repo=_make_repo(tmp_path),
        context_dir=tmp_path / "ctx",
        task_prompt="Bump a pinned dependency",
        base_ref="dev",
    )
    assert plan["recommended_mode"] == "competing"
    assert "final-verify" in plan["required_checks"]


def test_model_pool_is_local_claude_and_codex_only(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    context = repo / "shared-context"
    context.mkdir()
    plan = route_plan(repo=repo, context_dir=context, task_prompt="Implement the next item", base_ref="dev")

    assert set(plan["model_pool"]) == {"claude", "codex"}
    assert set(plan["worker_pool"]) == {"claude", "codex"}
    assert "nous" not in plan["model_pool"]
    assert set(plan["runtime_health"]) == {"claude", "codex"}
    assert plan["provider_health"] == {}
    assert "No Nous Portal" in plan["provider_policy"]
    for role in plan["roles"]:
        assert role["preferred"] in {"claude", "codex"}
        assert role["fallback"] in {"claude", "codex"}


def test_dirty_status_preserves_first_path_character(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    context = repo / "shared-context"
    context.mkdir()
    (repo / "README.md").write_text("# Repo\n\nDirty\n", encoding="utf-8")

    plan = route_plan(repo=repo, context_dir=context, task_prompt="Document update", base_ref="dev")

    assert "README.md" in plan["changed_files"]
    assert "EADME.md" not in plan["changed_files"]


def test_model_roster_default_does_not_probe_auth(monkeypatch, tmp_path: Path) -> None:
    def fail_run(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("auth subprocess should not run without check_auth=True")

    monkeypatch.setattr(routing_context.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(routing_context.subprocess, "run", fail_run)
    roster = model_roster(tmp_path, tmp_path / "ctx", check_auth=False, env={})

    assert roster["pool"]["claude"]["available"] is True
    assert roster["pool"]["codex"]["available"] is True
    assert roster["runtime_health"]["claude"]["authenticated"] is None
    assert roster["runtime_health"]["codex"]["authenticated"] is None


def test_refresh_routing_context_writes_prompt_context_without_provider_gateway(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    context = repo / "shared-context"
    context.mkdir()
    plan = refresh_routing_context(context, repo, task_prompt="Fix CI workflow and Dependabot vulnerability", base_ref="dev")

    assert plan["recommended_mode"] == "competing"
    assert "github-health" in plan["required_checks"]
    assert "OpenAI-compatible gateway" in plan["provider_policy"]
    assert (context / "ROUTING_CONTEXT.md").is_file()
    assert (context / "routing-context" / "model-roster.json").is_file()
    assert (context / "routing-context" / "worker-roster.json").is_file()
    assert (context / "routing-context" / "runtime-health.json").is_file()
    report = json.loads((context / "routing-context" / "routing-context-report.json").read_text(encoding="utf-8"))
    assert report["task_classification"]["primary"] in {"security", "repo_workflow"}
    assert report["provider_health"] == {}
    assert "worker_health" in report


def test_worker_prompt_includes_routing_context(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    context = repo / "shared-context"
    context.mkdir()
    (context / "CONTEXT.md").write_text("canonical context", encoding="utf-8")

    prompt = build_prompt_with_shared_context(
        "Implement a safe security patch",
        context,
        repo,
        max_prompt_chars=20000,
        include_git_snapshot=False,
    )

    assert "## ROUTING_CONTEXT.md" in prompt
    assert "Routing Context" in prompt
    assert "does not use Nous Portal" in prompt
    assert "# CURRENT STAGE TASK" in prompt


def test_summarize_routing_policy_reports_telemetry_without_training_claim(tmp_path: Path) -> None:
    context = tmp_path / "shared-context"
    context.mkdir()
    (context / "learning-ledger.jsonl").write_text(
        '{"provider":"claude","status":"success","quality":0.9}\n'
        '{"provider":"codex","status":"failed","quality":0.2}\n',
        encoding="utf-8",
    )

    policy = summarize_routing_policy(context)
    assert policy["telemetry"]["runtimes"]["claude"]["success_rate"] == 1.0
    assert policy["telemetry"]["workers"]["codex"]["success_rate"] == 0.0
    assert policy["kind"] == "deterministic-rules-with-telemetry-summary"
    assert (context / "routing-context" / "routing-policy-summary.json").is_file()
    assert train_policy(context)["routing_rules"] == policy["routing_rules"]
