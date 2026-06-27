from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from hermes_legion_commander import worker_runtime as runtime


class Agent:
    def __init__(self, name: str, runtime_name: str, output_format: str):
        self.name = name
        self.role = name
        self.runtime = runtime_name
        self.provider = name
        self.model = ""
        self.effort = "medium"
        self.output_format = output_format
        self.prompt_transport = "stdin"
        self.command = (runtime.RUNTIME_EXECUTABLES[runtime_name], "{context_dir}", "{output_file}")


def test_codex_jsonl_normalization_uses_last_message_file(tmp_path):
    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    output = tmp_path / "last.txt"
    output.write_text("Final Codex result\n", encoding="utf-8")
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    text, metadata = runtime.normalize_worker_output(agent, stdout, "", 0, output)
    assert text == "Final Codex result"
    assert metadata["session_id"] == "thread-1"


def test_claude_json_normalization_rejects_semantic_error(tmp_path):
    agent = Agent("claude", "claude-code", "claude-json")
    payload = json.dumps({"is_error": True, "result": "billing failed"})
    try:
        runtime.normalize_worker_output(agent, payload, "", 0, tmp_path / "none")
    except RuntimeError as exc:
        assert "reported an error" in str(exc)
    else:
        raise AssertionError("semantic Claude error was accepted")


def test_shared_context_is_common_and_records_stage_outputs(tmp_path):
    run = tmp_path / "run"
    stage_one = run / "v1" / "01-research"
    stage_two = run / "v1" / "02-literature"
    repo = tmp_path / "repo"
    repo.mkdir()
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo", "type": "campaign"}), encoding="utf-8")

    codex = Agent("gpt", "codex-cli", "codex-jsonl")
    claude = Agent("claude", "claude-code", "claude-json")
    first_context = runtime.ensure_shared_context(stage_one, repo, codex)
    runtime.record_stage_event(stage_one, repo, codex, "Research evidence", {"session_id": "c1"})
    second_context = runtime.ensure_shared_context(stage_two, repo, claude)

    assert first_context == second_context
    memory = (second_context / "shared-memory.md").read_text(encoding="utf-8")
    assert "Research evidence" in memory
    prompt = runtime.build_prompt_with_shared_context("Review literature", second_context, repo, 120000)
    assert "Research evidence" in prompt
    assert "CURRENT STAGE TASK" in prompt


def test_non_retryable_billing_error_is_not_treated_as_quota():
    stderr = "Third-party apps now draw from your extra usage. Add more at claude.ai/settings/usage"
    assert runtime.is_quota_error("", stderr, 1) is False
    assert runtime.is_quota_error("", "429 rate limit, retry after 60 seconds", 1) is True


def test_worker_context_snapshot_is_independent_and_fully_hashed(tmp_path):
    run = tmp_path / "run"
    stage = run / "v1" / "01-research"
    repo = tmp_path / "repo"
    repo.mkdir()
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo"}), encoding="utf-8")
    agent = Agent("gpt", "codex-cli", "codex-jsonl")

    canonical = runtime.ensure_shared_context(stage, repo, agent)
    (canonical / "artifacts" / "prior.md").write_text("prior evidence", encoding="utf-8")
    snapshot = runtime.create_worker_context_snapshot(stage, canonical)

    assert snapshot != canonical
    assert (snapshot / "artifacts" / "prior.md").read_text(encoding="utf-8") == "prior evidence"
    before = runtime.shared_context_integrity(snapshot)
    assert "artifacts/prior.md" in before

    # Simulate a worker defeating read-only permissions; canonical memory remains safe
    # and the integrity map detects the snapshot mutation.
    target = snapshot / "artifacts" / "prior.md"
    target.chmod(0o600)
    target.write_text("tampered", encoding="utf-8")
    after = runtime.shared_context_integrity(snapshot)
    assert after != before
    assert (canonical / "artifacts" / "prior.md").read_text(encoding="utf-8") == "prior evidence"


