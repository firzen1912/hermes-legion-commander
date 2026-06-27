#!/usr/bin/env python3
"""Coordinate Codex CLI and Claude Code directly.

Hermes Legion Commander runs three distinct execution modes; this module hosts two of them:

  - Collaborative council mode (`campaign`): multiple roles collaborate per version and the
    campaign AUTO-CONTINUES across the whole version range in one run.
  - Rapid alternate mode (`alternate`): a single chosen worker implements ONE version, then the
    run STOPS at the version boundary and emits a handoff that prompts the other worker
    (codex<->claude) to continue the next version. Use it for a fast single-worker relay with a
    human (or scheduler) deciding each handoff.

Competitive convergence mode lives in checkpoint_competition.py (two independent candidates per
version that are judged and converged).

The supervisor preserves the full roadmap-driven workflow while using the vendors' native CLIs and
subscription/OAuth authentication. A provider-neutral shared-context directory gives every worker the
same durable campaign memory, prior outputs, decisions, Git snapshot, and artifact index. All mutable
stages share one isolated candidate worktree.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import hashlib
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

try:
    from .pr_workflow import (
        PRWorkflowOptions,
        PRWorkflowError,
        actor_from_worker,
        branch_name as pr_branch_name,
        build_pr_body,
        commit_all_if_changed,
        fetch_base,
        push_branch,
        create_or_view_pr,
        write_pr_artifacts,
    )
except ImportError:  # Support direct file loading in isolated validation fixtures.
    from hermes_legion_commander.pr_workflow import (
        PRWorkflowOptions,
        PRWorkflowError,
        actor_from_worker,
        branch_name as pr_branch_name,
        build_pr_body,
        commit_all_if_changed,
        fetch_base,
        push_branch,
        create_or_view_pr,
        write_pr_artifacts,
    )

try:
    from .prompt_contracts import host_side_evidence_boundary, per_version_recipe, quota_handoff_template, subagent_delegation_contract, version_execution_contract
    from .stop_condition import (
        deterministic_all_passed,
        parse_stop_verdict,
        run_deterministic_checks,
        stop_condition_prompt,
    )
    from .loop_driver import (
        LoopLimits,
        LoopState,
        cloud_workflow_yaml,
        load_or_init_state,
        run_loop,
    )
    from .roadmap import (
        campaign_versions,
        extract_version_entry,
        implementation_section,
        parse_version_entries,
        release_versions,
        version_keys,
    )
except ImportError:  # Support direct file loading in isolated validation fixtures.
    from hermes_legion_commander.prompt_contracts import host_side_evidence_boundary, per_version_recipe, quota_handoff_template, subagent_delegation_contract, version_execution_contract
    from hermes_legion_commander.stop_condition import (
        deterministic_all_passed,
        parse_stop_verdict,
        run_deterministic_checks,
        stop_condition_prompt,
    )
    from hermes_legion_commander.loop_driver import (
        LoopLimits,
        LoopState,
        cloud_workflow_yaml,
        load_or_init_state,
        run_loop,
    )
    from hermes_legion_commander.roadmap import (
        campaign_versions,
        extract_version_entry,
        implementation_section,
        parse_version_entries,
        release_versions,
        version_keys,
    )

try:
    from .worker_runtime import (
        RUNTIME_EXECUTABLES,
        build_prompt_with_shared_context,
        create_worker_context_snapshot,
        ensure_shared_context,
        is_quota_error as runtime_quota_error,
        normalize_worker_output,
        classify_worker_failure,
        sanitized_worker_environment,
        record_stage_event,
        record_prompt_preflight,
        reconcile_usage,
        render_command,
        run_worker_process,
        seal_worker_context_snapshot,
        select_agent_for_scope,
        shared_context_integrity,
        stdin_for,
    )
except ImportError:
    from hermes_legion_commander.worker_runtime import (
        RUNTIME_EXECUTABLES,
        build_prompt_with_shared_context,
        create_worker_context_snapshot,
        ensure_shared_context,
        is_quota_error as runtime_quota_error,
        normalize_worker_output,
        classify_worker_failure,
        sanitized_worker_environment,
        record_stage_event,
        record_prompt_preflight,
        reconcile_usage,
        render_command,
        run_worker_process,
        seal_worker_context_snapshot,
        select_agent_for_scope,
        shared_context_integrity,
        stdin_for,
    )

UTC = dt.timezone.utc
SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


class CouncilError(RuntimeError):
    pass


class QuotaPaused(CouncilError):
    """Raised when an agent quota is exhausted and no-wait mode is active."""

    def __init__(self, agent: str, retry_at: str, stage_dir: Path):
        super().__init__(f"{agent} quota exhausted; retry after {retry_at}; stage: {stage_dir}")
        self.agent = agent
        self.retry_at = retry_at
        self.stage_dir = stage_dir


class ApprovalRequired(CouncilError):
    """Raised when a dangerous or massive change needs explicit user approval."""

    def __init__(self, run_id: str, reason: str, request_path: Path):
        super().__init__(f"approval required for {run_id}: {reason}; review {request_path}")
        self.run_id = run_id
        self.reason = reason
        self.request_path = request_path


@dataclasses.dataclass(frozen=True)
class Agent:
    name: str
    role: str
    runtime: str
    provider: str
    command: tuple[str, ...]
    timeout_seconds: int
    model: str = ""
    effort: str = "medium"
    prompt_transport: str = "stdin"
    output_format: str = "text"
    capabilities: tuple[str, ...] = ()
    unset_env: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Config:
    repo: Path
    state_dir: Path
    research_dir: Path
    max_prompt_chars: int
    checks: tuple[tuple[str, ...], ...]
    agents: dict[str, Agent]
    lookback_days: int
    max_findings: int
    topics: tuple[str, ...]
    pdf_dir: Path
    review_dir: Path
    library_manifest: Path
    literature_reviewer: str
    max_pdf_chars: int
    default_budget: str
    campaign_strategy: str
    literature_validation: str
    quota_retry_seconds: int
    quota_max_retry_seconds: int
    quota_wait: bool
    massive_files: int
    massive_lines: int
    roadmap_path: Path
    iterations_dir: Path
    tests_dir: Path
    experiments_dir: Path
    results_dir: Path
    version_test_command: tuple[str, ...]
    version_experiment_command: tuple[str, ...]
    version_validation_timeout_seconds: int
    roles: dict[str, str]
    worker_failover: bool = True
    failover_on: tuple[str, ...] = ("quota", "entitlement", "authentication")
    subagent_cap: int = 5


def _make_pr_options_from_args(args: Any, *, mode: str, actor: str = "commander") -> PRWorkflowOptions:
    enabled = bool(getattr(args, "pr", False) or getattr(args, "push_branch", False) or getattr(args, "open_pr", False))
    return PRWorkflowOptions(
        enabled=enabled,
        base_branch=str(getattr(args, "pr_base", "dev") or "dev"),
        remote=str(getattr(args, "pr_remote", "origin") or "origin"),
        actor=actor,
        mode=mode,
        slug=getattr(args, "pr_slug", None),
        push=bool(getattr(args, "push_branch", False) or getattr(args, "open_pr", False)),
        open_pr=bool(getattr(args, "open_pr", False)),
        draft=bool(getattr(args, "draft_pr", False)),
        title=getattr(args, "pr_title", None),
        gh=getattr(args, "gh", None),
    )


def _add_pr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pr", action="store_true", help="create a Legion Commander review branch from the latest origin/dev-equivalent base")
    parser.add_argument("--pr-base", default="dev", help="base branch for the review branch and pull request (default: dev)")
    parser.add_argument("--pr-remote", default="origin", help="Git remote to fetch/push (default: origin)")
    parser.add_argument("--pr-slug", help="short branch slug; Commander appends a timestamp")
    parser.add_argument("--push-branch", action="store_true", help="push the review branch after committing generated changes")
    parser.add_argument("--open-pr", action="store_true", help="push the review branch and open a GitHub pull request back to --pr-base")
    parser.add_argument("--draft-pr", action="store_true", help="open the pull request as a draft")
    parser.add_argument("--pr-title", help="custom pull request title")
    parser.add_argument("--gh", type=Path, help="path to gh executable for PR creation; auto-detects PATH and common Windows installs")


def _finalize_pr_workflow(
    *,
    options: PRWorkflowOptions,
    run_dir: Path,
    worktree: Path,
    branch: str,
    run_id: str,
    mode: str,
    title: str,
    summary: str,
    validation: str,
    artifacts: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    if not options.active:
        return None
    payload: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "run_id": run_id,
        "branch": branch,
        "base_branch": options.base_branch,
        "remote": options.remote,
        "worktree": str(worktree),
        "dry_run": dry_run,
    }
    if dry_run:
        payload["skipped"] = "dry-run"
        write_pr_artifacts(run_dir, payload)
        return payload
    commit = commit_all_if_changed(worktree, message=title)
    payload["commit"] = commit
    governance_report = None
    governance_comment = ""
    try:
        from .workflow_governance import refresh_governance, render_pr_comment
        governance_report = refresh_governance(
            run_dir / "shared-context",
            worktree,
            task_prompt=summary,
            base_ref=f"{options.remote}/{options.base_branch}",
        )
        governance_comment = render_pr_comment(governance_report)
        payload["governance"] = {
            "risk": governance_report.get("risk"),
            "merge_readiness": governance_report.get("merge_readiness"),
            "artifacts": str(run_dir / "shared-context" / "governance"),
        }
    except Exception as exc:  # pragma: no cover - PR governance enriches review, but should not mask completed work
        payload["governance_error"] = str(exc)
    body = build_pr_body(
        mode=mode,
        branch=branch,
        base_branch=options.base_branch,
        run_id=run_id,
        summary=summary,
        validation=validation + ("\n\n" + governance_comment if governance_comment else ""),
        artifacts=[*(artifacts or []), str(run_dir / "shared-context" / "governance" / "merge-readiness.md"), str(run_dir / "shared-context" / "dashboard" / "index.html")],
        extra={**(extra or {}), "governance": payload.get("governance")},
    )
    payload["title"] = options.title or title
    payload["body"] = body
    if options.push or options.open_pr:
        payload["pushed"] = push_branch(worktree, branch=branch, remote=options.remote)
    if options.open_pr:
        payload["pull_request"] = create_or_view_pr(
            worktree,
            branch=branch,
            base_branch=options.base_branch,
            title=payload["title"],
            body=body,
            draft=options.draft,
            gh_path=options.gh,
            remote=options.remote,
        )
        try:
            if governance_report is not None:
                from .workflow_governance import post_pr_comment, render_pr_comment
                payload["review_comment"] = post_pr_comment(
                    worktree,
                    branch_or_pr=branch,
                    body=render_pr_comment(governance_report),
                    gh_path=options.gh,
                )
        except Exception as exc:  # pragma: no cover - comments are review aids
            payload["review_comment_error"] = str(exc)
    write_pr_artifacts(run_dir, payload)
    return payload


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (base / p).resolve()


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise CouncilError(f"{field} must be a non-empty array of strings")
    return tuple(value)


def _repo_relative_path(value: str, field: str) -> Path:
    """Return a normalized path that is guaranteed to remain inside a target checkout."""
    path = Path(value).expanduser()
    if path.is_absolute() or ".." in path.parts:
        raise CouncilError(f"{field} must be a repository-relative path without '..'")
    return path


DEFAULT_ROLES = {
    "roadmap_plan_reviewer": "gpt",
    "researcher": "gpt",
    "literature_reviewer": "claude",
    "prototyper": "gpt",
    "code_polisher": "claude",
    "security_assurance": "claude",
}


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    base = path.parent
    root = raw.get("council")
    agents_raw = raw.get("agents")
    research = raw.get("research")
    if not isinstance(root, dict) or not isinstance(agents_raw, dict) or not isinstance(research, dict):
        raise CouncilError("missing [council], [agents.*], or [research] configuration")
    agents: dict[str, Agent] = {}
    if not agents_raw:
        raise CouncilError("at least one [agents.*] table is required")
    for name, item in agents_raw.items():
        if not isinstance(item, dict):
            raise CouncilError(f"[agents.{name}] must be a table")
        runtime = str(item.get("runtime", ""))
        if not runtime:
            raise CouncilError(
                f"agents.{name}.runtime is required "
                f"(built-in: 'codex-cli', 'claude-code'; or any custom runtime id)"
            )
        provider = str(item.get("provider", ""))
        if not provider:
            raise CouncilError(f"agents.{name}.provider must identify the vendor backend")
        command = _strings(item.get("command"), f"agents.{name}.command")
        # For the two built-in runtimes, keep the executable safety check so a
        # misconfigured command cannot silently launch the wrong tool. Custom
        # runtimes accept any command: the operator owns the invocation, and the
        # runtime engine parses output by output_format with a plain-text
        # fallback, so any provider's CLI can drive a role.
        expected_executable = RUNTIME_EXECUTABLES.get(runtime)
        if expected_executable is not None and Path(command[0]).name.lower().removesuffix(".exe") != expected_executable:
            raise CouncilError(
                f"agents.{name}.command must launch {expected_executable}, not {command[0]}"
            )
        prompt_transport = str(item.get("prompt_transport", "stdin"))
        if prompt_transport not in {"stdin", "argument"}:
            raise CouncilError(f"agents.{name}.prompt_transport must be 'stdin' or 'argument'")
        agents[name] = Agent(
            name=name,
            role=str(item.get("role", "")),
            runtime=runtime,
            provider=provider,
            command=command,
            timeout_seconds=int(item.get("timeout_seconds", 10800)),
            model=str(item.get("model", "")),
            effort=str(item.get("effort", "medium")),
            prompt_transport=prompt_transport,
            output_format=str(item.get("output_format", "text")),
            capabilities=tuple(str(x) for x in item.get("capabilities", [])),
            unset_env=tuple(str(x) for x in item.get("unset_env", [])),
        )
    roles_raw = raw.get("roles", {})
    if not isinstance(roles_raw, dict):
        raise CouncilError("[roles] must be a table")
    role_defaults = DEFAULT_ROLES
    roles: dict[str, str] = {}
    for role_name, default_agent in role_defaults.items():
        selected = str(roles_raw.get(role_name, default_agent))
        if selected not in agents:
            raise CouncilError(
                f"roles.{role_name} must name a configured agent {sorted(agents)}; "
                f"set it explicitly in [roles] when not using the default '{default_agent}'"
            )
        roles[role_name] = selected
    literature_reviewer = str(research.get("literature_reviewer", DEFAULT_ROLES["literature_reviewer"]))
    if literature_reviewer not in agents:
        raise CouncilError(
            f"research.literature_reviewer must name a configured agent {sorted(agents)}"
        )
    checks_raw = root.get("checks", [["python", "-m", "pytest", "-q"]])
    if not isinstance(checks_raw, list):
        raise CouncilError("council.checks must be an array")
    checks = tuple(_strings(x, "council.checks[]") for x in checks_raw)
    campaign_strategy = str(root.get("campaign_strategy", "full"))
    if campaign_strategy not in {"full", "staggered", "alternating"}:
        raise CouncilError("council.campaign_strategy must be 'full', 'staggered', or 'alternating'")
    return Config(
        repo=_resolve(base, str(root.get("repo", ".."))),
        state_dir=_resolve(base, str(root.get("state_dir", "../.hermes-legion-commander/model-council"))),
        research_dir=_resolve(base, str(root.get("research_dir", "../research"))),
        max_prompt_chars=max(10000, int(root.get("max_prompt_chars", 120000))),
        checks=checks,
        agents=agents,
        lookback_days=max(1, int(research.get("lookback_days", 30))),
        max_findings=max(1, int(research.get("max_findings", 35))),
        topics=_strings(research.get("topics"), "research.topics"),
        pdf_dir=_resolve(base, str(research.get("pdf_dir", "../research/library/pdfs"))),
        review_dir=_resolve(base, str(research.get("review_dir", "../research/library/reviews"))),
        library_manifest=_resolve(base, str(research.get("library_manifest", "../research/library/manifest.jsonl"))),
        literature_reviewer=literature_reviewer,
        max_pdf_chars=max(20000, int(research.get("max_pdf_chars", 180000))),
        default_budget=str(root.get("default_budget", "balanced")),
        campaign_strategy=campaign_strategy,
        literature_validation=str(research.get("literature_validation", "balanced")),
        quota_retry_seconds=max(60, int(root.get("quota_retry_seconds", 900))),
        quota_max_retry_seconds=max(60, int(root.get("quota_max_retry_seconds", 21600))),
        quota_wait=bool(root.get("quota_wait", True)),
        massive_files=max(1, int(root.get("massive_files", 25))),
        massive_lines=max(1, int(root.get("massive_lines", 1500))),
        roadmap_path=Path(str(root.get("roadmap_path", "docs/roadmap.md"))).expanduser(),
        iterations_dir=_repo_relative_path(str(root.get("iterations_dir", "docs/iterations")),
                                           "council.iterations_dir"),
        tests_dir=_repo_relative_path(str(root.get("tests_dir", "tests")),
                                      "council.tests_dir"),
        experiments_dir=_repo_relative_path(str(root.get("experiments_dir", "experiments")),
                                            "council.experiments_dir"),
        results_dir=_repo_relative_path(str(root.get("results_dir", "results/iterations")),
                                        "council.results_dir"),
        version_test_command=_strings(
            root.get("version_test_command", ["python", "-m", "pytest", "-q"]),
            "council.version_test_command",
        ),
        version_experiment_command=_strings(
            root.get("version_experiment_command", ["python"]),
            "council.version_experiment_command",
        ),
        version_validation_timeout_seconds=max(
            60, int(root.get("version_validation_timeout_seconds", 1800))
        ),
        roles=roles,
        worker_failover=bool(root.get("worker_failover", True)),
        failover_on=tuple(str(x) for x in root.get("failover_on", ["quota", "entitlement", "authentication"])),
        subagent_cap=max(0, int(root.get("subagent_cap", 5))),
    )




def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def render(agent: Agent, prompt: str, *, prompt_file: Path | None = None,
           context_dir: Path | None = None, stage_dir: Path | None = None,
           cwd: Path | None = None, output_file: Path | None = None) -> list[str]:
    """Render a direct vendor CLI command.

    The optional paths are populated by ``run_agent``. They remain optional so
    configuration and unit tests can inspect command rendering independently.
    """
    placeholder = Path(".")
    return render_command(
        agent, prompt, prompt_file or placeholder / "prompt.md",
        context_dir or placeholder / "shared-context",
        stage_dir or placeholder / "stage", cwd or placeholder,
        output_file or placeholder / "last-message.txt",
    )


def is_quota_error(stdout: str, stderr: str, returncode: int) -> bool:
    return runtime_quota_error(stdout, stderr, returncode)


def run_agent(config: Config, agent_name: str, prompt: str, cwd: Path, stage_dir: Path,
              dry_run: bool, wait_for_quota: bool | None = None) -> str:
    """Run one durable stage with shared memory, failover, and quota recovery."""
    requested_agent = agent_name
    wait_for_quota = config.quota_wait if wait_for_quota is None else wait_for_quota
    stage_dir.mkdir(parents=True, exist_ok=True)
    state_path = stage_dir / "state.json"
    stdout_path = stage_dir / "stdout.md"
    stderr_path = stage_dir / "stderr.txt"
    raw_stdout_path = stage_dir / "raw-stdout.txt"
    if state_path.exists():
        prior = json.loads(state_path.read_text(encoding="utf-8"))
        if prior.get("status") == "completed" and stdout_path.exists():
            return stdout_path.read_text(encoding="utf-8")

    primary = config.agents[requested_agent]
    canonical_context = ensure_shared_context(stage_dir, cwd, primary)
    selected_agent_name, selected_primary, scope_routing = select_agent_for_scope(
        config.agents, requested_agent, prompt, cwd, canonical_context, stage_dir,
        role=str(getattr(primary, "role", "")), allow_agent_switch=not dry_run,
    )
    candidate_agents = dict(config.agents)
    candidate_agents[selected_agent_name] = selected_primary
    canonical_context = ensure_shared_context(stage_dir, cwd, selected_primary)
    context_dir = create_worker_context_snapshot(stage_dir, canonical_context)
    combined_prompt = build_prompt_with_shared_context(
        prompt, context_dir, cwd, config.max_prompt_chars, include_git_snapshot=not dry_run
    )
    seal_worker_context_snapshot(context_dir)
    prompt_path = stage_dir / "prompt.md"
    prompt_path.write_text(combined_prompt, encoding="utf-8")

    candidates = [selected_agent_name]
    if getattr(config, "worker_failover", False):
        candidates.extend(name for name in config.agents if name != selected_agent_name)
    if dry_run:
        candidates = candidates[:1]

    attempts = 0
    delay = config.quota_retry_seconds
    failovers: list[dict[str, Any]] = []
    cycle_index = 0
    while True:
        current_name = candidates[cycle_index]
        agent = candidate_agents.get(current_name, config.agents[current_name])
        output_file = stage_dir / f"last-message-{current_name}.txt"
        prompt_preflight = record_prompt_preflight(stage_dir, agent, combined_prompt, scope_routing, env=os.environ.copy())
        cmd = render(
            agent, combined_prompt, prompt_file=prompt_path, context_dir=context_dir,
            stage_dir=stage_dir, cwd=cwd, output_file=output_file,
        )
        (stage_dir / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
        (stage_dir / f"command-{current_name}.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")

        if dry_run:
            output = (
                f"DRY RUN: {current_name} / {agent.runtime}\n\n"
                f"Requested worker: {requested_agent}\n"
                f"Prompt prepared at: {prompt_path}\n"
                f"Worker context snapshot: {context_dir}\n"
                f"Prompt characters: {len(combined_prompt)}\n"
                f"Estimated input tokens: {prompt_preflight.get('prompt', {}).get('estimated_tokens')}\n"
                f"Shadow API cost expected USD: {prompt_preflight.get('estimated_api_cost_usd', {}).get('cost_usd', {}).get('total_expected')}\n"
            )
            stdout_path.write_text(output, encoding="utf-8")
            raw_stdout_path.write_text(output, encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            _atomic_json(state_path, {
                "status": "completed", "agent": current_name, "requested_agent": requested_agent,
                "runtime": agent.runtime, "attempts": 0, "dry_run": True,
                "scope_routing": scope_routing,
                "prompt_preflight": prompt_preflight,
                "shared_context": str(canonical_context), "worker_context": str(context_dir),
            })
            record_stage_event(stage_dir, cwd, agent, output, {"dry_run": True, "scope_routing": scope_routing, "prompt_preflight": prompt_preflight}, capture_git=False, prompt=combined_prompt)
            return output
        if shutil.which(cmd[0]) is None:
            raise CouncilError(f"required CLI not found: {cmd[0]}")

        attempts += 1
        integrity_before = shared_context_integrity(context_dir)
        _atomic_json(state_path, {
            "status": "running", "agent": current_name, "requested_agent": requested_agent,
            "runtime": agent.runtime, "attempts": attempts, "failovers": failovers,
            "scope_routing": scope_routing,
            "prompt_preflight": prompt_preflight,
            "started_at": dt.datetime.now(UTC).isoformat(),
            "shared_context": str(canonical_context), "worker_context": str(context_dir),
        })
        try:
            completed = run_worker_process(
                cmd, cwd=cwd, prompt=stdin_for(agent, combined_prompt),
                timeout=agent.timeout_seconds,
                env=sanitized_worker_environment(agent, os.environ.copy()),
            )
        except KeyboardInterrupt:
            _atomic_json(state_path, {
                "status": "interrupted", "agent": current_name, "requested_agent": requested_agent,
                "runtime": agent.runtime, "attempts": attempts,
                "interrupted_at": dt.datetime.now(UTC).isoformat(),
            })
            raise
        raw_stdout = completed.stdout or ""
        raw_stderr = completed.stderr or ""
        raw_stdout_path.write_text(raw_stdout, encoding="utf-8")
        stderr_path.write_text(raw_stderr, encoding="utf-8")
        (stage_dir / f"raw-stdout-{current_name}.txt").write_text(raw_stdout, encoding="utf-8")
        (stage_dir / f"stderr-{current_name}.txt").write_text(raw_stderr, encoding="utf-8")

        failure_kind = classify_worker_failure(raw_stdout, raw_stderr, completed.returncode)
        can_failover = failure_kind in set(getattr(config, "failover_on", ())) and cycle_index + 1 < len(candidates)
        if can_failover:
            next_name = candidates[cycle_index + 1]
            failovers.append({
                "from": current_name, "to": next_name, "reason": failure_kind,
                "at": dt.datetime.now(UTC).isoformat(), "returncode": completed.returncode,
            })
            cycle_index += 1
            continue

        if failure_kind == "quota":
            retry_at = (dt.datetime.now(UTC) + dt.timedelta(seconds=delay)).isoformat()
            _atomic_json(state_path, {
                "status": "quota_paused", "agent": current_name, "requested_agent": requested_agent,
                "runtime": agent.runtime, "attempts": attempts, "retry_at": retry_at,
                "returncode": completed.returncode, "failovers": failovers,
            })
            if not wait_for_quota:
                raise QuotaPaused(current_name, retry_at, stage_dir)
            time.sleep(delay)
            delay = min(delay * 2, config.quota_max_retry_seconds)
            cycle_index = 0
            continue

        try:
            normalized, runtime_metadata = normalize_worker_output(
                agent, raw_stdout, raw_stderr, completed.returncode, output_file
            )
        except RuntimeError as exc:
            _atomic_json(state_path, {
                "status": "failed", "agent": current_name, "requested_agent": requested_agent,
                "runtime": agent.runtime, "attempts": attempts, "returncode": completed.returncode,
                "error": str(exc), "failure_kind": failure_kind, "failovers": failovers,
                "failed_at": dt.datetime.now(UTC).isoformat(),
            })
            raise CouncilError(f"{current_name} ({agent.runtime}) failed in {stage_dir.name}: {exc}") from exc

        integrity_after = shared_context_integrity(context_dir)
        if integrity_after != integrity_before:
            _atomic_json(state_path, {
                "status": "failed", "agent": current_name, "requested_agent": requested_agent,
                "runtime": agent.runtime, "attempts": attempts,
                "error": "worker modified supervisor-owned shared context",
                "failed_at": dt.datetime.now(UTC).isoformat(),
            })
            raise CouncilError(f"{current_name} modified the supervisor-owned shared context in {stage_dir.name}")

        stdout_path.write_text(normalized.rstrip() + "\n", encoding="utf-8")
        runtime_metadata["requested_agent"] = requested_agent
        runtime_metadata["selected_agent"] = selected_agent_name
        runtime_metadata["executed_agent"] = current_name
        runtime_metadata["subagent_cap"] = getattr(config, "subagent_cap", 5)
        runtime_metadata["scope_routing"] = scope_routing
        runtime_metadata["prompt_preflight"] = prompt_preflight
        runtime_metadata["usage_reconciliation"] = reconcile_usage(prompt_preflight, runtime_metadata.get("usage") if isinstance(runtime_metadata.get("usage"), dict) else {})
        runtime_metadata["failovers"] = failovers
        record_stage_event(stage_dir, cwd, agent, normalized, runtime_metadata, prompt=combined_prompt, raw_stdout=raw_stdout, raw_stderr=raw_stderr, command=cmd)
        _atomic_json(state_path, {
            "status": "completed", "agent": current_name, "requested_agent": requested_agent,
            "runtime": agent.runtime, "attempts": attempts,
            "completed_at": dt.datetime.now(UTC).isoformat(),
            "runtime_metadata": runtime_metadata, "failovers": failovers,
            "scope_routing": scope_routing,
            "shared_context": str(canonical_context), "worker_context": str(context_dir),
        })
        return normalized


@dataclasses.dataclass(frozen=True)
class TaskRoute:
    kind: str
    primary: str
    reviewer: str | None
    budget: str
    include_library: bool
    rationale: str


def route_task(task: str, budget: str = "balanced") -> TaskRoute:
    """Choose the least expensive role that still fits the task's risk and complexity."""
    text = task.lower()
    high_risk = any(x in text for x in ("security", "crypto", "authentication", "authorization",
                                         "safety", "mavlink", "flight", "byzantine", "zero trust"))
    research_signal = any(x in text for x in ("research", "paper", "literature", "citation", "state of the art",
                                               "slam", "fmarl", "interoperability"))
    implementation_signal = any(x in text for x in ("implement", "code", "fix", "refactor", "test", "adapter",
                                                     "node", "service", "pipeline", "prototype"))
    research = research_signal and not implementation_signal
    docs = any(x in text for x in ("readme", "documentation", "design doc", "draft", "roadmap"))
    small = len(task) < 500 and any(x in text for x in ("typo", "rename", "format", "comment", "small fix"))
    if research:
        return TaskRoute("research", "gpt", "claude", budget, True,
                         "Codex performs roadmap-bounded source discovery; Claude audits literature and evidence quality.")
    if high_risk:
        return TaskRoute("security-code", "gpt", "claude", budget, True,
                         "GPT prototypes; Claude performs mandatory security and architecture polish.")
    if docs:
        return TaskRoute("documentation", "gpt", "claude" if budget == "quality" else None, budget, True,
                         "GPT drafts efficiently; Claude review is reserved for quality mode.")
    if small and budget == "economy":
        return TaskRoute("small-code", "gpt", None, budget, False,
                         "A focused low-risk change does not justify a second model pass.")
    return TaskRoute("code", "gpt", "claude", budget, False,
                     "GPT builds quickly and Claude polishes the candidate.")


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value[:96] or "paper"