def test_runtime_executables_are_codex_and_claude_only():
    assert runtime.RUNTIME_EXECUTABLES == {"codex-cli": "codex", "claude-code": "claude"}


def test_render_command_expands_role_model_and_effort_args(tmp_path):
    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    agent.model = "gpt-role-model"
    agent.effort = "high"
    agent.command = ("codex", "exec", "{model_args}", "{effort_args}", "-")
    command = runtime.render_command(
        agent,
        "task",
        tmp_path / "prompt.md",
        tmp_path / "context",
        tmp_path / "stage",
        tmp_path,
        tmp_path / "last.txt",
    )
    assert command[:4] == ["codex", "exec", "--model", "gpt-role-model"]
    assert "model_reasoning_effort=\"high\"" in command


def test_render_command_expands_claude_role_model_and_effort(tmp_path):
    agent = Agent("claude", "claude-code", "claude-json")
    agent.model = "claude-role-model"
    agent.effort = "medium"
    agent.command = ("claude", "-p", "x", "{model_args}", "{effort_args}")
    command = runtime.render_command(
        agent,
        "task",
        tmp_path / "prompt.md",
        tmp_path / "context",
        tmp_path / "stage",
        tmp_path,
        tmp_path / "last.txt",
    )
    assert command[-4:] == ["--model", "claude-role-model", "--effort", "medium"]


def test_render_command_expands_role_model_and_effort_args(tmp_path):
    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    agent.model = "gpt-role-model"
    agent.effort = "high"
    agent.command = ("codex", "exec", "{model_args}", "{effort_args}", "-")
    command = runtime.render_command(
        agent, "task", tmp_path / "prompt.md", tmp_path / "context",
        tmp_path / "stage", tmp_path, tmp_path / "last.txt",
    )
    assert command[:4] == ["codex", "exec", "--model", "gpt-role-model"]
    assert 'model_reasoning_effort="high"' in command


def test_render_command_expands_claude_role_model_and_effort(tmp_path):
    agent = Agent("claude", "claude-code", "claude-json")
    agent.model = "claude-role-model"
    agent.effort = "medium"
    agent.command = ("claude", "-p", "x", "{model_args}", "{effort_args}")
    command = runtime.render_command(
        agent, "task", tmp_path / "prompt.md", tmp_path / "context",
        tmp_path / "stage", tmp_path, tmp_path / "last.txt",
    )
    assert command[-4:] == ["--model", "claude-role-model", "--effort", "medium"]

def test_run_worker_process_forces_utf8_stdin_and_output(tmp_path):
    script = tmp_path / "utf8_worker.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "text = data.decode('utf-8')\n"
        "sys.stdout.buffer.write(text.encode('utf-8'))\n"
        "sys.stderr.buffer.write('stderr — UTF-8 ✓'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    prompt = "Roadmap — v51 ✓ 中文"
    completed = runtime.run_worker_process(
        [sys.executable, str(script)],
        cwd=tmp_path,
        prompt=prompt,
        timeout=30,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0
    assert completed.stdout == prompt
    assert completed.stderr == "stderr — UTF-8 ✓"



def test_codex_jsonl_normalization_extracts_observed_usage(tmp_path):
    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    output = tmp_path / "last.txt"
    output.write_text("STATUS: PASS\nFinal Codex result for v51\n", encoding="utf-8")
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-usage"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}}),
        ]
    )
    text, metadata = runtime.normalize_worker_output(agent, stdout, "", 0, output)
    assert text.startswith("STATUS: PASS")
    assert metadata["usage"]["input_tokens"] == 120
    assert metadata["usage"]["output_tokens"] == 30
    assert metadata["usage"]["total_tokens"] == 150


def test_claude_json_normalization_extracts_usage_and_cost(tmp_path):
    agent = Agent("claude", "claude-code", "claude-json")
    payload = json.dumps(
        {
            "session_id": "claude-session",
            "result": "STATUS: PASS\nReviewed v52",
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 80, "output_tokens": 20},
        }
    )
    text, metadata = runtime.normalize_worker_output(agent, payload, "", 0, tmp_path / "none")
    assert "Reviewed v52" in text
    assert metadata["total_cost_usd"] == 0.0123
    assert metadata["usage"]["cost_usd"] == 0.0123
    assert metadata["usage"]["total_tokens"] == 100


def test_stage_event_records_learning_ledger_and_roadmap_alignment(tmp_path):
    run = tmp_path / "run"
    stage = run / "v51" / "01-prototype"
    repo = tmp_path / "repo"
    (repo / "request").mkdir(parents=True)
    (repo / "request" / "roadmap.md").write_text("# Roadmap\n\n## v51 Token telemetry\n", encoding="utf-8")
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo"}), encoding="utf-8")
    agent = Agent("gpt", "codex-cli", "codex-jsonl")

    context = runtime.ensure_shared_context(stage, repo, agent)
    runtime.record_stage_event(
        stage,
        repo,
        agent,
        "STATUS: PASS\nImplemented v51 and ran pytest.",
        {"usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}, "returncode": 0},
        prompt="Implement v51 from request/roadmap.md",
        raw_stdout=json.dumps({"usage": {"total_tokens": 150}}),
        raw_stderr="",
    )

    events = list((context / "events").glob("*.json"))
    assert events
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["prompt_artifact"]
    assert event["quality_signals"]["status"] == "PASS"
    assert event["quality_signals"]["requested_versions"] == ["v51"]
    assert event["quality_signals"]["version_overlap"] == ["v51"]
    assert event["roadmap_snapshot"]["available"] is True

    ledger = (context / "learning-ledger.jsonl").read_text(encoding="utf-8")
    assert "total_tokens_observed" in ledger
    summary = json.loads((context / "learning-summary.json").read_text(encoding="utf-8"))
    assert summary["by_runtime"]["codex-cli"]["total_tokens_observed"] == 150
    lessons = (context / "prompt-lessons.md").read_text(encoding="utf-8")
    assert "reduce redundant context" in lessons


def test_scope_assessment_raises_effort_for_security_multiversion_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    scope = runtime.assess_task_scope(
        "Implement security hardening for v51-v68 and cross validate authentication tests",
        repo,
    )
    assert scope["scope_bucket"] in {"large", "critical"}
    assert scope["base_effort"] == "high"
    assert "security" in scope["risk_flags"]
    assert scope["version_span"] == 18


def test_scope_planner_downgrades_low_risk_docs_to_low_effort_and_writes_audit(tmp_path):
    run = tmp_path / "state" / "run-1"
    stage = run / "docs"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "scope"}), encoding="utf-8")
    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    agent.model = "gpt-default"
    agent.effort = "medium"
    context = runtime.ensure_shared_context(stage, repo, agent)

    name, selected, decision = runtime.select_agent_for_scope(
        {"gpt": agent}, "gpt", "Update README wording and comments only", repo, context, stage,
        allow_agent_switch=False,
    )

    assert name == "gpt"
    assert selected.effort == "low"
    assert decision["scope"]["base_effort"] == "low"
    assert (stage / "scope-assessment.json").exists()
    assert (stage / "routing-decision.json").exists()
    assert "scope-routing" in (context / "scope-routing-ledger.jsonl").read_text(encoding="utf-8") or decision["rationale"]