def normalize_version(version: int | str) -> str:
    """Normalize an integer or semantic version for headings and filenames."""
    value = str(version).strip()
    if value.lower().startswith("v"):
        value = value[1:]
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", value):
        raise CouncilError(f"invalid version identifier: {version}")
    return value


def version_label(version: int | str) -> str:
    return f"v{normalize_version(version)}"


def core_feature_from_section(section: str, version: int | str) -> str:
    """Derive the core-feature title from the roadmap heading for one version."""
    normalized = normalize_version(version)
    version_re = re.compile(rf"(?i)(?:\bversion\s*|\bv)?{re.escape(normalized)}\b")
    for line in section.splitlines():
        if not re.match(r"^#{1,6}\s+", line):
            continue
        heading = re.sub(r"^#{1,6}\s+", "", line).strip()
        match = version_re.search(heading)
        if not match:
            continue
        feature = heading[match.end():].strip(" \t—–-:|")
        feature = re.sub(
            r"^/\s*\d+(?:\.\d+){1,2}\s*[—–\-:|]*\s*",
            "",
            feature,
        )
        feature = re.sub(r"\s*\((?:v?\d+(?:\.\d+){1,2})\)\s*$", "", feature).strip()
        if feature:
            return feature
    return "Roadmap Iteration"


def iteration_filename(version: int | str, core_feature: str) -> str:
    return f"{normalize_version(version)}-{slugify(core_feature)}.md"


def _find_existing_iteration(iterations_dir: Path, version: int | str) -> Path | None:
    token = normalize_version(version).lower() + "-"
    if not iterations_dir.exists():
        return None
    matches = sorted(
        path for path in iterations_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".md"
        and path.name.lower() != "readme.md"
        and path.name.lower().startswith(token)
    )
    if len(matches) > 1:
        raise CouncilError(
            f"multiple iteration notes found for {version_label(version)}: "
            + ", ".join(path.name for path in matches)
        )
    return matches[0] if matches else None


def iteration_record_prompt(version: int | str, core_feature: str, roadmap_section: str,
                            plan_review: str, research: str, literature: str,
                            implementation: str, assurance: str,
                            validation_summary: str = "") -> str:
    label = version_label(version)
    return f"""You are the configured Codex CLI literature reviewer and technical-document drafter.
Create one concise, evidence-grounded iteration note for {label}. The format is modeled on mature
versioned engineering iteration notes: it must connect roadmap intent, literature, the core feature,
implementation evidence, verification, limitations, and next work. Remain project-neutral.

Return Markdown only, using exactly this top-level structure:
# {label} — {core_feature}
<one short opening summary>
## Roadmap alignment
## Literature review
## Core feature
## Items
## Security and quality assurance
## Verification
## Acceptance criteria — status
## Honest scope
## Files changed
## Next
## References

Rules:
- Use [DONE], [PARTIAL], [RESEARCH], or [DEFERRED] item markers honestly.
- Never invent citations, test counts, filenames, metrics, or completed work.
- Keep direct evidence separate from inference.
- Literature claims require a stable URL, DOI, or an explicit UNVERIFIED marker.
- If a stage has no artifact, say so and mark it incomplete rather than filling the gap.
- The "Core feature" section must state the single principal capability for this version.
- The "Files changed" section may list only paths supported by the supplied implementation/assurance artifacts.
- Do not claim merge, release, deployment, or production readiness.

ROADMAP SECTION:
{roadmap_section}

ROADMAP PLAN REVIEW:
{plan_review or 'No plan-review artifact is available for this version.'}

RESEARCH:
{research or 'No current research artifact is available for this version.'}

LITERATURE REVIEW:
{literature or 'No literature-review artifact is available for this version.'}

IMPLEMENTATION:
{implementation or 'No implementation artifact is available for this version.'}

ASSURANCE:
{assurance or 'No assurance artifact is available for this version.'}

SUPERVISOR-CAPTURED VALIDATION:
{validation_summary or 'No per-version validation result is available yet.'}
"""


def _iteration_block(content: str, version: int | str) -> str:
    label = version_label(version)
    start = f"<!-- HERMES-LEGION-COMMANDER ITERATION {label} START -->"
    end = f"<!-- HERMES-LEGION-COMMANDER ITERATION {label} END -->"
    return f"{start}\n{content.strip()}\n{end}"


def update_iteration_index(iterations_dir: Path, version: int | str, core_feature: str,
                           filename: str) -> Path:
    """Create or extend docs/iterations/README.md without disturbing existing rows."""
    iterations_dir.mkdir(parents=True, exist_ok=True)
    index = iterations_dir / "README.md"
    link = f"[{filename}]({filename})"
    row = f"| {normalize_version(version)} | {core_feature} | {link} |"
    if not index.exists():
        index.write_text(
            "# Iteration Notes\n\n"
            "Versioned records connect roadmap scope, literature, implementation, assurance, and verification.\n\n"
            "| Iteration | Core feature | Note |\n"
            "|---|---|---|\n"
            f"{row}\n",
            encoding="utf-8",
        )
        return index
    text = index.read_text(encoding="utf-8")
    if f"]({filename})" in text:
        return index
    if "| Iteration |" not in text and "| Version |" not in text:
        text = text.rstrip() + (
            "\n\n## Versioned iteration records\n\n"
            "| Iteration | Core feature | Note |\n"
            "|---|---|---|\n"
        )
    index.write_text(text.rstrip() + "\n" + row + "\n", encoding="utf-8")
    return index


def write_iteration_document(config: Config, checkout: Path, version: int | str,
                             core_feature: str, content: str) -> Path:
    """Write an idempotent version note inside an isolated target checkout."""
    iterations_dir = checkout / config.iterations_dir
    iterations_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_existing_iteration(iterations_dir, version)
    path = existing or iterations_dir / iteration_filename(version, core_feature)
    block = _iteration_block(content, version)
    start = f"<!-- HERMES-LEGION-COMMANDER ITERATION {version_label(version)} START -->"
    end = f"<!-- HERMES-LEGION-COMMANDER ITERATION {version_label(version)} END -->"
    if path.exists():
        prior = path.read_text(encoding="utf-8")
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        if pattern.search(prior):
            merged = pattern.sub(block, prior)
        else:
            merged = prior.rstrip() + "\n\n" + block + "\n"
    else:
        merged = block + "\n"
    path.write_text(merged, encoding="utf-8")
    update_iteration_index(iterations_dir, version, core_feature, path.name)
    return path