def test_scope_planner_uses_prior_learning_to_switch_runtime(tmp_path):
    state = tmp_path / "state"
    prior = state / "prior-run" / "shared-context"
    prior.mkdir(parents=True)
    (prior / "learning-ledger.jsonl").write_text(
        json.dumps({
            "completed_at": "2026-01-01T00:00:00+00:00",
            "agent": "claude",
            "runtime": "claude-code",
            "model": "claude-good",
            "effort": "medium",
            "status": "PASS",
            "quality_signal_score": 0.95,
            "total_tokens_observed": 2000,
            "scope_bucket": "medium",
            "task_types": ["implementation", "testing"],
        }) + "\n",
        encoding="utf-8",
    )
    run = state / "current-run"
    stage = run / "code"
    repo = tmp_path / "repo"
    repo.mkdir()
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "current"}), encoding="utf-8")
    gpt = Agent("gpt", "codex-cli", "codex-jsonl")
    gpt.model = "gpt-default"
    gpt.effort = "medium"
    claude = Agent("claude", "claude-code", "claude-json")
    claude.model = "claude-good"
    claude.effort = "medium"
    context = runtime.ensure_shared_context(stage, repo, gpt)

    name, selected, decision = runtime.select_agent_for_scope(
        {"gpt": gpt, "claude": claude},
        "gpt",
        "Implement adapter fix and add pytest validation",
        repo,
        context,
        stage,
        allow_agent_switch=True,
    )

    assert name == "claude"
    assert selected.runtime == "claude-code"
    assert decision["history_rows_considered"] >= 1
    assert any(row["agent"] == "claude" and row["history"]["rows"] >= 1 for row in decision["candidate_scores"])


def test_repo_graph_context_pack_maps_symbols_and_relevant_files(tmp_path):
    run = tmp_path / "run"
    stage = run / "v1" / "repo-map"
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (run / "job.json").parent.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "repo-map"}), encoding="utf-8")
    (repo / "pkg" / "__init__.py").write_text("from .core import build_context\n", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text(
        "def build_context(request: str) -> str:\n"
        "    \"\"\"Build compact context for a request.\"\"\"\n"
        "    return request.strip()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "cli.py").write_text(
        "from pkg.core import build_context\n\n"
        "def main() -> None:\n"
        "    print(build_context('cli'))\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_core.py").write_text(
        "from pkg.core import build_context\n\n"
        "def test_build_context():\n"
        "    assert build_context(' x ') == 'x'\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n[project.scripts]\ndemo = 'pkg.cli:main'\n",
        encoding="utf-8",
    )
    agent = Agent("gpt", "codex-cli", "codex-jsonl")

    context = runtime.ensure_shared_context(stage, repo, agent)
    graph = json.loads((context / "repo-map" / "graph.json").read_text(encoding="utf-8"))
    assert graph["entrypoints"]["demo"] == "pkg.cli:main"
    assert any(row["path"] == "pkg/core.py" and row["symbols"] for row in graph["files"])
    assert any(edge["kind"] == "imports_file" and edge["target"] == "file:pkg/core.py" for edge in graph["edges"])

    prompt = runtime.build_prompt_with_shared_context(
        "Fix build_context CLI behavior and add pytest validation", context, repo, 120000
    )
    pack = (context / "repo-context-pack.md").read_text(encoding="utf-8")
    assert "pkg/core.py" in pack
    assert "tests/test_core.py" in pack
    assert "repo-map/REPO_MAP.md" in prompt
    assert "Task-specific repository context pack" in prompt


def test_scope_assessment_includes_repo_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(35):
        (repo / f"module_{index}.py").write_text(f"def f_{index}():\n    return {index}\n", encoding="utf-8")

    scope = runtime.assess_task_scope("Update docs only", repo)

    assert scope["repo_facts"]["file_count"] == 35
    assert any("repository map" in reason for reason in scope["reasons"])


def test_repo_graph_graphify_class_outputs_and_query_helpers(tmp_path):
    from hermes_legion_commander import repo_graph

    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs").mkdir()
    (repo / "app" / "core.py").write_text(
        "def normalize(value: str) -> str:\n"
        "    return value.strip().lower()\n\n"
        "def build_context(value: str) -> str:\n"
        "    return normalize(value)\n",
        encoding="utf-8",
    )
    (repo / "app" / "api.py").write_text(
        "from app.core import build_context\n\n"
        "def handle_request(value: str) -> str:\n"
        "    return build_context(value)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_core.py").write_text(
        "from app.core import build_context\n\n"
        "def test_build_context():\n"
        "    assert build_context(' X ') == 'x'\n",
        encoding="utf-8",
    )
    (repo / "docs" / "ARCH.md").write_text("# Architecture\n\nSee `app/core.py`.\n", encoding="utf-8")

    context = tmp_path / "shared-context"
    graph = repo_graph.refresh_repo_intelligence(context, repo, task_prompt="fix build_context request handler tests")

    repo_map = context / "repo-map"
    assert (repo_map / "graph.json").is_file()
    assert (repo_map / "graph.html").is_file()
    assert (repo_map / "GRAPH_REPORT.md").is_file()
    assert (repo_map / "wiki").is_dir()
    assert graph["schema_version"] >= 2
    assert graph["nodes"]
    assert graph["communities"]
    assert any(edge["kind"] == "calls" for edge in graph["edges"])
    assert any(edge["kind"] == "references_file" and edge["target"] == "file:app/core.py" for edge in graph["edges"])

    results = repo_graph.query_graph(graph, "build context", budget=5)
    assert any(row.get("path") == "app/core.py" for row in results)

    paths = repo_graph.path_between(graph, "api", "core", max_depth=4, budget=3)
    assert paths


def test_prompt_preflight_estimates_subscription_shadow_cost_before_worker(tmp_path, monkeypatch):
    run = tmp_path / "run"
    stage = run / "v1" / "01-research"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    stage.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo"}), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)

    agent = Agent("gpt", "codex-cli", "codex-jsonl")
    agent.provider = "openai"
    agent.model = "gpt-5.3-codex"
    preflight = runtime.record_prompt_preflight(
        stage,
        agent,
        "Implement v51-v60 using repo-map/GRAPH_REPORT.md and add tests.",
        {"scope": {"scope_bucket": "large", "scope_score": 6.0, "task_types": ["implementation"], "risk_flags": []}},
        env={},
    )

    assert preflight["prompt"]["estimated_tokens"] > 0
    assert preflight["auth"]["mode"] == "chatgpt_oauth_or_cli_session"
    assert preflight["auth"]["uses_subscription_or_oauth"] is True
    assert preflight["estimated_api_cost_usd"]["cost_usd"]["total_expected"] > 0
    assert (stage / "prompt-preflight.json").is_file()
    context = run / "shared-context"
    assert (context / "prompt-preflight-ledger.jsonl").is_file()
    summary = (context / "prompt-cost-summary.md").read_text(encoding="utf-8")
    assert "shadow API cost" in summary


def test_record_stage_event_reconciles_preflight_with_observed_usage(tmp_path):
    run = tmp_path / "run"
    stage = run / "v1" / "02-implement"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    stage.mkdir(parents=True)
    (run / "job.json").write_text(json.dumps({"run_id": "demo"}), encoding="utf-8")
    agent = Agent("claude", "claude-code", "claude-json")
    agent.provider = "anthropic"
    agent.model = "claude-sonnet-4-6"
    preflight = runtime.record_prompt_preflight(stage, agent, "Review auth implementation for security.", {"scope": {"scope_bucket": "medium"}}, env={})
    metadata = {
        "prompt_preflight": preflight,
        "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        "usage_reconciliation": runtime.reconcile_usage(preflight, {"input_tokens": 20, "output_tokens": 10}),
    }

    runtime.record_stage_event(stage, repo, agent, "PASS\nReviewed auth implementation.", metadata, prompt="Review auth implementation for security.")
    events = list((run / "shared-context" / "events").glob("*.json"))
    assert events
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["prompt_metrics"]["estimated_tokens"] == preflight["prompt"]["estimated_tokens"]
    assert event["runtime_metadata"]["usage_reconciliation"]["has_observed_usage"] is True
    learning = json.loads((run / "shared-context" / "learning-summary.json").read_text(encoding="utf-8"))
    assert learning["totals"]["estimated_input_tokens"] == preflight["prompt"]["estimated_tokens"]