def append_iteration_verification(path: Path, version: int | str, checks: tuple[tuple[str, ...], ...],
                                  passed: bool) -> None:
    """Record deterministic supervisor check status without asking a model to invent it."""
    start = f"<!-- HERMES-LEGION-COMMANDER VERIFY {version_label(version)} START -->"
    end = f"<!-- HERMES-LEGION-COMMANDER VERIFY {version_label(version)} END -->"
    status = "PASSED" if passed else "FAILED"
    commands = "\n".join(f"- `{' '.join(cmd)}`" for cmd in checks) or "- No checks configured."
    block = f"{start}\n\n## Supervisor verification\n\n**Status:** {status}\n\n{commands}\n\n{end}"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pdf_text(path: Path, max_chars: int) -> str:
    """Extract bounded PDF text using pypdf, then pdftotext as a CLI fallback."""
    text = ""
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) >= max_chars:
                break
        text = "\n\n".join(parts)
    except (ImportError, OSError, ValueError):
        binary = shutil.which("pdftotext")
        if binary:
            completed = subprocess.run([binary, "-layout", str(path), "-"], text=True,
                                       capture_output=True, check=False, timeout=180)
            if completed.returncode == 0:
                text = completed.stdout
    if not text.strip():
        raise CouncilError(f"could not extract text from {path}; install pypdf or pdftotext")
    return text[:max_chars]


def load_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda x: (x.get("title", ""), x.get("sha256", "")))
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in ordered), encoding="utf-8")


def path_reference(path: Path, repo: Path) -> str:
    """Use a repository-relative reference when possible, otherwise an absolute path."""
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def resolve_reference(reference: str, repo: Path) -> Path:
    path = Path(reference).expanduser()
    return path if path.is_absolute() else repo / path


def library_context(config: Config, max_chars: int = 30000) -> str:
    rows = load_manifest(config.library_manifest)
    if not rows:
        return "No reviewed PDFs are currently indexed."
    chunks = ["Shared project literature library. Raw PDFs are under research/library/pdfs/.\n"]
    for row in rows:
        chunks.append(f"- {row.get('title')} | PDF: {row.get('pdf')} | Review: {row.get('review')} | SHA256: {row.get('sha256')}\n")
        review = resolve_reference(str(row.get("review", "")), config.repo)
        if review.exists():
            chunks.append(review.read_text(encoding="utf-8")[:6000] + "\n")
        if sum(map(len, chunks)) >= max_chars:
            chunks.append("[Library context truncated; inspect the referenced files directly.]\n")
            break
    return "".join(chunks)[:max_chars]


def literature_prompt(pdf: Path, digest: str, extracted: str) -> str:
    return f"""You are Claude, the project literature-review specialist. Review one paper only.
PDF filename: {pdf.name}
SHA256: {digest}

Produce a rigorous Markdown review with this exact structure:
# <Canonical paper title>
- Source file
- SHA256
- Authors
- Year and venue
- DOI / stable URL (write UNVERIFIED when absent)
- project roadmap topics

## Research question
## Methodology and system assumptions
## Architecture / algorithm
## Experimental setup, datasets, baselines, and metrics
## Principal results with page/table/figure locations
## Limitations and threats to validity
## Reproducibility and available artifacts
## Security, safety, and interoperability implications
## Direct relevance to the target project
## Concrete implementation or benchmark actions
## Claims requiring independent verification
## Citation record

Never invent metadata, metrics, URLs, or page references. Distinguish the authors' claims from your
inference. This review will be shared through the common Legion context with Codex CLI and Claude Code.

EXTRACTED PDF TEXT:
{extracted}
"""


def literature_audit_prompt(review: str, pdf_name: str) -> str:
    return f"""You are Codex auditing a Claude literature review against the cited paper.
Check bibliographic metadata, citations, numerical claims, page/table/figure references, and whether
the project recommendations follow from the paper. Use live search only to verify identity, DOI,
publication venue, official code, and dataset links. Never replace uncertainty with invention.
Return a compact PASS/FAIL/UNCERTAIN claim audit and corrections.

PDF: {pdf_name}

CLAUDE REVIEW:
{review}
"""


def literature_proofread_prompt(review: str, audit: str, pdf_name: str) -> str:
    return f"""You are Claude independently proofreading a project literature review.
Focus on technical interpretation, security and safety consequences, interoperability assumptions,
unsupported extrapolation, and reproducibility. Reconcile the Claude review with Codex's audit.
Return required corrections and an ACCEPT/REVISE/REJECT decision. Do not invent evidence.

PDF: {pdf_name}

CLAUDE REVIEW:
{review}

CODEX AUDIT:
{audit}
"""


def review_library(config: Config, dry_run: bool = False, force: bool = False, validation: str | None = None) -> list[Path]:
    validation = validation or config.literature_validation
    if validation not in {"economy", "balanced", "quality"}:
        raise CouncilError(f"invalid literature validation mode: {validation}")
    config.pdf_dir.mkdir(parents=True, exist_ok=True)
    config.review_dir.mkdir(parents=True, exist_ok=True)
    existing = {row.get("sha256"): row for row in load_manifest(config.library_manifest)}
    rows = list(existing.values())
    outputs: list[Path] = []
    for pdf in sorted(config.pdf_dir.glob("*.pdf")):
        digest = sha256_file(pdf)
        if digest in existing and not force:
            continue
        stem = f"{slugify(pdf.stem)}__{digest[:12]}__claude-literature-review.md"
        review_path = config.review_dir / stem
        run_dir = config.state_dir / "literature" / digest[:12]
        run_dir.mkdir(parents=True, exist_ok=True)
        if not (run_dir / "job.json").exists():
            _atomic_json(run_dir / "job.json", {
                "type": "literature", "run_id": digest[:12], "pdf": str(pdf),
                "sha256": digest, "validation": validation, "dry_run": dry_run,
            })
        extracted = f"[DRY RUN PDF TEXT for {pdf.name}]" if dry_run else extract_pdf_text(pdf, config.max_pdf_chars)
        output = run_agent(config, config.literature_reviewer, literature_prompt(pdf, digest, extracted),
                           config.repo, run_dir / "01-claude-review", dry_run)
        audit = ""
        proofread = ""
        if validation in {"balanced", "quality"}:
            audit = run_agent(config, "gpt", literature_audit_prompt(output, pdf.name),
                              config.repo, run_dir / "02-codex-audit", dry_run)
        if validation == "quality":
            proofread = run_agent(config, "claude", literature_proofread_prompt(output, audit, pdf.name),
                                  config.repo, run_dir / "03-claude-proofread", dry_run)
        assembled = output.rstrip() + "\n\n---\n\n## Cross-validation\n\n"
        assembled += f"**Validation mode:** {validation}\n\n"
        if audit:
            assembled += "### Codex citation and metadata audit\n\n" + audit.rstrip() + "\n\n"
        if proofread:
            assembled += "### Claude technical and security proofread\n\n" + proofread.rstrip() + "\n"
        if not audit and not proofread:
            assembled += "No independent model audit was requested.\n"
        review_path.write_text(assembled, encoding="utf-8")
        rel_pdf = path_reference(pdf, config.repo)
        rel_review = path_reference(review_path, config.repo)
        row = {"title": pdf.stem, "pdf": rel_pdf, "review": rel_review, "sha256": digest,
               "reviewer": config.literature_reviewer, "validation": validation,
               "reviewed_at": dt.datetime.now(UTC).isoformat()}
        rows = [r for r in rows if r.get("sha256") != digest] + [row]
        outputs.append(review_path)
    write_manifest(config.library_manifest, rows)
    return outputs


def roadmap_plan_review_prompt(roadmap: str, question: str = "", version: str = "") -> str:
    target = f"version {version}" if version else "the requested research scope"
    return f"""You are the configured Codex CLI roadmap plan reviewer. Before any research begins,
review the discovered roadmap context for {target}. Inspect repository structure read-only when useful.
Produce a concise research brief containing: roadmap objective, in-scope requirements, explicit exclusions,
dependencies, unresolved assumptions, security/interoperability concerns, evidence gaps, prioritized research
questions, recommended source types, and acceptance criteria for relevance. Do not perform the research,
modify files, implement code, or invent requirements. The subsequent researcher must stay within this brief.

USER EMPHASIS:
{question or 'None'}

ROADMAP CONTEXT:
{roadmap}
"""


def research_prompt(config: Config, as_of: dt.date, lookback: int, max_findings: int, question: str, plan_review: str = "") -> str:
    since = as_of - dt.timedelta(days=lookback)
    topics = "\n".join(f"- {x}" for x in config.topics)
    return f"""You are Codex, the primary evidence researcher for the target project.
Research window: {since.isoformat()} through {as_of.isoformat()}.
User question or emphasis: {question or 'General project research intelligence update'}

MANDATORY ROADMAP REVIEW BRIEF:
{plan_review or 'No roadmap review brief was supplied.'}

Topics:
{topics}

Produce at most {max_findings} material findings. Use live search and prioritize primary sources:
original papers, official standards, upstream repositories, and official project documentation.
For every factual claim provide a directly supporting citation with stable URL or DOI. Verify title,
authors, publication date, venue, repository status, dataset availability, and claimed metrics.
Deduplicate preprint/published versions. Separate evidence from inference. State search gaps and
contradictory evidence. Never invent a citation. Compare findings to the target project architecture and
recommend concrete benchmarks, roadmap changes, or threat-model updates.

The shared literature library below is trusted only as a reading aid; independently verify material claims.\n\nSHARED LITERATURE LIBRARY:\n{library_context(config)}\n\nReturn a complete Markdown research draft with a normalized bibliography.\n"""


def citation_audit_prompt(draft: str) -> str:
    return f"""You are Claude acting as an adversarial citation auditor and literature reviewer.
Do not accept the draft at face value. Check internal consistency, unsupported claims, suspicious or
incomplete citations, duplicated papers, mismatched dates, metrics without provenance, and whether
recommendations actually follow from evidence. Preserve useful content while producing:
1. a claim-by-claim audit table (PASS/FAIL/UNCERTAIN),
2. required citation corrections,
3. a clearer project integration/action plan,
4. a revised report draft.
Do not fabricate replacements; mark anything you cannot verify as requiring verification.

CODEX RESEARCH DRAFT:
{draft}
"""


def proofread_prompt(codex_draft: str, claude_audit: str) -> str:
    return f"""You are Claude, the independent final reviewer for project research assurance.
Proofread for technical correctness, architecture fit, security implications, overclaiming, citation
quality, and logical gaps. Cross-check disagreements between Codex research and Claude literature review. Classify every major
recommendation as ACCEPT, REVISE, or REJECT with rationale. Pay special attention to trust boundaries,
Byzantine behavior, unsafe autonomy assumptions, interoperability failure modes, and reproducibility.
Do not invent evidence. Return a concise review plus explicit corrections required before publication.

CODEX RESEARCH DRAFT:
{codex_draft}

CODEX AUDIT AND REVISION:
{claude_audit}
"""


def reconciliation_prompt(draft: str, audit: str, review: str, as_of: dt.date) -> str:
    return f"""You are Codex, citation owner for the final project research report dated {as_of.isoformat()}.
Reconcile the original Codex research, Claude citation/literature audit, and Claude security/quality review. Remove or
clearly label claims that remain unverified. Do not preserve a citation merely because it appeared in
the original. The final report must include: executive summary, verified high-priority findings,
claim-evidence table, project subsystem impact, security implications, reproducible baselines, concrete
roadmap actions, rejected/uncertain claims, search gaps, and normalized bibliography. Each material
claim must have a nearby citation. Distinguish direct evidence from your inference.

ORIGINAL:
{draft}

CODEX AUDIT:
{audit}

CLAUDE REVIEW:
{review}
"""



DANGEROUS_TERMS = (
    "delete", "drop database", "rotate key", "revoke", "credential", "secret", "production",
    "deploy", "release", "publish", "force push", "rewrite history", "migration", "firmware",
    "flight controller", "arming", "failsafe", "kill switch", "root", "sudo", "network policy",
    "authentication", "authorization", "cryptography", "mavlink command", "safety boundary",
)


def assess_change_risk(task: str) -> tuple[bool, list[str]]:
    text = task.lower()
    reasons = [term for term in DANGEROUS_TERMS if term in text]
    return bool(reasons), reasons


def approval_paths(run_dir: Path, phase: str) -> tuple[Path, Path]:
    safe_phase = slugify(phase)
    return run_dir / f"approval-request-{safe_phase}.json", run_dir / f"approval-grant-{safe_phase}.json"


def require_approval(run_dir: Path, run_id: str, phase: str, reason: str, details: dict[str, Any]) -> None:
    request, grant = approval_paths(run_dir, phase)
    if grant.exists():
        approved = json.loads(grant.read_text(encoding="utf-8"))
        if approved.get("run_id") == run_id and approved.get("phase") == phase and approved.get("approved") is True:
            return
    payload = {"run_id": run_id, "phase": phase, "reason": reason, "details": details,
               "requested_at": dt.datetime.now(UTC).isoformat(),
               "approval_command": f"hermes-legion-commander council --config <config> approve --run-id {run_id} --phase {phase}"}
    _atomic_json(request, payload)
    raise ApprovalRequired(run_id, reason, request)


def approve_run(config: Config, run_id: str, phase: str, note: str) -> Path:
    candidates = list(config.state_dir.glob(f"*-{run_id}"))
    if not candidates:
        raise CouncilError(f"run not found: {run_id}")
    run_dir = candidates[0]
    request, grant = approval_paths(run_dir, phase)
    if not request.exists():
        raise CouncilError(f"no pending approval request for {run_id}")
    req = json.loads(request.read_text(encoding="utf-8"))
    if req.get("phase") != phase:
        raise CouncilError(f"pending phase is {req.get('phase')}, not {phase}")
    _atomic_json(grant, {"run_id": run_id, "phase": phase, "approved": True, "note": note,
                         "approved_at": dt.datetime.now(UTC).isoformat()})
    return grant


def diff_size(cwd: Path) -> tuple[int, int, str]:
    stat = git(cwd, "diff", "--numstat", check=False)
    files = 0
    lines = 0
    for line in stat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files += 1
            for value in parts[:2]:
                if value.isdigit():
                    lines += int(value)
    return files, lines, stat


def make_run_id() -> str:
    return dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

def run_research(config: Config, as_of: dt.date, lookback: int, max_findings: int,
                 question: str, dry_run: bool, run_id: str | None = None,
                 wait_for_quota: bool | None = None) -> Path:
    stamp = run_id or make_run_id()
    run_dir = config.state_dir / f"research-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(run_dir / "job.json", {"type": "research", "run_id": stamp, "as_of": as_of.isoformat(),
                 "lookback": lookback, "max_findings": max_findings, "question": question, "dry_run": dry_run})
    _, roadmap, roadmap_files = roadmap_context(config)
    plan_review = run_agent(config, role_agent(config, "roadmap_plan_reviewer"),
                            roadmap_plan_review_prompt(roadmap, question), config.repo,
                            run_dir / "00-roadmap-plan-review", dry_run, wait_for_quota)
    draft = run_agent(config, role_agent(config, "researcher"),
                      research_prompt(config, as_of, lookback, max_findings, question, plan_review),
                      config.repo, run_dir / "01-research", dry_run, wait_for_quota)
    audit = run_agent(config, role_agent(config, "literature_reviewer"), citation_audit_prompt(draft), config.repo,
                      run_dir / "02-citation-audit", dry_run, wait_for_quota)
    review = run_agent(config, role_agent(config, "security_assurance"), proofread_prompt(draft, audit), config.repo,
                       run_dir / "03-proofread", dry_run)
    final = run_agent(config, role_agent(config, "researcher"), reconciliation_prompt(draft, audit, review, as_of), config.repo,
                      run_dir / "04-reconcile", dry_run, wait_for_quota)
    config.research_dir.mkdir(parents=True, exist_ok=True)
    report = config.research_dir / f"project-council-research-{as_of.isoformat()}.md"
    report.write_text(final, encoding="utf-8")
    metadata = {"type": "research", "created_at": stamp, "report": str(report),
                "agents": [config.roles["roadmap_plan_reviewer"], config.roles["researcher"], config.roles["literature_reviewer"], config.roles["security_assurance"], config.roles["researcher"]], "roadmap_sources": [str(p) for p in roadmap_files], "dry_run": dry_run}
    (run_dir / "result.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return report


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if check and p.returncode:
        raise CouncilError((p.stderr or p.stdout).strip())
    return p.stdout.strip()


def code_prompt(task: str, research_packet: str, shared_library: str = "") -> str:
    return f"""You are GPT, the project rapid prototyper and document drafter.
Implement the task in the current isolated Git worktree. Inspect architecture and tests first. Build a
minimal but complete prototype, add focused tests, and update technical documentation. Do not merge,
tag, release, alter credentials, or weaken safety/security gates. Leave intended changes uncommitted.

TASK:
{task}

RESEARCH PACKET (advisory; verify against repository reality):\n{research_packet or 'None'}\n\nSHARED LITERATURE INDEX AND CLAUDE REVIEWS:\n{shared_library or 'Not loaded for this task. The files remain available under research/library/.'}\n"""


def polish_prompt(task: str) -> str:
    return f"""You are Claude, the project code, architecture, and security polisher.
Review the existing GPT candidate in this worktree. Inspect its diff and tests. Correct defects,
improve maintainability, tighten trust boundaries and input validation, remove unsafe assumptions,
and strengthen tests/documentation. Preserve useful prototype scope. Do not merge, tag, publish, or
release. Leave intended changes uncommitted and finish with residual risks.

ORIGINAL TASK:
{task}
"""


def run_checks(config: Config, cwd: Path, run_dir: Path, dry_run: bool) -> bool:
    for i, cmd in enumerate(config.checks, 1):
        stage = run_dir / f"check-{i}"
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
        if dry_run:
            continue
        p = subprocess.run(list(cmd), cwd=cwd, text=True, capture_output=True, check=False)
        (stage / "stdout.txt").write_text(p.stdout or "", encoding="utf-8")
        (stage / "stderr.txt").write_text(p.stderr or "", encoding="utf-8")
        if p.returncode:
            return False
    return True


def run_code(config: Config, task: str, with_research: bool, dry_run: bool, budget: str | None = None,
             run_id: str | None = None, wait_for_quota: bool | None = None) -> Path:
    route = route_task(task, budget or config.default_budget)
    if not (config.repo / ".git").exists():
        raise CouncilError(f"not a Git checkout: {config.repo}")
    stamp = run_id or make_run_id()
    run_dir = config.state_dir / f"code-{stamp}"
    worktree = run_dir / "worktree"
    branch = f"hermes-legion/council/{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_path = run_dir / "job.json"
    if not job_path.exists():
        if git(config.repo, "status", "--porcelain"):
            raise CouncilError("repository must be clean before new code council execution")
        _atomic_json(job_path, {"type": "code", "run_id": stamp, "task": task,
                     "with_research": with_research, "dry_run": dry_run, "budget": budget})
    dangerous, reasons = assess_change_risk(task)
    if dangerous:
        require_approval(run_dir, stamp, "dangerous-intent",
                         "task changes a security, safety, deployment, credential, or destructive boundary",
                         {"matched_terms": reasons, "task": task})
    if not worktree.exists():
        git(config.repo, "worktree", "add", "-b", branch, str(worktree), str(base_info.get("base_ref", "HEAD")))
    research_packet = ""
    if with_research:
        _, roadmap, _ = roadmap_context(config)
        plan_review = run_agent(config, role_agent(config, "roadmap_plan_reviewer"),
            roadmap_plan_review_prompt(roadmap, task), config.repo,
            run_dir / "00-roadmap-plan-review", dry_run, wait_for_quota)
        research_packet = run_agent(config, role_agent(config, "researcher"),
            research_prompt(config, dt.date.today(), config.lookback_days, 12, task, plan_review),
            config.repo, run_dir / "01-task-research", dry_run, wait_for_quota)
    shared = library_context(config) if route.include_library else ""
    run_agent(config, "gpt", code_prompt(task, research_packet, shared), worktree,
              run_dir / "01-gpt-prototype", dry_run, wait_for_quota)
    files, lines, numstat = diff_size(worktree)
    if files >= config.massive_files or lines >= config.massive_lines:
        require_approval(run_dir, stamp, "massive-diff",
                         "prototype exceeded the configured human-review threshold",
                         {"changed_files": files, "changed_lines": lines, "numstat": numstat,
                          "threshold_files": config.massive_files, "threshold_lines": config.massive_lines})
    if route.reviewer == "claude":
        run_agent(config, "claude", polish_prompt(task), worktree,
                  run_dir / "02-claude-polish", dry_run, wait_for_quota)
    passed = run_checks(config, worktree, run_dir / "03-checks", dry_run)
    diff = git(worktree, "diff", "--stat", check=False)
    result = {"type": "code", "run_id": stamp, "branch": branch, "worktree": str(worktree),
              "checks_passed": passed, "diff_stat": diff, "dry_run": dry_run,
              "route": dataclasses.asdict(route), "approval_required": dangerous or files >= config.massive_files or lines >= config.massive_lines}
    _atomic_json(run_dir / "result.json", result)
    if not passed:
        raise CouncilError(f"candidate checks failed; inspect {run_dir / '03-checks'}")
    return run_dir


def resume_run(config: Config, run_id: str, wait_for_quota: bool | None = None) -> Path:
    candidates = list(config.state_dir.glob(f"*-{run_id}"))
    if not candidates:
        raise CouncilError(f"run not found: {run_id}")
    job = json.loads((candidates[0] / "job.json").read_text(encoding="utf-8"))
    if job["type"] == "research":
        return run_research(config, dt.date.fromisoformat(job["as_of"]), int(job["lookback"]),
                            int(job["max_findings"]), job.get("question", ""), bool(job.get("dry_run")),
                            run_id, wait_for_quota)
    if job["type"] == "code":
        return run_code(config, job["task"], bool(job.get("with_research")), bool(job.get("dry_run")),
                        job.get("budget"), run_id, wait_for_quota)
    if job["type"] == "campaign":
        return run_campaign(config, int(job["from_version"]), int(job["to_version"]), bool(job.get("dry_run")), run_id, wait_for_quota, job.get("strategy", "full"))
    if job["type"] == "bootstrap":
        return run_bootstrap(config, bool(job.get("dry_run")), run_id, wait_for_quota)
    raise CouncilError(f"unsupported resumable job type: {job['type']}")



BOOTSTRAP_VERSION = "0.0.1"


def role_agent(config: Config, role: str) -> str:
    try:
        return config.roles[role]
    except KeyError as exc:
        raise CouncilError(f"unconfigured council role: {role}") from exc


def _display_relative(path: Path, root: Path) -> str:
    """Relative path for display, falling back to the absolute path when the
    file is outside ``root`` (e.g. an explicit roadmap given outside the repo)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def discover_roadmap_files(config: Config) -> tuple[Path, ...]:
    """Discover Markdown roadmap files for the target repository.

    The configured/overridden ``roadmap_path`` is the authoritative primary
    roadmap: when it resolves to an existing file it is always included first,
    even if it lives outside ``docs/`` or is not named ``*roadmap*.md``. Any
    additional ``docs/*roadmap*.md`` files follow as secondary context.
    """
    docs = (config.repo / "docs").resolve()
    matches = sorted(
        (path.resolve() for path in docs.rglob("*") if path.is_file() and path.suffix.lower() == ".md" and "roadmap" in path.name.lower()),
        key=lambda path: str(path.relative_to(docs)).lower(),
    ) if docs.is_dir() else []
    preferred = config.roadmap_path
    if not preferred.is_absolute():
        preferred = (config.repo / preferred).resolve()
    else:
        preferred = preferred.resolve()
    canonical = (docs / "roadmap.md").resolve()
    ordered: list[Path] = []
    # An explicitly configured roadmap that exists is authoritative, regardless
    # of location or naming convention.
    if preferred.is_file():
        ordered.append(preferred)
    for candidate in (canonical, *matches):
        if candidate in matches and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def roadmap_context(config: Config) -> tuple[Path, str, tuple[Path, ...]]:
    """Return the primary roadmap plus combined context from every discovered roadmap."""
    files = discover_roadmap_files(config)
    if not files:
        raise CouncilError(f"no *roadmap*.md file found under {(config.repo / 'docs').resolve()}")
    chunks = []
    for path in files:
        relative = _display_relative(path, config.repo)
        content = path.read_text(encoding="utf-8")
        scoped = implementation_section(content)
        scope_note = (
            f"implementation section: {scoped.heading}"
            if scoped.found else "full-document fallback: no version-by-version implementation heading"
        )
        chunks.append(
            f"<!-- ROADMAP SOURCE: {relative}; {scope_note} -->\n{scoped.text}"
        )
    return files[0], "\n\n".join(chunks), files




def roadmap_preflight(config: Config, repo_override: Path | None = None, preview_chars: int = 800, verbose: bool = False) -> dict[str, Any]:
    """Inspect roadmap files using only the local filesystem. Never invokes a worker CLI or model API."""
    if preview_chars < 0:
        raise CouncilError("preview chars must be non-negative")
    effective = config if repo_override is None else dataclasses.replace(config, repo=repo_override.expanduser().resolve())
    repo = effective.repo.resolve()
    docs = (repo / "docs").resolve()
    if not repo.is_dir():
        raise CouncilError(f"target repository directory does not exist: {repo}")
    files = discover_roadmap_files(effective)
    if not files:
        raise CouncilError(f"no roadmap found: pass --roadmap <file.md> or add docs/*roadmap*.md under {repo}")
    entries: list[dict[str, Any]] = []
    heading_re = re.compile(r"(?im)^#{1,6}\s+(.+)$")
    for path in files:
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CouncilError(f"roadmap is not valid UTF-8: {path}: {exc}") from exc
        document_headings = [m.group(1).strip() for m in heading_re.finditer(content)]
        scoped, parsed_entries = parse_version_entries(content)
        scoped_headings = [m.group(1).strip() for m in heading_re.finditer(scoped.text)]
        entries.append({
            "path": str(path),
            "relative_path": _display_relative(path, repo),
            "selected_primary": path == files[0],
            "readable_utf8": True,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "roadmap_scope": "version-by-version-implementation" if scoped.found else "full-document-fallback",
            "implementation_section_heading": scoped.heading,
            "implementation_section_found": scoped.found,
            "version_entry_count": len(parsed_entries),
            "campaign_version_count": len(campaign_versions(parsed_entries)),
            "campaign_version_range": (
                [min(campaign_versions(parsed_entries)), max(campaign_versions(parsed_entries))]
                if parsed_entries else []
            ),
            "phase_sample": (
                version_keys(parsed_entries)
                if len(parsed_entries) <= 6
                else version_keys(parsed_entries)[:3] + ["..."] + version_keys(parsed_entries)[-3:]
            ),
            "special_versions_detected": [
                entry.version for entry in parsed_entries if not entry.version.isdigit()
            ][:200],
            "preview": scoped.text[:preview_chars] if preview_chars else "",
            **({
                "headings": scoped_headings[:200],
                "document_headings": document_headings[:200],
                "versions_detected": version_keys(parsed_entries)[:200],
                "campaign_versions_detected": campaign_versions(parsed_entries)[:200],
                "release_versions_detected": release_versions(parsed_entries)[:200],
                "version_entries": [
                    {
                        "version": entry.version,
                        "release_version": entry.release_version,
                        "title": entry.title,
                    }
                    for entry in parsed_entries[:200]
                ],
            } if verbose else {}),
        })
    return {
        "mode": "local-filesystem-only",
        "worker_cli_invoked": False,
        "model_or_api_calls": 0,
        "repo": str(repo),
        "docs_dir": str(docs),
        "roadmap_count": len(entries),
        "primary_roadmap": str(files[0]),
        "roadmaps": entries,
    }

def roadmap_template(project_name: str) -> str:
    today = dt.date.today().isoformat()
    return f"""# {project_name} Roadmap

> Managed with Hermes Legion Commander. Roadmap changes after initialization require explicit human approval.

## Versioning policy

- Semantic versions increment one release at a time.
- Initial planned release: `v0.0.1`.
- Every release must pass research, literature review, prototyping, code-quality review, security assurance, and deterministic verification.
- No agent may merge, publish, deploy, tag, or release without repository-owner authorization.

## Role pipeline

1. **Researcher** — gathers current evidence, standards, repositories, and project-relevant findings.
2. **Literature reviewer** — reviews supplied PDFs and validates citations and claims.
3. **Prototyper** — converts the approved scope into implementation, tests, and concise documentation.
4. **Code polisher** — improves correctness, maintainability, architecture, and interoperability.
5. **Security assurance** — examines trust boundaries, abuse cases, failure modes, and residual risk.

## v0.0.1 — Initial supervised prototype

**Status:** Pending human approval  
**Created:** {today}

### Objective

Establish the smallest useful, testable, and security-reviewed first increment of the project.

### Research findings

_Pending researcher output._

### Literature review

_Pending literature-review output._

### Prototype scope

_Pending prototyper output._

### Code-quality and architecture review

_Pending code-polisher output._

### Security assurance

_Pending security-assurance output._

### Acceptance criteria

- [ ] Scope is bounded and implementable as one increment.
- [ ] Claims and citations are traceable to primary or authoritative sources.
- [ ] Prototype changes include focused tests and concise documentation.
- [ ] Code-quality and architecture findings are resolved or documented.
- [ ] Security findings include severity, evidence, mitigation, and residual risk.
- [ ] Repository owner approves the finalized v0.0.1 roadmap content.

### Open risks and deferred work

_Pending council synthesis._
"""


def ensure_bootstrap_roadmap(config: Config) -> tuple[Path, bool]:
    """Use an existing discovered roadmap or create docs/roadmap.md at v0.0.1."""
    discovered = discover_roadmap_files(config)
    if discovered:
        return discovered[0], False
    path = (config.repo / "docs" / "roadmap.md").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(roadmap_template(config.repo.name or "Project"), encoding="utf-8")
    return path, True


def bootstrap_research_prompt(roadmap: str, library: str, plan_review: str) -> str:
    return f"""You are the configured Codex CLI researcher. Define the evidence-based v{BOOTSTRAP_VERSION}
initial roadmap increment for this repository. Inspect the repository read-only. Gather current primary
or authoritative sources relevant to the project, distinguish evidence from inference, and return a
concise Markdown research packet with objectives, findings, citations, constraints, risks, and a bounded
first-release recommendation. Do not edit files, implement code, merge, publish, or deploy.

ROADMAP SKELETON:
{roadmap}

MANDATORY ROADMAP REVIEW BRIEF:
{plan_review}

SHARED LITERATURE INDEX:
{library}
"""


def bootstrap_literature_prompt(roadmap: str, research: str, library: str) -> str:
    return f"""You are the configured Codex CLI literature reviewer. Review the shared PDF library and the
research packet for v{BOOTSTRAP_VERSION}. Validate citations and claims, identify contradictions and gaps,
and produce a concise literature review tied to the proposed first increment. Do not invent metadata,
page references, metrics, URLs, or conclusions. Do not edit repository files.

ROADMAP SKELETON:
{roadmap}

RESEARCH PACKET:
{research}

SHARED LITERATURE INDEX:
{library}
"""


def bootstrap_prototype_prompt(roadmap: str, research: str, literature: str) -> str:
    return f"""You are the configured Codex CLI prototyper. Inspect the repository and turn the evidence into
a bounded v{BOOTSTRAP_VERSION} prototype plan. In the isolated worktree, implement only changes that are
clearly justified, small enough for the first increment, and covered by focused tests and concise docs.
Do not merge, push, tag, publish, deploy, or release. Finish with changed files, tests, discovered bugs,
and residual risks.

ROADMAP SKELETON:
{roadmap}

RESEARCH:
{research}

LITERATURE REVIEW:
{literature}
"""


def bootstrap_polish_prompt(roadmap: str) -> str:
    return f"""You are the configured Claude Code polisher. Review the current isolated v{BOOTSTRAP_VERSION}
worktree. Improve correctness, maintainability, architecture, interoperability, tests, and documentation
without expanding scope. Leave changes uncommitted and do not merge, publish, deploy, tag, or release.
Return a concise list of defects found, corrections made, verification, and residual concerns.

ROADMAP SKELETON:
{roadmap}
"""


def bootstrap_security_prompt(roadmap: str) -> str:
    return f"""You are the configured Claude Code security-assurance reviewer. Review the current isolated
v{BOOTSTRAP_VERSION} worktree, trust boundaries, input handling, authentication/authorization assumptions,
secrets, dependency risks, failure modes, and unsafe operations. Correct in-scope defects, add focused
security tests where justified, and report severity, evidence, mitigation, blockers, and residual risk.
Do not merge, publish, deploy, tag, or release.

{ADVERSARIAL_EVALUATOR_STANCE}

ROADMAP SKELETON:
{roadmap}
"""


def bootstrap_synthesis_prompt(roadmap: str, research: str, literature: str, prototype: str,
                               polish: str, security: str) -> str:
    return f"""Draft the final HUMAN-REVIEWED Markdown content for the v{BOOTSTRAP_VERSION} section only.
Use exactly these headings: Objective; Research findings; Literature review; Prototype scope; Code-quality
and architecture review; Security assurance; Acceptance criteria; Open risks and deferred work. Be concise,
evidence-based, and actionable. Include no unsupported claim. Do not edit files.

CURRENT SKELETON:
{roadmap}

RESEARCH:
{research}

LITERATURE:
{literature}

PROTOTYPE:
{prototype}

POLISH:
{polish}

SECURITY:
{security}
"""


def apply_bootstrap_roadmap(config: Config, run_dir: Path) -> Path:
    proposal = run_dir / "roadmap-proposals" / f"v{BOOTSTRAP_VERSION}.md"
    grant = run_dir / "approvals" / f"roadmap-init-v{BOOTSTRAP_VERSION}.grant.json"
    if not proposal.exists() or not grant.exists():
        raise CouncilError("bootstrap roadmap proposal or approval grant is missing")
    path, text = roadmap_for(config)
    start = re.search(r"(?im)^##\s+v0\.0\.1\b", text)
    if not start:
        raise CouncilError("roadmap skeleton has no v0.0.1 section")
    next_heading = re.search(r"(?im)^##\s+v\d+\.\d+\.\d+\b", text[start.end():])
    end = start.end() + next_heading.start() if next_heading else len(text)
    replacement = f"## v{BOOTSTRAP_VERSION} — Initial supervised prototype\n\n" + proposal.read_text(encoding="utf-8").strip() + "\n"
    path.write_text(text[:start.start()] + replacement + text[end:], encoding="utf-8")
    _atomic_json(run_dir / "roadmap-proposals" / f"v{BOOTSTRAP_VERSION}.applied.json", {
        "version": BOOTSTRAP_VERSION, "path": str(path), "applied_at": dt.datetime.now(UTC).isoformat()
    })
    return path


def run_bootstrap(config: Config, dry_run: bool, run_id: str | None = None,
                  wait_for_quota: bool | None = None) -> Path:
    if not (config.repo / ".git").exists():
        raise CouncilError(f"not a Git checkout: {config.repo}")
    stamp = run_id or f"bootstrap-{make_run_id()}"
    run_dir = config.state_dir / f"bootstrap-{stamp}"
    worktree = run_dir / "worktree"
    branch = f"hermes-legion/council-bootstrap/{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    roadmap_path, created = ensure_bootstrap_roadmap(config)
    _, roadmap, roadmap_files = roadmap_context(config)
    job = run_dir / "job.json"
    if not job.exists():
        if git(config.repo, "status", "--porcelain") and not created:
            raise CouncilError("repository must be clean before a new bootstrap run")
        _atomic_json(job, {"type": "bootstrap", "run_id": stamp, "dry_run": dry_run,
                          "roadmap_created": created, "roadmap": str(roadmap_path),
                          "roadmap_sources": [str(path) for path in roadmap_files]})
    if not worktree.exists():
        git(config.repo, "worktree", "add", "-b", branch, str(worktree), str(base_info.get("base_ref", "HEAD")))
    library = library_context(config)
    plan_review = run_agent(config, role_agent(config, "roadmap_plan_reviewer"),
                            roadmap_plan_review_prompt(roadmap, version=BOOTSTRAP_VERSION), config.repo,
                            run_dir / "00-roadmap-plan-review", dry_run, wait_for_quota)
    research = run_agent(config, role_agent(config, "researcher"), bootstrap_research_prompt(roadmap, library, plan_review),
                         config.repo, run_dir / "01-research", dry_run, wait_for_quota)
    literature = run_agent(config, role_agent(config, "literature_reviewer"),
                           bootstrap_literature_prompt(roadmap, research, library), config.repo,
                           run_dir / "02-literature-review", dry_run, wait_for_quota)
    prototype = run_agent(config, role_agent(config, "prototyper"),
                          bootstrap_prototype_prompt(roadmap, research, literature), worktree,
                          run_dir / "03-prototype", dry_run, wait_for_quota)
    polish = run_agent(config, role_agent(config, "code_polisher"), bootstrap_polish_prompt(roadmap),
                       worktree, run_dir / "04-code-polish", dry_run, wait_for_quota)
    security = run_agent(config, role_agent(config, "security_assurance"), bootstrap_security_prompt(roadmap),
                         worktree, run_dir / "05-security-assurance", dry_run, wait_for_quota)
    core_feature = core_feature_from_section(roadmap, BOOTSTRAP_VERSION)
    iteration = run_agent(
        config,
        role_agent(config, "literature_reviewer"),
        iteration_record_prompt(BOOTSTRAP_VERSION, core_feature, roadmap, plan_review, research,
                                literature, prototype, "\n\n".join((polish, security))),
        config.repo,
        run_dir / "06-iteration-record",
        dry_run,
        wait_for_quota,
    )
    iteration_path = write_iteration_document(config, worktree, BOOTSTRAP_VERSION, core_feature, iteration)
    files, lines, numstat = diff_size(worktree)
    if files >= config.massive_files or lines >= config.massive_lines:
        require_approval(run_dir, stamp, "massive-diff", "bootstrap exceeded human-review threshold",
                         {"changed_files": files, "changed_lines": lines, "numstat": numstat})
    passed = run_checks(config, worktree, run_dir / "checks", dry_run)
    append_iteration_verification(iteration_path, BOOTSTRAP_VERSION, config.checks, passed)
    proposal = run_agent(config, role_agent(config, "prototyper"),
                         bootstrap_synthesis_prompt(roadmap, research, literature, prototype, polish, security),
                         config.repo, run_dir / "07-roadmap-proposal", dry_run, wait_for_quota)
    proposal_path = run_dir / "roadmap-proposals" / f"v{BOOTSTRAP_VERSION}.md"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(proposal, encoding="utf-8")
    request = run_dir / "approvals" / f"roadmap-init-v{BOOTSTRAP_VERSION}.request.json"
    _atomic_json(request, {"run_id": stamp, "phase": f"roadmap-init-v{BOOTSTRAP_VERSION}",
                          "roadmap": str(roadmap_path), "proposal": str(proposal_path),
                          "reason": "initial generated roadmap content requires repository-owner approval"})
    _atomic_json(run_dir / "result.json", {"type": "bootstrap", "run_id": stamp,
        "version": BOOTSTRAP_VERSION, "roadmap_created": created, "roadmap": str(roadmap_path),
        "branch": branch, "worktree": str(worktree), "checks_passed": passed,
        "role_assignments": config.roles, "proposal": str(proposal_path),
        "iteration_document": str(iteration_path)})
    if not passed:
        raise CouncilError(f"bootstrap checks failed; inspect {run_dir / 'checks'}")
    return run_dir


def extract_version_section(roadmap: str, version: int | str) -> str:
    """Return one actual version entry from the version-by-version implementation section."""
    try:
        return extract_version_entry(roadmap, version)
    except KeyError as exc:
        raise CouncilError(str(exc)) from exc

def roadmap_for(config: Config) -> tuple[Path, str]:
    """Return the primary discovered roadmap for approved mutation operations."""
    files = discover_roadmap_files(config)
    if not files:
        raise CouncilError(f"no *roadmap*.md file found under {(config.repo / 'docs').resolve()}")
    path = files[0]
    return path, path.read_text(encoding="utf-8")

def campaign_assignments(from_version: int, to_version: int) -> list[dict[str, int | None]]:
    if not (1 <= from_version <= to_version):
        raise CouncilError("campaign versions must satisfy 1 <= from-version <= to-version")
    rows = []
    for assurance in range(from_version, to_version + 1):
        rows.append({
            "claude_review": assurance,
            "gpt_implement": assurance + 1 if assurance + 1 <= to_version else None,
            "codex_research": assurance + 2 if assurance + 2 <= to_version else None,
        })
    return rows

def version_research_prompt(version: int, section: str, library: str, plan_review: str) -> str:
    return f"""You are the Codex CLI forward research agent for project version v{version}.
Research the newest credible papers, standards, official repositories, security advisories, and
relevant engineering developments for this exact roadmap phase. Verify dates and citations.
Return: concise findings, evidence/citations, implementation implications, risks, and proposed
roadmap edits. Do not modify the repository. Distinguish evidence from inference.

MANDATORY ROADMAP REVIEW BRIEF:
{plan_review}

ROADMAP SECTION:
{section}

SHARED LITERATURE:
{library}
"""


ROADMAP_VERSION_EXECUTION_CONTRACT = f"""
ROADMAP VERSION EXECUTION CONTRACT:
{version_execution_contract()}

{per_version_recipe('<NN>')}

{host_side_evidence_boundary()}

{quota_handoff_template()}
"""

WORKTREE_MUTATION_POLICY = """
WORKTREE AUTHORITY:
- Work only inside the current isolated Git worktree.
- You may inspect, add, modify, rename, or remove repository files anywhere needed to satisfy this
  bounded roadmap phase, including source, tests, experiments, docs, schemas, configuration,
  migrations, build assets, and result-generation code.
- Follow existing repository conventions and keep every change traceable to the current version.
- Do not modify `.git`, Commander external state, credentials, secrets, production systems, live
  hardware, or unrelated later roadmap phases.
- Do not merge, push, deploy, tag, publish, release, or commit. Leave the candidate uncommitted for
  human inspection.
- Never weaken safety, authorization, verification, evidence integrity, or tests merely to pass a gate.
"""


ADVERSARIAL_EVALUATOR_STANCE = """
ADVERSARIAL EVALUATOR STANCE:
- Default to doubt. Assume this implementation is BROKEN until the evidence in front of you proves
  otherwise. Your job is not to approve; it is to find what fails.
- Do not praise. A clean-looking diff is not evidence. The author's explanation of why the code is
  correct is not evidence; the only evidence is what the code actually does.
- Verify by acting, not by reading. Run the tests and read their real output rather than judging that
  the code "looks correct". Where a behavior can be exercised, exercise it before trusting it.
- Judge behavior against the roadmap obligation, not the author's stated intent. Check the edge cases
  the author skipped, the failure paths, and the gap between "it runs" and "it is right".
- Reach a verdict: PASS only if every obligation for this phase holds with evidence. Otherwise REJECT
  and list each concrete failure with file-level evidence. A review that never rejects is not a review.
"""


def version_literature_prompt(version: int, section: str, prior_research: str, library: str) -> str:
    return f"""You are the configured Codex CLI literature reviewer for project version v{version}.
Review only evidence relevant to this roadmap phase. Synthesize the shared reviewed-PDF library and
any current research packet into an actionable literature review. Inspect the current isolated
worktree and reconcile the roadmap with the repository's actual architecture, documentation, tests,
experiments, and evidence.

You are an active repository contributor, not a read-only commentator. Add or modify files whenever
needed to make the literature traceable and technically useful. Appropriate changes may include
research notes, citations, requirements traces, ADRs, iteration documentation, benchmark plans,
test/experiment scaffolding, schemas, comments, or bounded code corrections revealed by the evidence.
Do not invent citations, metrics, metadata, page references, or completed physical evidence.

{WORKTREE_MUTATION_POLICY}

Finish with: verified sources, principal findings, limitations, implementation implications,
benchmark ideas, security/interoperability consequences, files changed, and claims still requiring
verification.

ROADMAP SECTION:
{section}

CURRENT RESEARCH INPUT:
{prior_research or 'No current research packet is available; rely only on reviewed sources and mark gaps.'}

SHARED LITERATURE:
{library}
"""


def version_implement_prompt(version: int, section: str, prior_research: str, literature: str,
                             library: str, subagent_cap: int = 5) -> str:
    return f"""You are the configured Codex CLI prototyper for project version v{version}.
Implement the complete bounded v{version} roadmap phase in the current isolated worktree. Use the
version literature review and current research packet, inspect existing architecture and tests, and
change any repository files needed for a coherent implementation.

For code changes, add version-scoped pytest coverage under `tests/` using a
`test_v{version}_*.py` filename. When the roadmap requires measurable integration, simulation,
benchmark, fault, performance, or evidence behavior, add a safe deterministic
`experiments/run_v{version}_*.py` runner that writes JSON and Markdown beneath
`results/iterations/v{version}/`. Do not fabricate physical, HIL, or field evidence.

{subagent_delegation_contract(subagent_cap)}

{WORKTREE_MUTATION_POLICY}

Finish with a concise list of findings, bugs fixed/discovered, changed files, tests, experiments,
gathered result paths, compatibility effects, residual risks, and whether a clean version-boundary
handoff is required.

{ROADMAP_VERSION_EXECUTION_CONTRACT}

ROADMAP SECTION:
{section}

CURRENT RESEARCH INPUT:
{prior_research or 'No forward research packet available.'}

VERSION LITERATURE REVIEW:
{literature or 'No version literature review is available.'}

SHARED LITERATURE INDEX:
{library}
"""


def version_code_polish_prompt(version: int, section: str, literature: str, implementation: str) -> str:
    return f"""You are the configured Claude Code polisher for project version v{version}.
Review the complete current worktree, not just the prior agent's summary. Improve correctness,
maintainability, architecture, typing, interoperability, packaging, documentation, tests, and
experiment quality within the bounded v{version} scope. You may add, modify, rename, or remove any
repository files needed to produce the best coherent candidate. Correct implementation defects
directly rather than merely describing them.

Ensure focused pytest coverage exists under `tests/test_v{version}_*.py` when code behavior changes.
Ensure required host-safe experiments follow repository conventions and write machine-readable and
Markdown results beneath `results/iterations/v{version}/`.

{WORKTREE_MUTATION_POLICY}

Finish with changed files, defects corrected, tests/experiments updated, remaining design debt,
quota/context boundary risk, and anything the security-assurance stage must inspect.

{ROADMAP_VERSION_EXECUTION_CONTRACT}

ROADMAP SECTION:
{section}

LITERATURE REVIEW:
{literature or 'No literature review summary is available.'}

PROTOTYPE SUMMARY:
{implementation or 'No prototype summary is available; inspect the worktree directly.'}
"""


def version_security_assurance_prompt(version: int, section: str, code_polish: str) -> str:
    return f"""You are the configured Claude Code security assurance agent for project version v{version}.
Perform the final adversarial review of the current isolated worktree. Inspect all relevant source,
tests, experiments, configuration, migrations, documentation, dependencies, trust boundaries,
authorization paths, failure handling, evidence handling, and the actual diff.

You are authorized to correct defects directly anywhere in the repository when the correction is
necessary for this bounded phase. Add or strengthen tests and safe experiments as needed. Pay special
attention to false success, replay/idempotency, stale authority, cross-addressing, unsafe defaults,
input validation, resource exhaustion, secrets, downgrade paths, fail-open behavior, and evidence
integrity. Never fabricate HIL, field, or hardware evidence.

{ADVERSARIAL_EVALUATOR_STANCE}

{WORKTREE_MUTATION_POLICY}

Finish with a severity-ranked assurance summary, files changed, vulnerabilities or bugs corrected,
verification added, blockers, residual risk, host-side evidence gates, and clean-boundary handoff status.

{ROADMAP_VERSION_EXECUTION_CONTRACT}

ROADMAP SECTION:
{section}

CODE-POLISH SUMMARY:
{code_polish or 'No code-polish summary is available; inspect the worktree directly.'}
"""


EXPERIMENT_SIGNALS = (
    "experiment", "benchmark", "simulation", "sitl", "integration", "interoperability",
    "fault", "resilience", "recovery", "performance", "latency", "throughput", "scale",
    "scalability", "soak", "replay", "migration", "network", "partition", "fuzz",
    "property-based", "model-based", "perception", "slam", "marl", "calibration",
    "resource", "evidence", "campaign", "topology",
)
HARDWARE_ONLY_SIGNALS = (
    "live flight", "real hardware", "physical bench", "field operation", "airspace",
    "hardware-in-the-loop", "props-on", "powered rover", "site authorization",
)
HOST_SAFE_SIGNALS = (
    "simulation", "sitl", "mock", "replay", "benchmark", "fault", "model-based",
    "property-based", "fuzz", "process-level", "integration", "host-side",
)


def version_validation_requirements(
    version: int,
    section: str,
    has_code_stage: bool,
    tests_dir: Path = Path("tests"),
    experiments_dir: Path = Path("experiments"),
    results_dir: Path = Path("results/iterations"),
) -> dict[str, Any]:
    """Derive deterministic validation obligations from the roadmap and active campaign stage."""
    lowered = section.lower()
    tests_required = bool(has_code_stage)
    experiment_signal = any(marker in lowered for marker in EXPERIMENT_SIGNALS)
    hardware_only = any(marker in lowered for marker in HARDWARE_ONLY_SIGNALS)
    host_safe = any(marker in lowered for marker in HOST_SAFE_SIGNALS)
    experiments_required = bool(has_code_stage and experiment_signal and (not hardware_only or host_safe))
    experiment_deferred = bool(has_code_stage and hardware_only and not host_safe)
    return {
        "version": version,
        "tests_required": tests_required,
        "experiments_required": experiments_required,
        "experiment_deferred": experiment_deferred,
        "test_pattern": f"{tests_dir.as_posix()}/test_v{version}_*.py",
        "experiment_pattern": f"{experiments_dir.as_posix()}/run_v{version}_*.py",
        "expected_result_dir": f"{results_dir.as_posix()}/v{version}",
        "rationale": {
            "tests": (
                "A code-producing or assurance stage is active for this version."
                if tests_required else
                "This version is research/literature-only in the current staggered campaign round."
            ),
            "experiments": (
                "The roadmap requests measurable integration, evidence, performance, fault, or simulation behavior."
                if experiments_required else
                "No safe host-side experiment is mandatory in this campaign round."
            ),
        },
    }


def validation_artifact_prompt(
    version: int,
    section: str,
    requirements: dict[str, Any],
    changed_paths: list[str],
    tests_dir: Path,
    experiments_dir: Path,
    results_dir: Path,
) -> str:
    """Ask the prototyper to fill only missing, roadmap-justified validation artifacts."""
    return f"""You are the Codex CLI validation-artifact finisher for project version v{version}.
Inspect the current isolated worktree and the changes already made for this version. Add only the
focused validation artifacts required below. Do not broaden implementation scope and do not rewrite
unrelated tests or experiments.

REQUIREMENTS:
{json.dumps(requirements, indent=2)}

ROADMAP SECTION:
{section}

PATHS ATTRIBUTED TO THIS VERSION SO FAR:
{json.dumps(changed_paths, indent=2)}

RULES:
- When tests are required, create or update focused pytest files under `{tests_dir.as_posix()}`.
- Prefer deterministic names beginning `test_v{version}_`, such as
  `{tests_dir.as_posix()}/test_v{version}_<core_feature>.py`.
- Tests must exercise roadmap acceptance criteria, failure behavior, regressions, and security/safety
  boundaries. Do not weaken existing assertions or skip difficult cases without an explicit reason.
- When an experiment is required, create or update a deterministic Python runner under
  `{experiments_dir.as_posix()}` named `run_v{version}_<core_feature>.py`.
- The experiment must be safe for a normal development host: no live actuation, no real hardware,
  no external network dependency, no credentials, and no mandatory command-line arguments.
- The experiment must return nonzero on failure and write machine-readable JSON plus a concise
  Markdown summary beneath `{results_dir.as_posix()}/v{version}/`.
- If the roadmap requires physical/HIL/field evidence that cannot safely run on a normal host, do not
  fabricate it. Create a host-safe simulation/replay harness only when meaningful and state the
  remaining operator-attested requirement.
- Leave all changes uncommitted unless the explicit dispatch contract says commit-per-version. Do not merge, push, deploy, tag, publish, or release.
- Apply generated-artifact discipline: current-version evidence only; unrelated run output remains uncommitted.
- Respect host-side evidence boundaries and never machine-award physical/HIL/field/audit/publication gates.
- Finish with the exact test and experiment paths created or updated, plus any deferred evidence and a clean-boundary handoff line when pausing.
"""


def _worktree_snapshot(checkout: Path) -> dict[str, str]:
    """Hash tracked and untracked non-ignored files for stage-level change attribution."""
    listing = git(checkout, "ls-files", "--cached", "--others", "--exclude-standard", check=False)
    snapshot: dict[str, str] = {}
    for value in listing.splitlines():
        relative = value.strip()
        if not relative:
            continue
        candidate = checkout / relative
        if candidate.is_file():
            snapshot[relative.replace("\\", "/")] = sha256_file(candidate)
    return snapshot


def _snapshot_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def run_agent_with_change_capture(
    config: Config,
    agent_name: str,
    prompt: str,
    cwd: Path,
    stage_dir: Path,
    dry_run: bool,
    wait_for_quota: bool | None = None,
) -> tuple[str, list[str]]:
    """Run a worktree-mutating stage and durably retain the paths it changed."""
    changes_path = stage_dir / "changed-paths.json"
    before = _worktree_snapshot(cwd)
    output = run_agent(config, agent_name, prompt, cwd, stage_dir, dry_run, wait_for_quota)
    after = _worktree_snapshot(cwd)
    changes = _snapshot_changes(before, after)
    if not changes and changes_path.exists():
        prior = json.loads(changes_path.read_text(encoding="utf-8"))
        if isinstance(prior, list) and all(isinstance(item, str) for item in prior):
            changes = prior
    _atomic_json(changes_path, {"paths": changes})
    return output, changes


def _read_changed_paths(stage_dir: Path) -> list[str]:
    path = stage_dir / "changed-paths.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("paths", [])
    return [str(item) for item in raw if isinstance(item, str)]


def _version_python_artifacts(checkout: Path, directory: Path, pattern: str) -> list[Path]:
    root = checkout / directory
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob(pattern) if path.is_file())


def discover_version_tests(config: Config, checkout: Path, version: int) -> list[Path]:
    return _version_python_artifacts(checkout, config.tests_dir, f"test_v{version}*.py")


def discover_version_experiments(config: Config, checkout: Path, version: int) -> list[Path]:
    return _version_python_artifacts(checkout, config.experiments_dir, f"run_v{version}*.py")


def _relative_paths(checkout: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(checkout).as_posix() for path in paths]


def _run_recorded_command(
    command: tuple[str, ...] | list[str],
    cwd: Path,
    stage_dir: Path,
    dry_run: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    cmd = list(command)
    (stage_dir / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    started = dt.datetime.now(UTC)
    if dry_run:
        result = {
            "status": "planned",
            "returncode": None,
            "duration_seconds": 0.0,
            "command": cmd,
        }
        (stage_dir / "stdout.txt").write_text("", encoding="utf-8")
        (stage_dir / "stderr.txt").write_text("", encoding="utf-8")
        _atomic_json(stage_dir / "result.json", result)
        return result
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        status = "passed" if returncode == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nTimed out after {timeout_seconds} seconds."
        status = "timed_out"
    duration = (dt.datetime.now(UTC) - started).total_seconds()
    (stage_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (stage_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    result = {
        "status": status,
        "returncode": returncode,
        "duration_seconds": duration,
        "command": cmd,
    }
    _atomic_json(stage_dir / "result.json", result)
    return result


def _result_files(config: Config, checkout: Path, version: int) -> list[Path]:
    root = checkout / config.results_dir / f"v{version}"
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def run_version_validation(
    config: Config,
    checkout: Path,
    run_dir: Path,
    version: int,
    requirements: dict[str, Any],
    changed_paths: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    """Execute focused tests/experiments and capture a deterministic per-version result."""
    validation_dir = run_dir / f"v{version}" / "07-validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    tests = discover_version_tests(config, checkout, version)
    experiments = discover_version_experiments(config, checkout, version)
    test_paths = _relative_paths(checkout, tests)
    experiment_paths = _relative_paths(checkout, experiments)

    missing_tests = bool(requirements["tests_required"] and not tests and not dry_run)
    missing_experiments = bool(requirements["experiments_required"] and not experiments and not dry_run)

    test_result: dict[str, Any]
    if tests:
        test_result = _run_recorded_command(
            (*config.version_test_command, *test_paths),
            checkout,
            validation_dir / "tests",
            dry_run,
            config.version_validation_timeout_seconds,
        )
    else:
        test_result = {
            "status": "planned" if dry_run and requirements["tests_required"] else "not_required",
            "returncode": None,
            "duration_seconds": 0.0,
            "command": [*config.version_test_command, requirements["test_pattern"]],
        }
        _atomic_json(validation_dir / "tests" / "result.json", test_result)

    experiment_results: list[dict[str, Any]] = []
    for index, experiment in enumerate(experiments, 1):
        relative = experiment.relative_to(checkout).as_posix()
        command_result = _run_recorded_command(
            (*config.version_experiment_command, relative),
            checkout,
            validation_dir / f"experiment-{index}",
            dry_run,
            config.version_validation_timeout_seconds,
        )
        command_result["path"] = relative
        experiment_results.append(command_result)

    if not experiments and dry_run and requirements["experiments_required"]:
        experiment_results.append({
            "status": "planned",
            "returncode": None,
            "duration_seconds": 0.0,
            "command": [*config.version_experiment_command, requirements["experiment_pattern"]],
            "path": requirements["experiment_pattern"],
        })

    generated_results = _relative_paths(checkout, _result_files(config, checkout, version))
    command_failures = (
        test_result.get("status") in {"failed", "timed_out"}
        or any(item.get("status") in {"failed", "timed_out"} for item in experiment_results)
    )
    passed = not missing_tests and not missing_experiments and not command_failures
    if dry_run:
        passed = True

    result = {
        "version": version,
        "status": "planned" if dry_run else ("passed" if passed else "failed"),
        "passed": passed,
        "requirements": requirements,
        "changed_paths": sorted(set(changed_paths)),
        "tests": {
            "paths": test_paths,
            "missing_required": missing_tests,
            "execution": test_result,
        },
        "experiments": {
            "paths": experiment_paths,
            "missing_required": missing_experiments,
            "executions": experiment_results,
            "deferred": requirements["experiment_deferred"],
        },
        "result_files": generated_results,
    }
    _atomic_json(validation_dir / "result.json", result)
    return result


def write_version_result_summary(
    config: Config,
    checkout: Path,
    version: int,
    core_feature: str,
    validation: dict[str, Any],
    global_checks_passed: bool | None = None,
) -> tuple[Path, Path]:
    """Persist gathered version evidence inside the candidate worktree."""
    directory = checkout / config.results_dir / f"v{version}"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "campaign-result.json"
    markdown_path = directory / "campaign-result.md"
    payload = dict(validation)
    payload["core_feature"] = core_feature
    payload["global_checks_passed"] = global_checks_passed
    _atomic_json(json_path, payload)

    tests = payload["tests"]["paths"]
    experiments = payload["experiments"]["paths"]
    result_files = sorted(set(payload.get("result_files", [])) | {
        json_path.relative_to(checkout).as_posix(),
        markdown_path.relative_to(checkout).as_posix(),
    })
    test_status = payload["tests"]["execution"].get("status", "unknown")
    experiment_statuses = [item.get("status", "unknown") for item in payload["experiments"]["executions"]]
    lines = [
        f"# v{version} campaign result — {core_feature}",
        "",
        f"- Per-version status: **{payload['status'].upper()}**",
        f"- Focused test status: **{test_status.upper()}**",
        f"- Experiment statuses: **{', '.join(experiment_statuses) if experiment_statuses else 'NOT REQUIRED'}**",
        f"- Global checks: **{str(global_checks_passed).upper() if global_checks_passed is not None else 'PENDING'}**",
        "",
        "## Tests",
        *([f"- `{item}`" for item in tests] or ["- No version-specific test file was required or discovered."]),
        "",
        "## Experiments",
        *([f"- `{item}`" for item in experiments] or ["- No version-specific experiment was required or discovered."]),
        "",
        "## Gathered result files",
        *([f"- `{item}`" for item in result_files] or ["- No result files were generated."]),
        "",
        "## Changed paths attributed to this version",
        *([f"- `{item}`" for item in payload["changed_paths"]] or ["- No worktree changes were attributed to this stage."]),
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    payload["result_files"] = result_files
    _atomic_json(json_path, payload)
    return json_path, markdown_path


def append_iteration_validation(
    path: Path,
    version: int,
    validation: dict[str, Any],
    result_files: list[str],
) -> None:
    """Append model-independent focused test/experiment evidence to an iteration note."""
    start = f"<!-- HERMES-LEGION-COMMANDER VALIDATION v{version} START -->"
    end = f"<!-- HERMES-LEGION-COMMANDER VALIDATION v{version} END -->"
    tests = validation["tests"]["paths"]
    experiments = validation["experiments"]["paths"]
    test_status = validation["tests"]["execution"].get("status", "unknown")
    experiment_statuses = [item.get("status", "unknown") for item in validation["experiments"]["executions"]]
    block_lines = [
        start,
        "",
        "## Supervisor-captured version validation",
        "",
        f"**Per-version status:** {validation['status'].upper()}",
        "",
        f"**Focused tests:** {test_status.upper()}",
        *([f"- `{item}`" for item in tests] or ["- No focused test file was required or discovered."]),
        "",
        f"**Experiments:** {', '.join(experiment_statuses).upper() if experiment_statuses else 'NOT REQUIRED'}",
        *([f"- `{item}`" for item in experiments] or ["- No experiment was required or discovered."]),
        "",
        "**Gathered results:**",
        *([f"- `{item}`" for item in result_files] or ["- No result files were generated."]),
        "",
        end,
    ]
    block = "\n".join(block_lines)
    prior = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if pattern.search(prior):
        merged = pattern.sub(block, prior)
    else:
        merged = prior.rstrip() + "\n\n" + block + "\n"
    path.write_text(merged, encoding="utf-8")


def write_campaign_summary(
    run_dir: Path,
    checkout: Path,
    from_version: int,
    to_version: int,
    version_results: dict[int, dict[str, Any]],
    global_checks_passed: bool,
) -> tuple[Path, Path]:
    """Aggregate per-version validation evidence for operator review."""
    json_path = run_dir / "campaign-summary.json"
    markdown_path = run_dir / "campaign-summary.md"
    payload = {
        "from_version": from_version,
        "to_version": to_version,
        "worktree": str(checkout),
        "global_checks_passed": global_checks_passed,
        "versions": {str(version): result for version, result in version_results.items()},
    }
    _atomic_json(json_path, payload)
    lines = [
        f"# Campaign summary v{from_version}–v{to_version}",
        "",
        f"- Worktree: `{checkout}`",
        f"- Global checks: **{'PASSED' if global_checks_passed else 'FAILED'}**",
        "",
        "| Version | Status | Tests | Experiments | Result files |",
        "|---:|---|---:|---:|---:|",
    ]
    for version in range(from_version, to_version + 1):
        result = version_results[version]
        lines.append(
            f"| v{version} | {result['status']} | "
            f"{len(result['tests']['paths'])} | "
            f"{len(result['experiments']['paths'])} | "
            f"{len(result.get('result_files', []))} |"
        )
    lines.extend([
        "",
        "No merge, push, deployment, tag, publication, or release was performed.",
        "",
    ])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def roadmap_update_prompt(version: int, section: str, research: str, implementation: str, assurance: str) -> str:
    return f"""You are Codex CLI drafting a HUMAN-REVIEWED roadmap update for project version v{version}.
Do not edit files. Produce a concise Markdown block with exactly these headings:
### Approved-input proposal for v{version}
- New findings
- Bugs and security issues
- Updated roadmap content
- Verification evidence
- Open risks
Use only evidence in the supplied artifacts. Keep it concise and actionable.

CURRENT SECTION:
{section}

CODEX RESEARCH:
{research}

CODEX IMPLEMENTATION SUMMARY:
{implementation}

CLAUDE ASSURANCE SUMMARY:
{assurance}
"""

def apply_roadmap_update(config: Config, run_dir: Path, version: int) -> Path:
    proposal = run_dir / "roadmap-proposals" / f"v{version}.md"
    grant = run_dir / "approvals" / f"roadmap-update-v{version}.grant.json"
    if not proposal.exists():
        raise CouncilError(f"roadmap proposal missing for v{version}")
    if not grant.exists():
        raise CouncilError(f"roadmap update v{version} has not been approved")
    path, text = roadmap_for(config)
    marker = f"<!-- HERMES-LEGION-COMMANDER v{version} -->"
    if marker in text:
        return path
    block = f"\n\n{marker}\n{proposal.read_text(encoding='utf-8').strip()}\n"
    path.write_text(text.rstrip() + block, encoding="utf-8")
    _atomic_json(run_dir / "roadmap-proposals" / f"v{version}.applied.json", {"version": version, "path": str(path), "applied_at": dt.datetime.now(UTC).isoformat()})
    return path

def evaluate_goal(
    config: Config,
    condition: str,
    judge_agent: str | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    wait_for_quota: bool | None = None,
    checkout: Path | None = None,
) -> dict[str, Any]:
    """Evaluate a ``/goal`` stop condition with a fresh-model judge over the checks.

    Runs the configured deterministic checks for evidence, then asks a fresh model
    (by default the ``security_assurance`` agent, never the generator) whether the
    natural-language condition holds. A failed deterministic check vetoes a "met"
    verdict. ``judge_agent`` overrides which configured agent judges. ``checkout``
    overrides where checks run and where the judge inspects (defaults to the
    configured repo; the campaign passes its worktree).
    """
    condition = condition.strip()
    if not condition:
        raise CouncilError("--condition must be a non-empty stop condition")
    judge = judge_agent or role_agent(config, "security_assurance")
    if judge not in config.agents:
        raise CouncilError(f"--judge must name a configured agent {sorted(config.agents)}")
    target = (checkout or config.repo)
    if not (target / ".git").exists():
        raise CouncilError(f"not a Git checkout: {target}")
    results = run_deterministic_checks(
        target, config.checks, timeout=config.version_validation_timeout_seconds
    )
    prompt = stop_condition_prompt(condition, results)
    if dry_run:
        return {
            "condition": condition,
            "judge": judge,
            "judge_model": config.agents[judge].model or "CLI configured default",
            "dry_run": True,
            "deterministic_all_passed": deterministic_all_passed(results),
            "checks": [r.to_dict() for r in results],
            "prompt_chars": len(prompt),
        }
    stamp = run_id or dt.datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stage_dir = config.state_dir / "goal" / f"goal-{stamp}"
    output = run_agent(
        config, judge, prompt, target, stage_dir, dry_run=False,
        wait_for_quota=False if wait_for_quota is None else wait_for_quota,
    )
    verdict = parse_stop_verdict(output, results)
    verdict["condition"] = condition
    verdict["judge"] = judge
    verdict["judge_model"] = config.agents[judge].model or "CLI configured default"
    _atomic_json(stage_dir / "goal-verdict.json", verdict)
    return verdict


def run_campaign(config: Config, from_version: int, to_version: int, dry_run: bool,
                 run_id: str | None = None, wait_for_quota: bool | None = None,
                 strategy: str | None = None, until: str | None = None,
                 pr_options: PRWorkflowOptions | None = None) -> Path:
    if not (config.repo / ".git").exists():
        raise CouncilError(f"not a Git checkout: {config.repo}")
    strategy = strategy or config.campaign_strategy
    if strategy not in {"full", "staggered", "alternating"}:
        raise CouncilError("campaign strategy must be 'full', 'staggered', or 'alternating'")
    if strategy == "alternating":
        config = dataclasses.replace(config, worker_failover=True)
    stamp = run_id or f"v{from_version}-v{to_version}-{make_run_id()}"
    run_dir = config.state_dir / f"campaign-{stamp}"
    worktree = run_dir / "worktree"
    pr_options = pr_options or PRWorkflowOptions()
    branch_slug = pr_options.slug or f"{config.repo.name}-v{from_version}-v{to_version}"
    branch = (
        pr_branch_name(actor=pr_options.actor, mode="collaborating", slug=branch_slug, stamp=stamp)
        if pr_options.active
        else f"hermes-legion/council-campaign/{stamp}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    job = run_dir / "job.json"
    base_info: dict[str, Any] = {"base_ref": "HEAD"}
    if not job.exists():
        if git(config.repo, "status", "--porcelain"):
            raise CouncilError("repository must be clean before a new council campaign")
        if pr_options.active:
            try:
                base_info = fetch_base(config.repo, remote=pr_options.remote, base_branch=pr_options.base_branch)
            except PRWorkflowError as exc:
                raise CouncilError(str(exc)) from exc
        _atomic_json(job, {
            "type": "campaign",
            "run_id": stamp,
            "from_version": from_version,
            "to_version": to_version,
            "dry_run": dry_run,
            "strategy": strategy,
            "pr_workflow": dataclasses.asdict(pr_options),
            "base": base_info,
        })
    else:
        try:
            existing_job = json.loads(job.read_text(encoding="utf-8"))
            if isinstance(existing_job.get("base"), dict):
                base_info = existing_job["base"]
        except (OSError, json.JSONDecodeError):
            pass

    roadmap_path, roadmap, roadmap_files = roadmap_context(config)
    campaign_sections = {
        version: extract_version_section(roadmap, version)
        for version in range(from_version, to_version + 1)
    }
    dangerous, reasons = assess_change_risk("\n\n".join(campaign_sections.values()))
    if dangerous:
        require_approval(
            run_dir,
            stamp,
            "dangerous-intent",
            "campaign includes security, safety, authority, deployment, credential, or other high-risk boundaries",
            {
                "from_version": from_version,
                "to_version": to_version,
                "strategy": strategy,
                "matched_terms": reasons,
                "roadmap": str(roadmap_path),
            },
        )

    if not worktree.exists():
        git(config.repo, "worktree", "add", "-b", branch, str(worktree), str(base_info.get("base_ref", "HEAD")))

    library = library_context(config)
    plan_reviews: dict[int, str] = {}
    research_outputs: dict[int, str] = {}
    literature_outputs: dict[int, str] = {}
    implementation_outputs: dict[int, str] = {}
    code_polish_outputs: dict[int, str] = {}
    security_outputs: dict[int, str] = {}
    assurance_outputs: dict[int, str] = {}
    changed_paths_by_version: dict[int, list[str]] = {
        version: [] for version in range(from_version, to_version + 1)
    }

    if strategy in {"full", "alternating"}:
        research_versions = set(range(from_version, to_version + 1))
        implementation_versions = set(range(from_version, to_version + 1))
        assurance_versions = set(range(from_version, to_version + 1))
    else:
        rows = campaign_assignments(from_version, to_version)
        research_versions = {
            int(row["codex_research"])
            for row in rows
            if row["codex_research"] is not None
        }
        implementation_versions = {
            int(row["gpt_implement"])
            for row in rows
            if row["gpt_implement"] is not None
        }
        assurance_versions = {
            int(row["claude_review"])
            for row in rows
            if row["claude_review"] is not None
        }

    for version in range(from_version, to_version + 1):
        section = campaign_sections[version]

        if version in research_versions:
            plan_reviews[version] = run_agent(
                config,
                role_agent(config, "roadmap_plan_reviewer"),
                roadmap_plan_review_prompt(section, version=str(version)),
                config.repo,
                run_dir / f"v{version}" / "00-roadmap-plan-review",
                dry_run,
                wait_for_quota,
            )
            research_outputs[version] = run_agent(
                config,
                role_agent(config, "researcher"),
                version_research_prompt(version, section, library, plan_reviews[version]),
                config.repo,
                run_dir / f"v{version}" / "01-research",
                dry_run,
                wait_for_quota,
            )

        literature_outputs[version], stage_changes = run_agent_with_change_capture(
            config,
            role_agent(config, "literature_reviewer"),
            version_literature_prompt(
                version, section, research_outputs.get(version, ""), library
            ),
            worktree,
            run_dir / f"v{version}" / "02-literature-review",
            dry_run,
            wait_for_quota,
        )
        changed_paths_by_version[version].extend(stage_changes)

        if version in implementation_versions:
            implementation_outputs[version], stage_changes = run_agent_with_change_capture(
                config,
                role_agent(config, "prototyper"),
                version_implement_prompt(
                    version,
                    section,
                    research_outputs.get(version, ""),
                    literature_outputs[version],
                    library,
                    config.subagent_cap,
                ),
                worktree,
                run_dir / f"v{version}" / "03-prototype",
                dry_run,
                wait_for_quota,
            )
            changed_paths_by_version[version].extend(stage_changes)

        if version in assurance_versions:
            code_polish_outputs[version], stage_changes = run_agent_with_change_capture(
                config,
                role_agent(config, "code_polisher"),
                version_code_polish_prompt(
                    version,
                    section,
                    literature_outputs.get(version, ""),
                    implementation_outputs.get(version, ""),
                ),
                worktree,
                run_dir / f"v{version}" / "04-code-polish",
                dry_run,
                wait_for_quota,
            )
            changed_paths_by_version[version].extend(stage_changes)

            security_outputs[version], stage_changes = run_agent_with_change_capture(
                config,
                role_agent(config, "security_assurance"),
                version_security_assurance_prompt(
                    version, section, code_polish_outputs.get(version, "")
                ),
                worktree,
                run_dir / f"v{version}" / "05-security-assurance",
                dry_run,
                wait_for_quota,
            )
            changed_paths_by_version[version].extend(stage_changes)
            assurance_outputs[version] = "\n\n".join(
                value for value in (
                    code_polish_outputs.get(version, ""),
                    security_outputs.get(version, ""),
                )
                if value
            )

    requirements_by_version: dict[int, dict[str, Any]] = {}
    for version in range(from_version, to_version + 1):
        section = campaign_sections[version]
        has_code_stage = (
            version in implementation_outputs
            or version in code_polish_outputs
            or version in security_outputs
            or bool(changed_paths_by_version[version])
        )
        requirements = version_validation_requirements(
            version,
            section,
            has_code_stage,
            config.tests_dir,
            config.experiments_dir,
            config.results_dir,
        )
        requirements_by_version[version] = requirements
        tests = discover_version_tests(config, worktree, version)
        experiments = discover_version_experiments(config, worktree, version)
        missing_required = (
            (requirements["tests_required"] and not tests)
            or (requirements["experiments_required"] and not experiments)
        )
        if missing_required:
            _, stage_changes = run_agent_with_change_capture(
                config,
                role_agent(config, "prototyper"),
                validation_artifact_prompt(
                    version,
                    section,
                    requirements,
                    sorted(set(changed_paths_by_version[version])),
                    config.tests_dir,
                    config.experiments_dir,
                    config.results_dir,
                ),
                worktree,
                run_dir / f"v{version}" / "06-validation-artifacts",
                dry_run,
                wait_for_quota,
            )
            changed_paths_by_version[version].extend(stage_changes)

    version_results: dict[int, dict[str, Any]] = {}
    for version in range(from_version, to_version + 1):
        core_feature = core_feature_from_section(campaign_sections[version], version)
        result = run_version_validation(
            config,
            worktree,
            run_dir,
            version,
            requirements_by_version[version],
            sorted(set(changed_paths_by_version[version])),
            dry_run,
        )
        json_result, markdown_result = write_version_result_summary(
            config, worktree, version, core_feature, result
        )
        result["result_files"] = sorted(set(result.get("result_files", [])) | {
            json_result.relative_to(worktree).as_posix(),
            markdown_result.relative_to(worktree).as_posix(),
        })
        _atomic_json(run_dir / f"v{version}" / "07-validation" / "result.json", result)
        version_results[version] = result

    iteration_documents: list[Path] = []
    for version in range(from_version, to_version + 1):
        section = campaign_sections[version]
        core_feature = core_feature_from_section(section, version)
        validation_summary = json.dumps(version_results[version], indent=2)
        record = run_agent(
            config,
            role_agent(config, "literature_reviewer"),
            iteration_record_prompt(
                version,
                core_feature,
                section,
                plan_reviews.get(version, ""),
                research_outputs.get(version, ""),
                literature_outputs.get(version, ""),
                implementation_outputs.get(version, ""),
                assurance_outputs.get(version, ""),
                validation_summary,
            ),
            worktree,
            run_dir / f"v{version}" / "08-iteration-record",
            dry_run,
            wait_for_quota,
        )
        document = write_iteration_document(config, worktree, version, core_feature, record)
        append_iteration_validation(
            document,
            version,
            version_results[version],
            version_results[version]["result_files"],
        )
        iteration_documents.append(document)

    files, lines, numstat = diff_size(worktree)
    if files >= config.massive_files or lines >= config.massive_lines:
        require_approval(
            run_dir,
            stamp,
            "massive-diff",
            "campaign exceeded the configured human-review threshold",
            {
                "changed_files": files,
                "changed_lines": lines,
                "numstat": numstat,
                "strategy": strategy,
            },
        )
    global_checks_passed = run_checks(config, worktree, run_dir / "checks", dry_run)
    focused_validation_passed = all(result["passed"] for result in version_results.values())
    passed = global_checks_passed and focused_validation_passed

    for version, document in zip(range(from_version, to_version + 1), iteration_documents, strict=True):
        append_iteration_verification(document, version, config.checks, global_checks_passed)
        core_feature = core_feature_from_section(campaign_sections[version], version)
        json_result, markdown_result = write_version_result_summary(
            config,
            worktree,
            version,
            core_feature,
            version_results[version],
            global_checks_passed,
        )
        version_results[version]["result_files"] = sorted(set(version_results[version]["result_files"]) | {
            json_result.relative_to(worktree).as_posix(),
            markdown_result.relative_to(worktree).as_posix(),
        })
        version_results[version]["global_checks_passed"] = global_checks_passed
        _atomic_json(
            run_dir / f"v{version}" / "07-validation" / "result.json",
            version_results[version],
        )

    campaign_summary_json, campaign_summary_md = write_campaign_summary(
        run_dir,
        worktree,
        from_version,
        to_version,
        version_results,
        global_checks_passed,
    )

    for version in range(from_version, to_version + 1):
        proposal = run_agent(
            config,
            role_agent(config, "prototyper"),
            roadmap_update_prompt(
                version,
                campaign_sections[version],
                research_outputs.get(version, ""),
                implementation_outputs.get(version, ""),
                assurance_outputs.get(version, ""),
            ),
            worktree,
            run_dir / f"v{version}" / "09-roadmap-proposal",
            dry_run,
            wait_for_quota,
        )
        out = run_dir / "roadmap-proposals" / f"v{version}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(proposal, encoding="utf-8")
        request = run_dir / "approvals" / f"roadmap-update-v{version}.request.json"
        _atomic_json(request, {
            "run_id": stamp,
            "phase": f"roadmap-update-v{version}",
            "version": version,
            "roadmap": str(roadmap_path),
            "proposal": str(out),
            "reason": "roadmap modifications require repository-owner approval",
        })

    result_payload = {
        "type": "campaign",
        "run_id": stamp,
        "from_version": from_version,
        "to_version": to_version,
        "strategy": strategy,
        "branch": branch,
        "worktree": str(worktree),
        "checks_passed": passed,
        "global_checks_passed": global_checks_passed,
        "focused_validation_passed": focused_validation_passed,
        "iteration_documents": [str(path) for path in iteration_documents],
        "version_results": {str(version): result for version, result in version_results.items()},
        "campaign_summary_json": str(campaign_summary_json),
        "campaign_summary_markdown": str(campaign_summary_md),
        "roadmap_proposals": [
            str(run_dir / "roadmap-proposals" / f"v{version}.md")
            for version in range(from_version, to_version + 1)
        ],
        "roadmap_sources": [str(path) for path in roadmap_files],
        "stage_mutations": {
            str(version): sorted(set(paths))
            for version, paths in changed_paths_by_version.items()
        },
    }
    _atomic_json(run_dir / "result.json", result_payload)

    pr_result = None
    if passed and pr_options.active:
        try:
            pr_result = _finalize_pr_workflow(
                options=pr_options,
                run_dir=run_dir,
                worktree=worktree,
                branch=branch,
                run_id=stamp,
                mode="collaborating",
                title=pr_options.title or f"Legion Commander collaborating v{from_version}-v{to_version}",
                summary=(
                    f"Implements roadmap range v{from_version}-v{to_version} with Legion Commander "
                    f"collaborating mode using strategy `{strategy}`. The branch starts from latest "
                    f"`{pr_options.remote}/{pr_options.base_branch}` and is opened for review before merging to `{pr_options.base_branch}`."
                ),
                validation=(
                    f"- Global checks: {'PASS' if global_checks_passed else 'FAIL'}\n"
                    f"- Focused version validation: {'PASS' if focused_validation_passed else 'FAIL'}\n"
                    f"- Campaign summary: `{campaign_summary_md}`"
                ),
                artifacts=[str(campaign_summary_md), str(campaign_summary_json), str(run_dir / "result.json")],
                extra={"from_version": from_version, "to_version": to_version, "strategy": strategy},
                dry_run=dry_run,
            )
            result_payload["pull_request"] = pr_result
            _atomic_json(run_dir / "result.json", result_payload)
        except PRWorkflowError as exc:
            raise CouncilError(str(exc)) from exc

    if until:
        # Post-campaign /goal gate: a fresh model judges whether the campaign's
        # output satisfies the stop condition, evaluated against the campaign
        # worktree. The verdict is recorded for the operator and for a scheduler
        # deciding whether another campaign is needed. This does not stop the
        # batch mid-flight; "run until met" across reruns is the scheduler's job.
        stop_verdict = evaluate_goal(
            config, until, dry_run=dry_run, run_id=f"campaign-{stamp}",
            wait_for_quota=wait_for_quota, checkout=worktree,
        )
        _atomic_json(run_dir / "stop-condition.json", stop_verdict)
        result_path = run_dir / "result.json"
        recorded = json.loads(result_path.read_text(encoding="utf-8"))
        recorded["stop_condition"] = {
            "condition": until,
            "met": stop_verdict.get("met"),
            "deterministic_all_passed": stop_verdict.get("deterministic_all_passed"),
            "artifact": str(run_dir / "stop-condition.json"),
        }
        _atomic_json(result_path, recorded)
    if not passed:
        raise CouncilError(
            f"campaign validation failed; inspect {campaign_summary_md} and {run_dir / 'checks'}"
        )
    return run_dir


def _campaign_shadow_cost(run_dir: Path) -> float:
    """Read a finished campaign's cumulative shadow API-cost estimate, if present."""
    summary = run_dir / "shared-context" / "prompt-cost-summary.json"
    try:
        data = json.loads(summary.read_text(encoding="utf-8"))
        return float(data.get("estimated_api_cost_usd", 0.0) or 0.0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def run_council_loop(
    config: Config,
    condition: str,
    from_version: int,
    to_version: int,
    limits: LoopLimits,
    *,
    judge_agent: str | None = None,
    strategy: str | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    single_turn: bool = False,
    wait_for_quota: bool | None = None,
) -> dict[str, Any]:
    """Run a council campaign on a loop until the stop condition is met or a cap trips.

    Each turn first asks a fresh model whether ``condition`` already holds in the
    repository (the /goal check); if so, the loop stops without doing more work.
    Otherwise it runs one campaign turn over the version range, records the turn's
    shadow cost against the cumulative ceiling, and either sleeps (local) or
    returns (cloud single-turn). The campaign opens proposals for human review and
    never auto-merges, so the human review point stays installed between turns.
    """
    stamp = run_id or make_run_id()
    loop_dir = config.state_dir / "loops" / f"loop-{stamp}"
    loop_dir.mkdir(parents=True, exist_ok=True)
    state_path = loop_dir / "loop-state.json"
    state = load_or_init_state(state_path, stamp, condition)

    def persist(s: LoopState) -> None:
        _atomic_json(state_path, s.to_dict())

    def check_goal() -> dict[str, Any]:
        # Evaluate against the repository: the human merges approved proposals
        # between turns, so the repo reflects accumulated progress.
        return evaluate_goal(
            config, condition, judge_agent=judge_agent, dry_run=dry_run,
            run_id=f"{stamp}-t{state.turn + 1}", wait_for_quota=wait_for_quota,
        )

    def run_turn() -> dict[str, Any]:
        try:
            run_dir = run_campaign(
                config, from_version, to_version, dry_run,
                run_id=f"{stamp}-turn{state.turn}", wait_for_quota=wait_for_quota,
                strategy=strategy,
            )
            return {"passed": True, "run_dir": str(run_dir)}
        except CouncilError as exc:
            # A campaign that fails validation is a failed turn, not a crashed loop.
            return {"passed": False, "error": str(exc), "run_dir": ""}

    def read_turn_cost(result: dict[str, Any]) -> float:
        rd = result.get("run_dir")
        return _campaign_shadow_cost(Path(rd)) if rd else 0.0

    final = run_loop(
        state, limits,
        check_goal=check_goal,
        run_turn=run_turn,
        read_turn_cost=read_turn_cost,
        persist=persist,
        sleep_fn=time.sleep,
        single_turn=single_turn,
        log=lambda m: print(f"[loop {stamp}] {m}", file=sys.stderr),
    )
    payload = final.to_dict()
    payload["loop_dir"] = str(loop_dir)
    payload["state_file"] = str(state_path)
    return payload


def next_alternate_worker(current: str, agents: list[str], handoff_to: str | None) -> str:
    """Pick the worker that takes the next version in rapid-alternate mode.

    An explicit handoff target wins. Otherwise, when exactly two agents are
    configured, alternate to the other one (the codex<->claude ping-pong). With
    more than two agents the choice is ambiguous, so an explicit target is
    required.
    """
    if handoff_to:
        return handoff_to
    others = [a for a in agents if a != current]
    if len(others) == 1:
        return others[0]
    raise CouncilError(
        "--handoff-to is required when more than two agents are configured "
        f"(configured: {sorted(agents)})"
    )


def build_handoff_document(
    version: int,
    worker: str,
    handoff_to: str,
    next_version: int,
    validation_passed: bool,
    changed_count: int,
    tree_dirty: bool,
    continuation_prompt: str,
    has_next: bool,
    config_hint: str = "<config>",
) -> tuple[str, dict[str, Any]]:
    """Build the rapid-alternate handoff: a stop-point document for the next worker.

    Pure and testable. Returns the markdown document plus a structured summary.
    The document hands the baton to ``handoff_to`` with the exact next command and
    a ready-to-paste continuation prompt, or declares the alternation complete.
    """
    tree = "dirty" if tree_dirty else "clean"
    verdict = "PASSED" if validation_passed else "FAILED"
    next_cmd = (
        f"hermes-legion-commander council --config {config_hint} alternate "
        f"--version {next_version} --worker {handoff_to} --handoff-to {worker}"
    )
    if has_next:
        handoff_line = (
            f"HANDOFF: v{version} implemented by {worker} (tree {tree}, validation {verdict.lower()}). "
            f"Resume at v{next_version} with {handoff_to}."
        )
        next_block = (
            f"## Next command\n```\n{next_cmd}\n```\n\n"
            f"## Continuation prompt for {handoff_to.upper()}\n"
            f"Paste this to {handoff_to} (or let the next `alternate` turn issue it):\n\n"
            f"```\n{continuation_prompt}\n```\n"
        )
    else:
        handoff_line = (
            f"HANDOFF: v{version} implemented by {worker} (tree {tree}, validation {verdict.lower()}). "
            f"No further roadmap versions; alternation complete."
        )
        next_block = "## Next\nNo further roadmap versions in range. Alternation complete; review and merge.\n"

    markdown = (
        f"# Rapid-alternate handoff: v{version}"
        + (f" \u2192 v{next_version}\n\n" if has_next else " (final)\n\n")
        + f"- Implemented by: **{worker}**\n"
        + f"- Validation: **{verdict}**\n"
        + f"- Changed files: {changed_count}\n"
        + f"- Worktree: {tree}\n"
        + (f"- Next worker: **{handoff_to}**\n" if has_next else "")
        + (f"- Next version: v{next_version}\n\n" if has_next else "\n")
        + "This is a stop point. The implementation is left in the run worktree for human review; "
        + "nothing was merged, pushed, or committed to the target branch.\n\n"
        + next_block
        + f"\n{handoff_line}\n"
    )
    summary = {
        "version": version,
        "implemented_by": worker,
        "handoff_to": handoff_to if has_next else None,
        "next_version": next_version if has_next else None,
        "validation_passed": validation_passed,
        "changed_files": changed_count,
        "tree_dirty": tree_dirty,
        "has_next": has_next,
        "handoff_line": handoff_line,
    }
    return markdown, summary


def run_alternate(
    config: Config,
    version: int,
    worker: str,
    handoff_to: str | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    wait_for_quota: bool | None = None,
    to_version: int | None = None,
    pr_options: PRWorkflowOptions | None = None,
) -> dict[str, Any]:
    """Rapid-alternate mode: implement ONE version with ONE worker, then stop and hand off.

    Unlike the collaborative council campaign (which auto-continues a version
    range with multiple roles) and competitive convergence (two candidates per
    version), rapid alternate runs a single chosen worker on a single version,
    validates it, and then STOPS, emitting a structured handoff that names the
    next worker (codex<->claude) and the exact command and prompt to continue.
    Nothing is merged; the worktree is left for review.
    """
    if worker not in config.agents:
        raise CouncilError(f"--worker must name a configured agent {sorted(config.agents)}")
    if pr_options is not None and pr_options.active:
        worker_agent = config.agents[worker]
        pr_options = dataclasses.replace(
            pr_options,
            actor=actor_from_worker(worker, worker_agent.runtime, worker_agent.provider),
        )
    next_worker = next_alternate_worker(worker, sorted(config.agents), handoff_to)
    if next_worker not in config.agents:
        raise CouncilError(f"--handoff-to must name a configured agent {sorted(config.agents)}")
    if not (config.repo / ".git").exists():
        raise CouncilError(f"not a Git checkout: {config.repo}")

    stamp = run_id or f"v{version}-{make_run_id()}"
    run_dir = config.state_dir / f"alternate-{stamp}"
    worktree = run_dir / "worktree"
    pr_options = pr_options or PRWorkflowOptions(actor=worker, mode="alternating")
    branch_slug = pr_options.slug or f"{config.repo.name}-v{version}"
    branch = (
        pr_branch_name(actor=pr_options.actor or worker, mode="alternating", slug=branch_slug, stamp=stamp)
        if pr_options.active
        else f"hermes-legion/rapid-alternate/{stamp}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    base_info: dict[str, Any] = {"base_ref": "HEAD"}
    if not worktree.exists():
        if git(config.repo, "status", "--porcelain"):
            raise CouncilError("repository must be clean before a rapid-alternate turn")
        if pr_options.active:
            try:
                base_info = fetch_base(config.repo, remote=pr_options.remote, base_branch=pr_options.base_branch)
            except PRWorkflowError as exc:
                raise CouncilError(str(exc)) from exc
        git(config.repo, "worktree", "add", "-b", branch, str(worktree), str(base_info.get("base_ref", "HEAD")))

    roadmap_path, roadmap, roadmap_files = roadmap_context(config)
    section = extract_version_section(roadmap, version)
    if not section.strip():
        raise CouncilError(f"roadmap has no v{version} section to implement")
    library = library_context(config)

    dangerous, reasons = assess_change_risk(section)
    if dangerous:
        require_approval(
            run_dir, stamp, "dangerous-intent",
            "rapid-alternate version touches security, safety, authority, deployment, or credential boundaries",
            {"version": version, "worker": worker, "matched_terms": reasons, "roadmap": str(roadmap_path)},
        )

    prompt = version_implement_prompt(version, section, "", "", library, config.subagent_cap)
    _, changes = run_agent_with_change_capture(
        config, worker, prompt, worktree, run_dir / f"v{version}" / "03-implementation",
        dry_run, wait_for_quota,
    )

    core_feature = core_feature_from_section(section, version)
    requirements = version_validation_requirements(
        version, section, bool(changes), config.tests_dir, config.experiments_dir, config.results_dir
    )
    result = run_version_validation(
        config, worktree, run_dir, version, requirements, sorted(set(changes)), dry_run
    )
    write_version_result_summary(config, worktree, version, core_feature, result)

    next_version = version + 1
    try:
        next_section = extract_version_section(roadmap, next_version)
    except CouncilError:
        next_section = ""
    has_next = bool(next_section.strip()) and (to_version is None or next_version <= to_version)
    continuation_prompt = ""
    if has_next:
        continuation_prompt = (
            f"You are {next_worker}, continuing a rapid-alternate relay. {worker} implemented v{version} "
            f"in the shared branch; build on that work, do not restart it. Implement the next version below.\n\n"
            + version_implement_prompt(next_version, next_section, "", "", library, config.subagent_cap)
        )

    tree_dirty = bool(git(worktree, "status", "--porcelain"))
    markdown, summary = build_handoff_document(
        version, worker, next_worker, next_version, bool(result.get("passed")),
        len(changes), tree_dirty, continuation_prompt, has_next,
        config_hint=str(getattr(config, "config_path", "<config>")),
    )
    handoff_path = run_dir / "HANDOFF.md"
    handoff_path.write_text(markdown, encoding="utf-8")
    _atomic_json(run_dir / "handoff.json", summary)

    # Non-blocking review proposal: nothing is auto-merged; the worktree awaits review.
    _atomic_json(run_dir / "review-request.json", {
        "run_id": stamp,
        "mode": "rapid-alternate",
        "version": version,
        "implemented_by": worker,
        "branch": branch,
        "worktree": str(worktree),
        "validation_passed": bool(result.get("passed")),
        "reason": "rapid-alternate implementation requires repository-owner review before merge",
    })

    pr_result = None
    if bool(result.get("passed")) and pr_options.active:
        try:
            pr_result = _finalize_pr_workflow(
                options=pr_options,
                run_dir=run_dir,
                worktree=worktree,
                branch=branch,
                run_id=stamp,
                mode="alternating",
                title=pr_options.title or f"Legion Commander alternating v{version} by {worker}",
                summary=(
                    f"Implements roadmap v{version} with Legion Commander rapid alternating mode using worker `{worker}`. "
                    f"The branch starts from latest `{pr_options.remote}/{pr_options.base_branch}` and is opened for review before merging to `{pr_options.base_branch}`."
                ),
                validation=f"- Version validation: {'PASS' if result.get('passed') else 'FAIL'}\n- Handoff: `{handoff_path}`",
                artifacts=[str(handoff_path), str(run_dir / "review-request.json")],
                extra={"version": version, "worker": worker, "handoff_to": next_worker},
                dry_run=dry_run,
            )
        except PRWorkflowError as exc:
            raise CouncilError(str(exc)) from exc
    return {
        "mode": "rapid-alternate",
        "run_id": stamp,
        "run_dir": str(run_dir),
        "branch": branch,
        "worktree": str(worktree),
        "version": version,
        "implemented_by": worker,
        "validation_passed": bool(result.get("passed")),
        "changed_files": len(changes),
        "handoff": summary,
        "handoff_document": str(handoff_path),
        "continuation_prompt_file": str(handoff_path),
        "pull_request": pr_result,
        "stopped": True,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("config/model_council.toml"))
    p.add_argument(
        "--roadmap",
        type=Path,
        help="explicit roadmap markdown file to drive this run; overrides the "
        "config roadmap_path and is honored even if it lives outside docs/ or is "
        "not named *roadmap*.md",
    )
    sub = p.add_subparsers(dest="action", required=True)
    pf = sub.add_parser("preflight", help="Locally discover and read roadmap files without invoking a worker CLI or model API")
    pf.add_argument("--repo", type=Path, help="target repository override")
    pf.add_argument("--preview-chars", type=int, default=800, help="implementation-spine preview; use 0 to disable")
    pf.add_argument("--verbose", action="store_true", help="include document headings, release aliases, and every parsed version entry")
    r = sub.add_parser("research")
    r.add_argument("--as-of", type=dt.date.fromisoformat, default=dt.date.today())
    r.add_argument("--lookback-days", type=int)
    r.add_argument("--max-findings", type=int)
    r.add_argument("--question", default="")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--run-id")
    r.add_argument("--no-wait", action="store_true", help="pause on quota exhaustion instead of sleeping")
    c = sub.add_parser("code")
    group = c.add_mutually_exclusive_group(required=True)
    group.add_argument("--task")
    group.add_argument("--task-file", type=Path)
    c.add_argument("--with-research", action="store_true")
    c.add_argument("--budget", choices=("economy", "balanced", "quality"))
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--run-id")
    c.add_argument("--no-wait", action="store_true", help="pause on quota exhaustion instead of sleeping")
    init = sub.add_parser("init", help="Create and populate docs/roadmap.md with an initial v0.0.1 pipeline")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--run-id")
    init.add_argument("--no-wait", action="store_true")
    cp = sub.add_parser("campaign", help="Run a full or staggered roadmap version campaign")
    cp.add_argument("--from-version", type=int, required=True)
    cp.add_argument("--to-version", type=int, required=True)
    cp.add_argument("--dry-run", action="store_true")
    cp.add_argument("--run-id")
    cp.add_argument("--strategy", choices=("full", "staggered", "alternating"), help="full uses assigned workers; alternating immediately fails over quota/entitlement-blocked stages; staggered preserves the rolling pipeline")
    cp.add_argument("--until", help="after the campaign, a fresh-model judge evaluates this stop condition against the worktree and records the verdict (the /goal gate)")
    cp.add_argument("--no-wait", action="store_true")
    _add_pr_args(cp)
    l = sub.add_parser("literature", help="Review new PDFs for the shared evidence library")
    l.add_argument("--force", action="store_true")
    l.add_argument("--validation", choices=("economy", "balanced", "quality"))
    l.add_argument("--dry-run", action="store_true")
    rs = sub.add_parser("resume", help="Resume a quota-paused or interrupted durable run")
    rs.add_argument("--run-id", required=True)
    rs.add_argument("--no-wait", action="store_true")
    ap = sub.add_parser("approve", help="Approve a blocked dangerous-intent or massive-diff phase")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--phase", required=True, help="dangerous-intent, massive-diff, or roadmap-update-vNN")
    ap.add_argument("--note", default="Approved by repository owner after review")
    wk = sub.add_parser("workers", help="Show the configured Codex CLI and Claude Code workers")
    wk.add_argument("--check", action="store_true", help="verify each configured executable is on PATH and responds to --version; no model call")
    sub.add_parser("profiles", help="Deprecated alias for 'workers'")
    rt = sub.add_parser("route", help="Explain the token-efficient model route for a task")
    rt.add_argument("--task", required=True)
    rt.add_argument("--budget", choices=("economy", "balanced", "quality"), default="balanced")
    gl = sub.add_parser("goal", help="Evaluate a stop condition with a fresh-model judge over the configured checks (the /goal primitive)")
    gl.add_argument("--condition", required=True, help="natural-language stop condition, e.g. 'all tests pass and lint is clean'")
    gl.add_argument("--judge", help="configured agent that judges the condition (default: the security_assurance agent; should differ from the generator)")
    gl.add_argument("--dry-run", action="store_true", help="run the deterministic checks and build the judge prompt without invoking the judge model")
    gl.add_argument("--run-id")
    gl.add_argument("--no-wait", action="store_true")
    lp = sub.add_parser("loop", help="Run campaigns on a loop until a stop condition is met or a budget cap trips (scheduling + run-until-met + circuit-breaker)")
    lp.add_argument("--condition", required=True, help="natural-language stop condition that ends the loop when a fresh model judges it met")
    lp.add_argument("--from-version", type=int, required=True)
    lp.add_argument("--to-version", type=int, required=True)
    lp.add_argument("--max-turns", type=int, default=10, help="hard cap on iterations (circuit-breaker)")
    lp.add_argument("--max-consecutive-failures", type=int, default=3, help="stop after this many failed turns in a row")
    lp.add_argument("--max-cost-usd", type=float, help="cumulative shadow-cost ceiling in USD (estimate-based circuit-breaker)")
    lp.add_argument("--interval", type=int, default=3600, help="seconds to sleep between turns in local mode")
    lp.add_argument("--single-turn", action="store_true", help="run exactly one turn and exit (for cloud/CI cron schedulers; state resumes across runs)")
    lp.add_argument("--judge", help="agent that judges the stop condition (default: the security_assurance agent)")
    lp.add_argument("--strategy", choices=("full", "staggered", "alternating"))
    lp.add_argument("--dry-run", action="store_true")
    lp.add_argument("--run-id")
    lp.add_argument("--no-wait", action="store_true")
    li = sub.add_parser("loop-init", help="Emit a GitHub Actions workflow that runs one loop turn per scheduled run (machine-off autonomy)")
    li.add_argument("--condition", required=True)
    li.add_argument("--from-version", type=int, required=True)
    li.add_argument("--to-version", type=int, required=True)
    li.add_argument("--cron", default="0 6 * * *", help="cron schedule for the workflow (default: 06:00 daily)")
    li.add_argument("--config-path", default="config/model_council.toml", help="config path as seen inside the CI checkout")
    li.add_argument("--max-turns", type=int, default=1)
    li.add_argument("--out", type=Path, help="write the workflow to this path instead of stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config.resolve())
        if getattr(args, "roadmap", None) is not None:
            config = dataclasses.replace(config, roadmap_path=args.roadmap.expanduser())
        if args.action == "preflight":
            print(json.dumps(roadmap_preflight(config, args.repo, args.preview_chars, args.verbose), indent=2, sort_keys=True))
        elif args.action == "research":
            report = run_research(config, args.as_of, args.lookback_days or config.lookback_days,
                                  args.max_findings or config.max_findings, args.question, args.dry_run, args.run_id, not args.no_wait)
            print(report)
        elif args.action == "code":
            task = args.task if args.task is not None else args.task_file.read_text(encoding="utf-8")
            print(run_code(config, task, args.with_research, args.dry_run, args.budget, args.run_id, not args.no_wait))
        elif args.action == "init":
            print(run_bootstrap(config, args.dry_run, args.run_id, not args.no_wait))
        elif args.action == "campaign":
            print(run_campaign(
                config, args.from_version, args.to_version, args.dry_run, args.run_id,
                not args.no_wait, args.strategy, args.until,
                _make_pr_options_from_args(args, mode="collaborating", actor="commander"),
            ))
        elif args.action == "resume":
            print(resume_run(config, args.run_id, not args.no_wait))
        elif args.action == "approve":
            if args.phase == f"roadmap-init-v{BOOTSTRAP_VERSION}":
                candidates = list(config.state_dir.glob(f"bootstrap-{args.run_id}"))
                if not candidates:
                    raise CouncilError(f"bootstrap run not found: {args.run_id}")
                run_dir = candidates[0]
                request = run_dir / "approvals" / f"roadmap-init-v{BOOTSTRAP_VERSION}.request.json"
                if not request.exists():
                    raise CouncilError("no pending bootstrap roadmap proposal")
                grant = run_dir / "approvals" / f"roadmap-init-v{BOOTSTRAP_VERSION}.grant.json"
                _atomic_json(grant, {"run_id": args.run_id, "phase": args.phase, "approved": True, "note": args.note, "approved_at": dt.datetime.now(UTC).isoformat()})
                print(apply_bootstrap_roadmap(config, run_dir))
            elif args.phase.startswith("roadmap-update-v"):
                version = int(args.phase.rsplit("v", 1)[1])
                candidates = list(config.state_dir.glob(f"campaign-{args.run_id}"))
                if not candidates:
                    raise CouncilError(f"campaign run not found: {args.run_id}")
                run_dir = candidates[0]
                request = run_dir / "approvals" / f"roadmap-update-v{version}.request.json"
                if not request.exists():
                    raise CouncilError(f"no pending roadmap proposal for v{version}")
                grant = run_dir / "approvals" / f"roadmap-update-v{version}.grant.json"
                _atomic_json(grant, {"run_id": args.run_id, "phase": args.phase, "approved": True, "note": args.note, "approved_at": dt.datetime.now(UTC).isoformat()})
                print(apply_roadmap_update(config, run_dir, version))
            else:
                print(approve_run(config, args.run_id, args.phase, args.note))
        elif args.action == "literature":
            outputs = review_library(config, args.dry_run, args.force, args.validation)
            print("\n".join(map(str, outputs)) if outputs else "No new PDFs to review.")
        elif args.action in {"workers", "profiles"}:
            do_check = bool(getattr(args, "check", False))
            payload: dict[str, Any] = {}
            failures: list[str] = []
            for name, agent in config.agents.items():
                executable = agent.command[0]
                resolved = shutil.which(executable)
                row: dict[str, Any] = {
                    "runtime": agent.runtime,
                    "executable": executable,
                    "resolved_executable": resolved,
                    "available": bool(resolved),
                    "provider": agent.provider,
                    "model": agent.model or "CLI configured default",
                    "effort": agent.effort,
                    "prompt_transport": agent.prompt_transport,
                    "output_format": agent.output_format,
                    "capabilities": list(agent.capabilities),
                    "role": agent.role,
                }
                if do_check and resolved:
                    try:
                        cp = subprocess.run(
                            [resolved, "--version"],
                            text=True,
                            capture_output=True,
                            check=False,
                            timeout=30,
                        )
                        row["version_exit_code"] = cp.returncode
                        row["version_output"] = (cp.stdout or cp.stderr).strip()[:2000]
                        if cp.returncode != 0:
                            failures.append(f"{name}: {executable} --version exited {cp.returncode}")
                    except (OSError, subprocess.SubprocessError) as exc:
                        row["version_error"] = str(exc)
                        failures.append(f"{name}: could not run {executable} --version: {exc}")
                elif do_check:
                    failures.append(f"{name}: executable not found on PATH: {executable}")
                payload[name] = row
            print(json.dumps(payload, indent=2))
            if failures:
                raise CouncilError("worker executable check failed: " + "; ".join(failures))
        elif args.action == "goal":
            print(json.dumps(
                evaluate_goal(config, args.condition, args.judge, args.dry_run, args.run_id, not args.no_wait),
                indent=2, sort_keys=True,
            ))
        elif args.action == "loop":
            limits = LoopLimits(
                max_turns=args.max_turns,
                max_consecutive_failures=args.max_consecutive_failures,
                max_cost_usd=args.max_cost_usd,
                interval_seconds=args.interval,
            )
            print(json.dumps(
                run_council_loop(
                    config, args.condition, args.from_version, args.to_version, limits,
                    judge_agent=args.judge, strategy=args.strategy, dry_run=args.dry_run,
                    run_id=args.run_id, single_turn=args.single_turn,
                    wait_for_quota=not args.no_wait,
                ),
                indent=2, sort_keys=True,
            ))
        elif args.action == "loop-init":
            workflow = cloud_workflow_yaml(
                cron=args.cron, config_path=args.config_path, condition=args.condition,
                from_version=args.from_version, to_version=args.to_version, max_turns=args.max_turns,
            )
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(workflow, encoding="utf-8")
                print(str(args.out))
            else:
                print(workflow)
        else:
            print(json.dumps(dataclasses.asdict(route_task(args.task, args.budget)), indent=2))
        return 0
    except (CouncilError, OSError, ValueError, subprocess.TimeoutExpired, tomllib.TOMLDecodeError) as exc:
        print(f"model council error: {exc}", file=sys.stderr)
        return 2


def alternate_main(argv: list[str] | None = None) -> int:
    """Top-level entry for rapid alternate mode (the `alternating` command).

    Implements ONE version with ONE worker, then stops and hands off to the other.
    """
    ap = argparse.ArgumentParser(
        prog="hermes-legion-commander alternating",
        description=(
            "Rapid alternate mode: implement ONE roadmap version with ONE worker, then stop at the "
            "version boundary and hand off to the other worker (codex<->claude) to continue."
        ),
    )
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--repo", type=Path, help="override the target repository from the config")
    ap.add_argument("--roadmap", type=Path, help="override the roadmap file from the config")
    ap.add_argument("--version", type=int, required=True, help="roadmap version to implement this turn")
    ap.add_argument("--worker", required=True, help="configured agent that implements this version")
    ap.add_argument("--handoff-to", help="agent that takes the next version (default: the other agent when exactly two are configured)")
    ap.add_argument("--to-version", type=int, help="final version of the relay; beyond it the handoff reports completion")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id")
    ap.add_argument("--no-wait", action="store_true")
    _add_pr_args(ap)
    args = ap.parse_args(argv)
    try:
        config = load_config(args.config.resolve())
        if args.roadmap is not None:
            config = dataclasses.replace(config, roadmap_path=args.roadmap.expanduser())
        if args.repo is not None:
            config = dataclasses.replace(config, repo=args.repo.resolve())
        print(json.dumps(
            run_alternate(
                config, args.version, args.worker, args.handoff_to,
                args.dry_run, args.run_id, not args.no_wait, args.to_version,
                _make_pr_options_from_args(args, mode="alternating", actor=str(args.worker)),
            ),
            indent=2, sort_keys=True,
        ))
        return 0
    except (CouncilError, OSError, ValueError, subprocess.TimeoutExpired, tomllib.TOMLDecodeError) as exc:
        print(f"model council error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
