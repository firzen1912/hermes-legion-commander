#!/usr/bin/env python3
"""Competitive convergence mode for an external Git repository.

This is one of Hermes Legion Commander's three execution modes (the other two --
collaborative council and rapid alternate -- live in model_council.py).

Two competitors (any two configured agents -- codex<->claude, or any provider/model
pair) independently execute every competition role in separate Git worktrees. The
run AUTO-CONTINUES across the version range. Hermes then runs deterministic checks,
collects two independent comparative judgements, selects a provisional winner, and
creates a third converged worktree that both workers improve in sequence. Nothing
is merged, pushed, deployed, tagged, published, or released.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

try:
    from .roadmap import (
        campaign_versions,
        extract_version_range,
        implementation_section,
        parse_version_entries,
        release_versions,
        version_keys,
    )
    from .stop_condition import (
        deterministic_all_passed,
        parse_stop_verdict,
        run_deterministic_checks,
        stop_condition_prompt,
    )
    from .prompt_contracts import subagent_delegation_contract
    from .worker_runtime import (
        RUNTIME_EXECUTABLES,
        build_prompt_with_shared_context,
        create_worker_context_snapshot,
        ensure_shared_context,
        is_quota_error as runtime_quota_error,
        normalize_worker_output,
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
    from hermes_legion_commander.roadmap import (
        campaign_versions,
        extract_version_range,
        implementation_section,
        parse_version_entries,
        release_versions,
        version_keys,
    )
    from hermes_legion_commander.stop_condition import (
        deterministic_all_passed,
        parse_stop_verdict,
        run_deterministic_checks,
        stop_condition_prompt,
    )
    from hermes_legion_commander.prompt_contracts import subagent_delegation_contract
    from hermes_legion_commander.worker_runtime import (
        RUNTIME_EXECUTABLES,
        build_prompt_with_shared_context,
        create_worker_context_snapshot,
        ensure_shared_context,
        is_quota_error as runtime_quota_error,
        normalize_worker_output,
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

UTC = dt.timezone.utc
DANGEROUS_TERMS = (
    "deploy", "production", "credential", "secret", "private key", "rotate key",
    "authentication", "authorization", "cryptograph", "mavlink command", "arming",
    "failsafe", "kill switch", "flight controller", "firmware", "migration",
    "force push", "rewrite history", "release", "publish", "delete data",
)
SECURITY_BLOCKERS = {
    "authority_bypass", "safety_bypass", "false_success",
    "duplicate_hazardous_execution", "cross_vehicle_acceptance",
    "invented_evidence", "release_trust_bypass", "integrity_corruption",
}
DEFAULT_COMPETITORS = ("gpt", "claude")

ADVERSARIAL_EVALUATOR_STANCE = """
ADVERSARIAL EVALUATOR STANCE:
- Default to doubt. Assume the implementation under review is BROKEN until the evidence proves otherwise.
  Your job is not to approve; it is to find what fails.
- Do not praise. A clean-looking diff is not evidence, and the author's rationale for why it is correct
  is not evidence; the only evidence is what the code actually does.
- Verify by acting where you can: read the real output of the deterministic checks rather than judging
  that the code "looks correct".
- Judge behavior against the roadmap obligation, not stated intent. Probe the edge cases the author
  skipped, the failure paths, and the gap between "it runs" and "it is right".
- A review that never finds a blocker across a real workload is not a review; surface concrete,
  file-level findings or state explicitly, with evidence, that none remain.
"""
CANDIDATE_ROLES = (
    "roadmap_plan_reviewer",
    "researcher",
    "literature_reviewer",
    "prototyper",
    "code_polisher",
    "security_assurance",
    "validation_artifacts",
)
CROSS_VALIDATION_ROLES = ("cross_reviewer", "cross_polisher")
FINAL_ROLES = ("judge", "converger", "final_verifier")
ALL_ROLES = (*CANDIDATE_ROLES, *CROSS_VALIDATION_ROLES, *FINAL_ROLES)
ROLE_STAGE = {
    "roadmap_plan_reviewer": "00-roadmap-plan-review",
    "researcher": "01-research",
    "literature_reviewer": "02-literature-review",
    "prototyper": "03-prototype",
    "code_polisher": "04-code-polish",
    "security_assurance": "05-security-assurance",
    "validation_artifacts": "06-validation-artifacts",
    "cross_reviewer": "06b-cross-review",
    "cross_polisher": "06c-cross-polish",
    "judge": "07-judge",
    "converger": "08-converge",
    "final_verifier": "09-final-verify",
}
ROLE_OBJECTIVES = {
    "roadmap_plan_reviewer": (
        "Audit the requested roadmap range, repository seams, dependencies, exclusions, "
        "acceptance criteria, risks, and precise evidence questions. Create or improve "
        "planning documentation when useful, but do not broaden the requested range."
    ),
    "researcher": (
        "Gather current primary-source evidence, upstream implementation patterns, standards, "
        "benchmarks, and security implications. Record stable citations and clearly separate "
        "verified facts from inference. Add bounded research notes when useful."
    ),
    "literature_reviewer": (
        "Review repository literature, PDFs, prior research notes, and cited evidence. Challenge "
        "unsupported claims, identify applicability limits, and connect evidence to concrete "
        "implementation and validation decisions."
    ),
    "prototyper": (
        "Implement the complete bounded roadmap range as an independent candidate. Add or modify "
        "source, configuration, schemas, migrations, tests, experiments, and documentation as needed."
    ),
    "code_polisher": (
        "Review and improve the candidate implementation for architecture, maintainability, typing, "
        "interoperability, failure semantics, compatibility, and test quality. Apply fixes directly."
    ),
    "security_assurance": (
        "Perform an adversarial security and safety review, then fix verified defects involving "
        "authority, authentication, replay, integrity, false success, unsafe defaults, isolation, "
        "or evidence truthfulness."
    ),
    "validation_artifacts": (
        "Complete focused tests, deterministic host-safe experiments, result collectors, and concise "
        "iteration evidence. Do not fabricate HIL, field, or physical-system results."
    ),
    "cross_reviewer": (
        "Adversarially review the opponent candidate after independent implementation. Identify concrete "
        "security, correctness, regression, test-quality, performance, and documentation risks with file-level "
        "evidence. Do not edit any repository files."
    ),
    "cross_polisher": (
        "Review the opponent's cross-validation findings against your own candidate. Fix true-positive defects, "
        "add tests or documentation for verified gaps, and explicitly rebut false positives with evidence."
    ),
    "judge": (
        "Independently compare both completed candidates using patches, deterministic results, cross-validation "
        "findings, roadmap coverage, security, correctness, maintainability, performance evidence, and honest scope."
    ),
    "converger": (
        "Improve the provisional winning implementation in the convergence worktree. Preserve its "
        "strongest properties, incorporate superior non-conflicting ideas from the other candidate, "
        "resolve cross-validation and judge findings, and leave a coherent tested implementation."
    ),
    "final_verifier": (
        "Perform a final read-only adversarial verification of the converged implementation. Report remaining "
        "blockers, residual risks, test gaps, and release-readiness evidence without editing files."
    ),
}

CROSS_FINDING_SEVERITIES = ("critical", "high", "medium", "low", "info")
CROSS_FINDING_CATEGORIES = (
    "security", "correctness", "regression", "test_quality", "performance",
    "maintainability", "documentation", "roadmap_scope", "evidence_truthfulness",
)
CROSS_BLOCKING_SEVERITIES = {"critical", "high"}
CROSS_BLOCKING_CATEGORIES = {"security", "correctness", "regression", "evidence_truthfulness"}


class CompetitionError(RuntimeError):
    pass


class QuotaPaused(CompetitionError):
    def __init__(self, agent: str, retry_at: str, stage_dir: Path):
        super().__init__(f"{agent} quota paused until {retry_at}; resume this campaign later")
        self.agent = agent
        self.retry_at = retry_at
        self.stage_dir = stage_dir


class ApprovalRequired(CompetitionError):
    pass


@dataclasses.dataclass(frozen=True)
class VersionRange:
    start: int
    end: int

    @property
    def key(self) -> str:
        return f"v{self.start}-v{self.end}"

    def validate(self) -> None:
        if self.start < 1 or self.start > self.end:
            raise CompetitionError("version range must satisfy 1 <= from-version <= to-version")


@dataclasses.dataclass(frozen=True)
class Agent:
    name: str
    role: str
    runtime: str
    provider: str
    model: str
    effort: str
    command: tuple[str, ...]
    timeout_seconds: int
    prompt_transport: str = "stdin"
    output_format: str = "text"
    capabilities: tuple[str, ...] = ()
    unset_env: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RoleProfile:
    role: str
    agent: str
    model: str
    effort: str
    instructions: str
    command: tuple[str, ...] | None = None


@dataclasses.dataclass(frozen=True)
class Benchmark:
    id: str
    command: tuple[str, ...]
    direction: str
    weight: float


@dataclasses.dataclass(frozen=True)
class Config:
    repo: Path
    plan: Path
    registry: Path | None
    state_dir: Path
    base_ref: str
    branch_prefix: str
    checks: tuple[tuple[str, ...], ...]
    benchmarks: tuple[Benchmark, ...]
    weights: dict[str, float]
    agents: dict[str, Agent]
    role_matrix: dict[str, dict[str, RoleProfile]]
    quota_wait: bool
    quota_retry_seconds: int
    quota_max_retry_seconds: int
    massive_files: int
    massive_lines: int
    competitors: tuple[str, ...] = DEFAULT_COMPETITORS
    subagent_cap: int = 5


def _make_pr_options_from_args(args: Any, *, mode: str = "competitive") -> PRWorkflowOptions:
    enabled = bool(getattr(args, "pr", False) or getattr(args, "push_branch", False) or getattr(args, "open_pr", False))
    return PRWorkflowOptions(
        enabled=enabled,
        base_branch=str(getattr(args, "pr_base", "dev") or "dev"),
        remote=str(getattr(args, "pr_remote", "origin") or "origin"),
        actor="commander",
        mode=mode,
        slug=getattr(args, "pr_slug", None),
        push=bool(getattr(args, "push_branch", False) or getattr(args, "open_pr", False)),
        open_pr=bool(getattr(args, "open_pr", False)),
        draft=bool(getattr(args, "draft_pr", False)),
        title=getattr(args, "pr_title", None),
        gh=getattr(args, "gh", None),
    )


def _add_pr_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pr", action="store_true", help="create Legion Commander competitive branches from latest origin/dev-equivalent base")
    parser.add_argument("--pr-base", default="dev", help="base branch for candidate/converged work and pull request (default: dev)")
    parser.add_argument("--pr-remote", default="origin", help="Git remote to fetch/push (default: origin)")
    parser.add_argument("--pr-slug", help="short branch slug; Commander appends the version range/run stamp")
    parser.add_argument("--push-branch", action="store_true", help="push the converged review branch after committing generated changes")
    parser.add_argument("--open-pr", action="store_true", help="push the converged review branch and open a GitHub pull request back to --pr-base")
    parser.add_argument("--draft-pr", action="store_true", help="open the pull request as a draft")
    parser.add_argument("--pr-title", help="custom pull request title")
    parser.add_argument("--gh", type=Path, help="path to gh executable for PR creation; auto-detects PATH and common Windows installs")


def _finalize_competition_pr(
    *,
    cfg: Config,
    vr: VersionRange,
    options: PRWorkflowOptions,
    worktree: Path,
    branch: str,
    result: dict[str, Any],
    no_wait: bool,
) -> dict[str, Any] | None:
    if not options.active:
        return None
    root = campaign_root(cfg, vr)
    title = options.title or f"Legion Commander competitive v{vr.start}-v{vr.end}"
    try:
        commit = commit_all_if_changed(worktree, message=title)
        body = build_pr_body(
            mode="competitive",
            branch=branch,
            base_branch=options.base_branch,
            run_id=vr.key,
            summary=(
                f"Converges independent competitive candidates for v{vr.start}-v{vr.end}. "
                f"Candidate branches are named with `legion-commander-codex-competitive/` and "
                f"`legion-commander-claude-competitive/` style prefixes where the worker runtime can be inferred. "
                f"The final review branch is opened against `{options.base_branch}` for human review before merge."
            ),
            validation=(
                f"- Provisional winner: `{result.get('provisional_winner')}`\n"
                f"- Cross-validation artifacts: `{root / 'cross-validation-summary.json'}`\n"
                f"- Converged result: `{root / 'converged-result.json'}`\n"
                f"- Final verification: `{root / 'final-verification-summary.json'}`"
            ),
            artifacts=[
                str(root / "manifest.json"),
                str(root / "comparison-report.json"),
                str(root / "cross-validation-summary.json"),
                str(root / "converged-result.json"),
                str(root / "final-verification-summary.json"),
            ],
            extra={"range_id": vr.key, "no_wait": no_wait},
        )
        payload: dict[str, Any] = {
            "enabled": True,
            "mode": "competitive",
            "range_id": vr.key,
            "branch": branch,
            "base_branch": options.base_branch,
            "remote": options.remote,
            "worktree": str(worktree),
            "commit": commit,
            "title": title,
            "body": body,
        }
        if options.push or options.open_pr:
            payload["pushed"] = push_branch(worktree, branch=branch, remote=options.remote)
        if options.open_pr:
            payload["pull_request"] = create_or_view_pr(
                worktree, branch=branch, base_branch=options.base_branch, title=title,
                body=body, draft=options.draft, gh_path=options.gh, remote=options.remote,
            )
        write_pr_artifacts(root, payload)
        return payload
    except PRWorkflowError as exc:
        raise CompetitionError(str(exc)) from exc


def now() -> str:
    return dt.datetime.now(UTC).isoformat()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run(command: list[str], cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def git(repo: Path, *args: str, cwd: Path | None = None) -> str:
    cp = run(["git", *args], cwd or repo)
    if cp.returncode:
        raise CompetitionError((cp.stderr or cp.stdout).strip())
    return cp.stdout.strip()


def resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (base / p).resolve()


def string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise CompetitionError(f"{field} must be a non-empty array of strings")
    return tuple(value)


def _load_agents(agents_raw: dict[str, Any]) -> dict[str, Agent]:
    if len(agents_raw) != 2:
        raise CompetitionError(
            "checkpoint competition requires exactly two [agents.*] tables "
            f"(the two competitors); got {sorted(agents_raw)}"
        )
    agents: dict[str, Agent] = {}
    for name, item in agents_raw.items():
        if not isinstance(item, dict):
            raise CompetitionError(f"[agents.{name}] must be a table")
        runtime = str(item.get("runtime", ""))
        if not runtime:
            raise CompetitionError(
                f"agents.{name}.runtime is required "
                f"(built-in: 'codex-cli', 'claude-code'; or any custom runtime id)"
            )
        command = string_list(item.get("command"), f"agents.{name}.command")
        # Keep the executable safety check for the two built-in runtimes; custom
        # runtimes accept any command (output is parsed by output_format with a
        # plain-text fallback, so any provider's CLI can compete).
        expected_executable = RUNTIME_EXECUTABLES.get(runtime)
        if expected_executable is not None and Path(command[0]).name.lower().removesuffix(".exe") != expected_executable:
            raise CompetitionError(f"agents.{name}.command must launch {expected_executable}")
        prompt_transport = str(item.get("prompt_transport", "stdin"))
        if prompt_transport not in {"stdin", "argument"}:
            raise CompetitionError(f"agents.{name}.prompt_transport must be 'stdin' or 'argument'")
        agents[name] = Agent(
            name=name,
            role=str(item.get("role", "")),
            runtime=runtime,
            provider=str(item.get("provider", "")),
            model=str(item.get("model", "")),
            effort=str(item.get("effort", "medium")),
            command=command,
            timeout_seconds=int(item.get("timeout_seconds", 21600)),
            prompt_transport=prompt_transport,
            output_format=str(item.get("output_format", "text")),
            capabilities=tuple(str(x) for x in item.get("capabilities", [])),
            unset_env=tuple(str(x) for x in item.get("unset_env", [])),
        )
    return agents


def _load_role_matrix(raw: dict[str, Any], agents: dict[str, Agent]) -> dict[str, dict[str, RoleProfile]]:
    matrix_raw = raw.get("role_matrix", {})
    if matrix_raw is None:
        matrix_raw = {}
    if not isinstance(matrix_raw, dict):
        raise CompetitionError("[role_matrix] must be a table")
    matrix: dict[str, dict[str, RoleProfile]] = {}
    for role in ALL_ROLES:
        role_raw = matrix_raw.get(role, {})
        if role_raw is None:
            role_raw = {}
        if not isinstance(role_raw, dict):
            raise CompetitionError(f"[role_matrix.{role}] must be a table")
        matrix[role] = {}
        for agent_name in agents:
            item = role_raw.get(agent_name, {})
            if item is None:
                item = {}
            if not isinstance(item, dict):
                raise CompetitionError(f"[role_matrix.{role}.{agent_name}] must be a table")
            command = item.get("command")
            matrix[role][agent_name] = RoleProfile(
                role=role,
                agent=agent_name,
                model=str(item.get("model", agents[agent_name].model)),
                effort=str(item.get("effort", agents[agent_name].effort)),
                instructions=str(item.get("instructions", ROLE_OBJECTIVES[role])),
                command=string_list(command, f"role_matrix.{role}.{agent_name}.command") if command is not None else None,
            )
    return matrix


def load_config(path: Path, repo_override: Path | None = None) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    base = path.parent
    root = raw.get("competition")
    agents_raw = raw.get("agents")
    if not isinstance(root, dict) or not isinstance(agents_raw, dict):
        raise CompetitionError("missing [competition] or [agents.*] configuration")
    repo = repo_override.resolve() if repo_override else resolve(base, str(root["repo"]))
    agents = _load_agents(agents_raw)
    role_matrix = _load_role_matrix(raw, agents)
    benches = tuple(
        Benchmark(
            str(x["id"]), tuple(map(str, x["command"])),
            str(x.get("direction", "lower")), float(x.get("weight", 1)),
        )
        for x in root.get("benchmarks", [])
    )
    registry_value = root.get("registry")
    return Config(
        repo=repo,
        plan=resolve(repo, str(root.get("plan", "docs/roadmap.md"))),
        registry=resolve(repo, str(registry_value)) if registry_value else None,
        state_dir=resolve(base, str(root.get("state_dir", "../state/checkpoint-competition"))),
        base_ref=str(root.get("base_ref", "HEAD")),
        branch_prefix=str(root.get("branch_prefix", "legion-competition")),
        checks=tuple(string_list(x, "competition.checks[]") for x in root.get("checks", [["python", "-m", "pytest", "-q"]])),
        benchmarks=benches,
        weights={str(k): float(v) for k, v in root.get(
            "weights", {"security": .35, "correctness": .35, "maintainability": .2, "performance": .1}
        ).items()},
        agents=agents,
        role_matrix=role_matrix,
        competitors=tuple(agents),
        subagent_cap=max(0, int(root.get("subagent_cap", 5))),
        quota_wait=bool(root.get("quota_wait", True)),
        quota_retry_seconds=max(60, int(root.get("quota_retry_seconds", 900))),
        quota_max_retry_seconds=max(60, int(root.get("quota_max_retry_seconds", 21600))),
        massive_files=max(1, int(root.get("massive_files", 25))),
        massive_lines=max(1, int(root.get("massive_lines", 1500))),
    )


def _display_relative(path: Path, root: Path) -> str:
    """Relative path for display, falling back to the absolute path when the
    file is outside ``root`` (e.g. an explicit roadmap given outside the repo)."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def discover_roadmap_files(cfg: Config) -> tuple[Path, ...]:
    """Discover roadmap files; the configured/overridden ``plan`` is the
    authoritative primary and is included even outside ``docs/`` or when not
    named ``*roadmap*.md``."""
    docs = (cfg.repo / "docs").resolve()
    matches = sorted(
        (
            path.resolve() for path in docs.rglob("*")
            if path.is_file() and path.suffix.lower() == ".md" and "roadmap" in path.name.lower()
        ),
        key=lambda path: str(path.relative_to(docs)).lower(),
    ) if docs.is_dir() else []
    preferred = cfg.plan.resolve()
    canonical = (docs / "roadmap.md").resolve()
    ordered: list[Path] = []
    if preferred.is_file():
        ordered.append(preferred)
    for candidate in (canonical, *matches):
        if candidate in matches and candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def roadmap_context(cfg: Config) -> tuple[Path, str, tuple[Path, ...]]:
    files = discover_roadmap_files(cfg)
    if not files:
        raise CompetitionError(f"no *roadmap*.md file found under {(cfg.repo / 'docs').resolve()}")
    chunks = []
    for path in files:
        content = path.read_text(encoding="utf-8")
        scoped = implementation_section(content)
        scope_note = (
            f"implementation section: {scoped.heading}"
            if scoped.found else "full-document fallback: no version-by-version implementation heading"
        )
        chunks.append(f"<!-- ROADMAP SOURCE: {_display_relative(path, cfg.repo)}; {scope_note} -->\n{scoped.text}")
    return files[0], "\n\n".join(chunks), files


def roadmap_preflight(cfg: Config, preview_chars: int = 800, verbose: bool = False) -> dict[str, Any]:
    if preview_chars < 0:
        raise CompetitionError("preview chars must be non-negative")
    repo = cfg.repo.resolve()
    docs = (repo / "docs").resolve()
    if not repo.is_dir():
        raise CompetitionError(f"target repository directory does not exist: {repo}")
    files = discover_roadmap_files(cfg)
    if not files:
        raise CompetitionError(f"no roadmap found: pass --roadmap <file.md> or add docs/*roadmap*.md under {repo}")
    entries: list[dict[str, Any]] = []
    heading_re = re.compile(r"(?im)^#{1,6}\s+(.+)$")
    for path in files:
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CompetitionError(f"roadmap is not valid UTF-8: {path}: {exc}") from exc
        document_headings = [m.group(1).strip() for m in heading_re.finditer(content)]
        scoped, parsed_entries = parse_version_entries(content)
        scoped_headings = [m.group(1).strip() for m in heading_re.finditer(scoped.text)]
        ints = campaign_versions(parsed_entries)
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
            "campaign_version_count": len(ints),
            "campaign_version_range": [min(ints), max(ints)] if ints else [],
            "phase_sample": version_keys(parsed_entries) if len(parsed_entries) <= 6 else version_keys(parsed_entries)[:3] + ["..."] + version_keys(parsed_entries)[-3:],
            "special_versions_detected": [entry.version for entry in parsed_entries if not entry.version.isdigit()][:200],
            "preview": scoped.text[:preview_chars] if preview_chars else "",
            **({
                "headings": scoped_headings[:200],
                "document_headings": document_headings[:200],
                "versions_detected": version_keys(parsed_entries)[:200],
                "campaign_versions_detected": ints[:200],
                "release_versions_detected": release_versions(parsed_entries)[:200],
                "version_entries": [
                    {"version": entry.version, "release_version": entry.release_version, "title": entry.title}
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


def validate(cfg: Config, require_workers: bool = True) -> None:
    if not (cfg.repo / ".git").exists():
        raise CompetitionError(f"target repo is not a Git checkout: {cfg.repo}")
    if git(cfg.repo, "status", "--porcelain"):
        raise CompetitionError("target repository must be clean")
    if not discover_roadmap_files(cfg):
        raise CompetitionError(f"no *roadmap*.md file found under {(cfg.repo / 'docs').resolve()}")
    if cfg.registry and not cfg.registry.is_file():
        raise CompetitionError(f"baseline registry missing: {cfg.registry}")
    if require_workers:
        missing = sorted({agent.command[0] for agent in cfg.agents.values() if shutil.which(agent.command[0]) is None})
        if missing:
            raise CompetitionError(f"required worker CLIs not found: {', '.join(missing)}")


def role_agent(cfg: Config, name: str, role: str) -> Agent:
    base = cfg.agents[name]
    profile = cfg.role_matrix[role][name]
    return dataclasses.replace(
        base,
        role=f"{role}: {profile.instructions}",
        model=profile.model,
        effort=profile.effort,
        command=profile.command or base.command,
    )


def render(agent: Agent, prompt: str, *, prompt_file: Path | None = None,
           context_dir: Path | None = None, stage_dir: Path | None = None,
           cwd: Path | None = None, output_file: Path | None = None) -> list[str]:
    placeholder = Path(".")
    return render_command(
        agent, prompt, prompt_file or placeholder / "prompt.md",
        context_dir or placeholder / "shared-context", stage_dir or placeholder / "stage",
        cwd or placeholder, output_file or placeholder / "last-message.txt",
    )


def is_quota_error(cp: subprocess.CompletedProcess[str]) -> bool:
    return runtime_quota_error(cp.stdout or "", cp.stderr or "", cp.returncode)


def run_agent(
    cfg: Config,
    name: str,
    role: str,
    prompt: str,
    cwd: Path,
    stage_dir: Path,
    wait_for_quota: bool | None = None,
) -> str:
    agent = role_agent(cfg, name, role)
    stage_dir.mkdir(parents=True, exist_ok=True)
    state_path = stage_dir / "state.json"
    stdout_path = stage_dir / "stdout.md"
    raw_stdout_path = stage_dir / "raw-stdout.txt"
    stderr_path = stage_dir / "stderr.txt"
    output_file = stage_dir / "last-message.txt"
    if state_path.exists() and stdout_path.exists():
        prior = json.loads(state_path.read_text(encoding="utf-8"))
        if prior.get("status") == "completed":
            return stdout_path.read_text(encoding="utf-8")
    canonical_context = ensure_shared_context(stage_dir, cwd, agent)
    selected_name, scoped_agent, scope_routing = select_agent_for_scope(
        {name: agent}, name, prompt, cwd, canonical_context, stage_dir,
        role=role, allow_agent_switch=False,
    )
    agent = scoped_agent
    canonical_context = ensure_shared_context(stage_dir, cwd, agent)
    context_dir = create_worker_context_snapshot(stage_dir, canonical_context)
    combined_prompt = build_prompt_with_shared_context(prompt, context_dir, cwd, 120000)
    seal_worker_context_snapshot(context_dir)
    prompt_file = stage_dir / "prompt.md"
    prompt_file.write_text(combined_prompt, encoding="utf-8")
    prompt_preflight = record_prompt_preflight(stage_dir, agent, combined_prompt, scope_routing, env=os.environ.copy())
    cmd = render(
        agent, combined_prompt, prompt_file=prompt_file, context_dir=context_dir,
        stage_dir=stage_dir, cwd=cwd, output_file=output_file,
    )
    (stage_dir / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    wait = cfg.quota_wait if wait_for_quota is None else wait_for_quota
    delay = cfg.quota_retry_seconds
    attempts = 0
    while True:
        attempts += 1
        integrity_before = shared_context_integrity(context_dir)
        atomic_json(state_path, {
            "status": "running", "agent": name, "role": role, "runtime": agent.runtime,
            "model": agent.model, "effort": agent.effort, "attempts": attempts,
            "scope_routing": scope_routing,
            "prompt_preflight": prompt_preflight,
            "updated_at": now(), "shared_context": str(canonical_context),
            "worker_context": str(context_dir),
        })
        try:
            cp = run_worker_process(
                cmd,
                cwd=cwd,
                prompt=stdin_for(agent, combined_prompt),
                timeout=agent.timeout_seconds,
                env=sanitized_worker_environment(agent, os.environ.copy()),
            )
        except KeyboardInterrupt:
            atomic_json(state_path, {
                "status": "interrupted", "agent": name, "role": role,
                "runtime": agent.runtime, "attempts": attempts, "updated_at": now(),
            })
            raise
        raw_stdout = cp.stdout or ""
        raw_stderr = cp.stderr or ""
        raw_stdout_path.write_text(raw_stdout, encoding="utf-8")
        stderr_path.write_text(raw_stderr, encoding="utf-8")
        if is_quota_error(cp):
            retry_at = (dt.datetime.now(UTC) + dt.timedelta(seconds=delay)).isoformat()
            atomic_json(state_path, {
                "status": "quota_paused", "agent": name, "role": role,
                "runtime": agent.runtime, "attempts": attempts, "retry_at": retry_at,
            })
            if not wait:
                raise QuotaPaused(name, retry_at, stage_dir)
            time.sleep(delay)
            delay = min(delay * 2, cfg.quota_max_retry_seconds)
            continue
        try:
            normalized, metadata = normalize_worker_output(agent, raw_stdout, raw_stderr, cp.returncode, output_file)
        except RuntimeError as exc:
            atomic_json(state_path, {
                "status": "failed", "agent": name, "role": role,
                "runtime": agent.runtime, "attempts": attempts,
                "returncode": cp.returncode, "error": str(exc), "updated_at": now(),
            })
            raise CompetitionError(f"{name}/{role} ({agent.runtime}) failed: {exc}") from exc
        if shared_context_integrity(context_dir) != integrity_before:
            raise CompetitionError(f"{name}/{role} modified supervisor-owned shared context")
        stdout_path.write_text(normalized.rstrip() + "\n", encoding="utf-8")
        metadata["scope_routing"] = scope_routing
        metadata["subagent_cap"] = getattr(cfg, "subagent_cap", 5)
        metadata["prompt_preflight"] = prompt_preflight
        metadata["usage_reconciliation"] = reconcile_usage(prompt_preflight, metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {})
        record_stage_event(stage_dir, cwd, agent, normalized, metadata, prompt=combined_prompt, raw_stdout=raw_stdout, raw_stderr=raw_stderr, command=cmd)
        atomic_json(state_path, {
            "status": "completed", "agent": name, "role": role,
            "runtime": agent.runtime, "model": agent.model, "effort": agent.effort,
            "attempts": attempts, "updated_at": now(), "runtime_metadata": metadata,
            "scope_routing": scope_routing,
        })
        return normalized


def checkpoint_range(target: int) -> tuple[int, int]:
    if target < 1:
        raise CompetitionError("target must be a positive version number")
    return max(1, target - 9), target


def resolve_range(target: int | None, start: int | None, end: int | None) -> VersionRange:
    if target is not None:
        if start is not None or end is not None:
            raise CompetitionError("use either --target or --from-version/--to-version")
        start, end = checkpoint_range(target)
    if start is None or end is None:
        raise CompetitionError("provide a version range")
    result = VersionRange(start, end)
    result.validate()
    return result


def campaign_root(cfg: Config, vr: VersionRange) -> Path:
    return cfg.state_dir / cfg.repo.name / vr.key


def approval_path(root: Path, phase: str) -> Path:
    return root / "approvals" / f"{phase}.json"


def approved(root: Path, phase: str) -> bool:
    path = approval_path(root, phase)
    return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("approved"))


def require_approval(root: Path, phase: str, reason: str, details: dict[str, Any]) -> None:
    path = approval_path(root, phase)
    if approved(root, phase):
        return
    atomic_json(path, {
        "approved": False, "phase": phase, "reason": reason,
        "details": details, "requested_at": now(),
    })
    raise ApprovalRequired(f"approval required: {reason}; inspect {path}")


def dangerous_intent(text: str) -> list[str]:
    low = text.lower()
    return sorted({term for term in DANGEROUS_TERMS if term in low})


def prepare(cfg: Config, vr: VersionRange, force: bool = False, pr_options: PRWorkflowOptions | None = None) -> dict[str, Any]:
    validate(cfg, require_workers=False)
    root = campaign_root(cfg, vr)
    pr_options = pr_options or PRWorkflowOptions()
    base_info: dict[str, Any] = {"base_ref": cfg.base_ref}
    if pr_options.active:
        try:
            base_info = fetch_base(cfg.repo, remote=pr_options.remote, base_branch=pr_options.base_branch)
        except PRWorkflowError as exc:
            raise CompetitionError(str(exc)) from exc
    base = git(cfg.repo, "rev-parse", str(base_info.get("base_ref", cfg.base_ref)))
    manifest_path = root / "manifest.json"
    if root.exists() and force:
        for name in (*cfg.competitors, "converged"):
            wt = root / "worktrees" / name
            if wt.exists():
                run(["git", "worktree", "remove", "--force", str(wt)], cfg.repo)
        shutil.rmtree(root)
    elif manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 2,
        "project": "hermes-legion-commander",
        "repo": str(cfg.repo),
        "range_id": vr.key,
        "from_version": vr.start,
        "to_version": vr.end,
        "base_commit": base,
        "base_ref": str(base_info.get("base_ref", cfg.base_ref)),
        "pr_workflow": dataclasses.asdict(pr_options),
        "competitors": list(cfg.competitors),
        "roles": list(ALL_ROLES),
        "status": "prepared",
        "created_at": now(),
    }
    atomic_json(manifest_path, manifest)
    for name in cfg.competitors:
        wt = root / "worktrees" / name
        agent = cfg.agents[name]
        actor = actor_from_worker(name, agent.runtime, agent.provider)
        branch_slug = (pr_options.slug or f"{cfg.repo.name}-{vr.key}-{name}")
        branch = (
            pr_branch_name(actor=actor, mode="competitive", slug=branch_slug, stamp=f"{vr.key}-{name}")
            if pr_options.active
            else f"{cfg.branch_prefix}/{cfg.repo.name}/{vr.key}/{name}-candidate"
        )
        if run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cfg.repo).returncode == 0:
            raise CompetitionError(f"branch already exists: {branch}")
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(cfg.repo, "worktree", "add", "-b", branch, str(wt), base)
        atomic_json(root / "candidates" / name / "state.json", {
            "status": "ready", "branch": branch, "worktree": str(wt), "updated_at": now(),
        })
    return manifest


def roadmap_excerpt(cfg: Config, vr: VersionRange) -> str:
    _, scoped_context, _ = roadmap_context(cfg)
    try:
        excerpt = extract_version_range(scoped_context, vr.start, vr.end)
    except KeyError as exc:
        raise CompetitionError(str(exc)) from exc
    return excerpt + f"\n\nTarget range: v{vr.start} through v{vr.end}."


def role_prompt(cfg: Config, vr: VersionRange, role: str, name: str) -> str:
    profile = cfg.role_matrix[role][name]
    delegation = f"\n{subagent_delegation_contract(cfg.subagent_cap)}\n" if role in CANDIDATE_ROLES else ""
    return f"""You are the {name.upper()} checkpoint competitor performing role `{role}`.
You are building an independent candidate for project versions v{vr.start}-v{vr.end}.
Work only in the current isolated Git worktree. Read the canonical shared-context snapshot before acting.

ROLE OBJECTIVE
{profile.instructions}
{delegation}
COMPETITION RULES
- You are competing with an independent candidate produced by the other worker from the same baseline.
- Make the strongest evidence-based implementation you can; do not coordinate hidden state with the opponent.
- You may add, modify, rename, or remove repository files when needed for the bounded role and roadmap range.
- Keep all work inside the current worktree. Never modify the supervisor state or shared-context snapshot.
- Do not merge, push, deploy, alter credentials, operate hardware, tag, publish, or release.
- Do not fabricate research, citations, test results, experiment results, HIL evidence, or field evidence.
- Preserve prior correct candidate work and improve it rather than restarting blindly.
- Leave a concise final report of decisions, changed files, evidence, tests, and unresolved risks.

ROADMAP
{roadmap_excerpt(cfg, vr)}
"""


def _intent_to_add(worktree: Path) -> None:
    cp = run(["git", "add", "-N", "."], worktree)
    if cp.returncode not in {0, 1}:
        raise CompetitionError((cp.stderr or cp.stdout).strip())


def diff_stats(worktree: Path, base: str) -> dict[str, Any]:
    _intent_to_add(worktree)
    cp = run(["git", "diff", "--numstat", base], worktree)
    if cp.returncode:
        raise CompetitionError((cp.stderr or cp.stdout).strip())
    files = 0
    lines = 0
    rows = []
    for row in cp.stdout.splitlines():
        parts = row.split("\t")
        if len(parts) >= 3:
            add = int(parts[0]) if parts[0].isdigit() else 0
            delete = int(parts[1]) if parts[1].isdigit() else 0
            files += 1
            lines += add + delete
            rows.append({"path": parts[2], "added": add, "deleted": delete})
    return {"files": files, "lines": lines, "rows": rows}


def run_role_round(cfg: Config, vr: VersionRange, role: str, no_wait: bool) -> list[dict[str, Any]]:
    root = campaign_root(cfg, vr)
    stage = ROLE_STAGE[role]

    def execute(name: str) -> dict[str, Any]:
        wt = root / "worktrees" / name
        output = run_agent(
            cfg, name, role, role_prompt(cfg, vr, role, name), wt,
            root / "candidates" / name / stage, not no_wait,
        )
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        stats = diff_stats(wt, manifest["base_commit"])
        atomic_json(root / "candidates" / name / f"{stage}-diff.json", stats)
        return {
            "agent": name, "role": role, "stage": stage,
            "model": role_agent(cfg, name, role).model,
            "effort": role_agent(cfg, name, role).effort,
            "output_chars": len(output), "diff": stats,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(execute, name): name for name in cfg.competitors}
        return sorted((future.result() for future in concurrent.futures.as_completed(futures)), key=lambda row: row["agent"])


def checks(cfg: Config, vr: VersionRange, name: str) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    wt = root / "worktrees" / name
    records = []
    for cmd in cfg.checks:
        cp = run(list(cmd), wt, 7200)
        records.append({
            "command": list(cmd), "returncode": cp.returncode,
            "stdout": cp.stdout[-12000:], "stderr": cp.stderr[-12000:],
        })
    benchmarks = []
    for bench in cfg.benchmarks:
        cp = run(list(bench.command), wt, 7200)
        benchmarks.append({
            "id": bench.id, "direction": bench.direction, "weight": bench.weight,
            "command": list(bench.command), "returncode": cp.returncode,
            "stdout": cp.stdout[-12000:], "stderr": cp.stderr[-12000:],
        })
    result = {
        "agent": name,
        "all_checks_pass": all(row["returncode"] == 0 for row in records),
        "checks": records,
        "benchmarks": benchmarks,
    }
    atomic_json(root / "candidates" / name / "evaluation.json", result)
    return result


def _patch_for(worktree: Path, base: str) -> str:
    _intent_to_add(worktree)
    cp = run(["git", "diff", "--binary", base], worktree)
    if cp.returncode:
        raise CompetitionError((cp.stderr or cp.stdout).strip())
    return cp.stdout


def publish_candidate_evidence(cfg: Config, vr: VersionRange) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    artifacts = root / "shared-context" / "artifacts" / "candidates"
    artifacts.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    for name in cfg.competitors:
        wt = root / "worktrees" / name
        patch = _patch_for(wt, manifest["base_commit"])
        patch_path = artifacts / f"{name}.patch"
        patch_path.write_text(patch, encoding="utf-8")
        evaluation_path = root / "candidates" / name / "evaluation.json"
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8")) if evaluation_path.exists() else checks(cfg, vr, name)
        diff = diff_stats(wt, manifest["base_commit"])
        status_text = git(wt, "status", "--short")
        payload = {
            "agent": name, "worktree": str(wt), "patch": str(patch_path),
            "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "diff": diff, "git_status": status_text, "evaluation": evaluation,
        }
        atomic_json(artifacts / f"{name}.json", payload)
        summary[name] = payload
    atomic_json(root / "candidate-evidence.json", summary)
    return summary


def opponent_of(name: str, competitors: tuple[str, ...]) -> str:
    if name not in competitors:
        raise CompetitionError(f"unknown competitor: {name}")
    others = [c for c in competitors if c != name]
    if len(others) != 1:
        raise CompetitionError(f"opponent is only defined for a two-competitor pair, got {list(competitors)}")
    return others[0]


def _candidate_patch_digest(cfg: Config, vr: VersionRange, name: str) -> str:
    root = campaign_root(cfg, vr)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return hashlib.sha256(_patch_for(root / "worktrees" / name, manifest["base_commit"]).encode("utf-8")).hexdigest()


def _ensure_read_only_review(before: dict[str, str], after: dict[str, str], *, stage: str) -> None:
    changed = {name: {"before": before.get(name), "after": after.get(name)} for name in sorted(before) if before.get(name) != after.get(name)}
    if changed:
        raise CompetitionError(f"{stage} modified candidate worktrees during a read-only review: {changed}")


def cross_review_prompt(cfg: Config, vr: VersionRange, reviewer: str, target: str) -> str:
    return f"""You are the {reviewer.upper()} cross-validator reviewing the opponent candidate `{target}` for checkpoint range v{vr.start}-v{vr.end}.
Read shared-context/artifacts/candidates/{target}.json and its patch. Compare it against the roadmap, deterministic evaluation, and the base safety boundaries. You may also inspect the current repository worktree for orientation, but do not edit files.

{cfg.role_matrix['cross_reviewer'][reviewer].instructions}

{ADVERSARIAL_EVALUATOR_STANCE}

CROSS-VALIDATION RULES
- This is adversarial review of the opponent implementation, not judging your own candidate.
- Focus on concrete vulnerabilities, correctness mistakes, regressions, missing tests, unsafe defaults, overbroad roadmap changes, and false or unsupported evidence.
- Prefer file/path/function-specific findings with reproduction or reasoning evidence.
- Do not edit the worktree, do not merge, do not push, do not deploy, and do not fabricate results.
- A critical/high security, correctness, regression, or evidence-truthfulness finding is a must-fix finding before final judging.

Return exactly one JSON object with this schema:
{{
  "reviewer": "{reviewer}",
  "target": "{target}",
  "verdict": "pass|needs_fix|blocker",
  "findings": [
    {{
      "severity": "critical|high|medium|low|info",
      "category": "security|correctness|regression|test_quality|performance|maintainability|documentation|roadmap_scope|evidence_truthfulness",
      "code": "short_snake_case_code",
      "file": "relative/path/or.empty",
      "evidence": "specific evidence, command, diff hunk, or reasoning",
      "recommended_fix": "bounded fix recommendation",
      "confidence": 0.0
    }}
  ],
  "strengths": [],
  "must_fix_before_judging": []
}}
Do not add prose outside the JSON object.

ROADMAP
{roadmap_excerpt(cfg, vr)}
"""


def _normalize_cross_review(data: dict[str, Any], reviewer: str, target: str) -> dict[str, Any]:
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    normalized_findings: list[dict[str, Any]] = []
    must_fix: list[str] = []
    for index, raw in enumerate(findings, start=1):
        item = raw if isinstance(raw, dict) else {"evidence": str(raw)}
        severity = str(item.get("severity", "medium")).casefold()
        category = str(item.get("category", "correctness")).casefold()
        if severity not in CROSS_FINDING_SEVERITIES:
            severity = "medium"
        if category not in CROSS_FINDING_CATEGORIES:
            category = "correctness"
        code = re.sub(r"[^a-z0-9_]+", "_", str(item.get("code") or f"finding_{index}").casefold()).strip("_") or f"finding_{index}"
        confidence_raw = item.get("confidence", 0.5)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = 0.5
        finding = {
            "id": f"{reviewer}-reviews-{target}-{index:03d}",
            "reviewer": reviewer,
            "target": target,
            "severity": severity,
            "category": category,
            "code": code,
            "file": str(item.get("file", ""))[:500],
            "evidence": str(item.get("evidence", ""))[:4000],
            "recommended_fix": str(item.get("recommended_fix", ""))[:4000],
            "confidence": confidence,
            "blocking": severity in CROSS_BLOCKING_SEVERITIES and category in CROSS_BLOCKING_CATEGORIES,
        }
        if finding["blocking"]:
            must_fix.append(finding["id"])
        normalized_findings.append(finding)
    verdict = str(data.get("verdict") or "pass").casefold()
    if any(item["blocking"] for item in normalized_findings):
        verdict = "blocker"
    elif normalized_findings and verdict == "pass":
        verdict = "needs_fix"
    if verdict not in {"pass", "needs_fix", "blocker"}:
        verdict = "needs_fix" if normalized_findings else "pass"
    payload = {
        "schema_version": 1,
        "reviewer": reviewer,
        "target": target,
        "verdict": verdict,
        "findings": normalized_findings,
        "strengths": data.get("strengths", []) if isinstance(data.get("strengths", []), list) else [],
        "must_fix_before_judging": sorted(set(must_fix + [str(x) for x in data.get("must_fix_before_judging", []) if isinstance(x, str)])),
        "generated_at": now(),
    }
    return payload


def _write_cross_review_artifacts(root: Path, reviewer: str, target: str, payload: dict[str, Any]) -> None:
    direct = root / "cross-validation" / f"{reviewer}-reviews-{target}.json"
    shared = root / "shared-context" / "artifacts" / "cross-validation" / f"{reviewer}-reviews-{target}.json"
    atomic_json(direct, payload)
    atomic_json(shared, payload)


def _cross_review_artifacts(root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for path in sorted((root / "cross-validation").glob("*-reviews-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = f"{row.get('reviewer', path.stem)}->{row.get('target', '')}"
        payload[key] = row
    return payload


def cross_validation_summary(root: Path, competitors: tuple[str, ...]) -> dict[str, Any]:
    reviews = _cross_review_artifacts(root)
    by_target: dict[str, dict[str, Any]] = {name: {"findings": 0, "blockers": [], "verdicts": []} for name in competitors}
    for review in reviews.values():
        target = str(review.get("target", ""))
        if target not in by_target:
            continue
        by_target[target]["verdicts"].append(str(review.get("verdict", "")))
        for finding in review.get("findings", []):
            if not isinstance(finding, dict):
                continue
            by_target[target]["findings"] += 1
            if finding.get("blocking"):
                by_target[target]["blockers"].append(finding)
    return {
        "review_count": len(reviews),
        "by_target": by_target,
        "total_blockers": sum(len(row["blockers"]) for row in by_target.values()),
    }


def cross_validate_candidates(cfg: Config, vr: VersionRange, no_wait: bool) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    if not (root / "candidate-evidence.json").exists():
        publish_candidate_evidence(cfg, vr)
    before = {name: _candidate_patch_digest(cfg, vr, name) for name in cfg.competitors}

    def execute(reviewer: str) -> tuple[str, dict[str, Any]]:
        target = opponent_of(reviewer, cfg.competitors)
        text = run_agent(
            cfg, reviewer, "cross_reviewer", cross_review_prompt(cfg, vr, reviewer, target),
            root / "worktrees" / reviewer,
            root / "cross-validation" / reviewer,
            not no_wait,
        )
        data = _normalize_cross_review(parse_json_object(text), reviewer, target)
        _write_cross_review_artifacts(root, reviewer, target, data)
        return f"{reviewer}->{target}", data

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, name) for name in cfg.competitors]
        result = dict(future.result() for future in concurrent.futures.as_completed(futures))
    after = {name: _candidate_patch_digest(cfg, vr, name) for name in cfg.competitors}
    _ensure_read_only_review(before, after, stage="cross-validation")
    atomic_json(root / "cross-validation-summary.json", cross_validation_summary(root, cfg.competitors))
    return result


def cross_polish_prompt(cfg: Config, vr: VersionRange, owner: str, reviewer: str) -> str:
    return f"""You are the {owner.upper()} candidate owner performing cross-validation polish for checkpoint range v{vr.start}-v{vr.end}.
The opponent `{reviewer}` reviewed your candidate. Read shared-context/artifacts/cross-validation/{reviewer}-reviews-{owner}.json and your current candidate evidence. Fix true-positive findings in your own isolated worktree.

{cfg.role_matrix['cross_polisher'][owner].instructions}

POLISH RULES
- Work only in your current candidate worktree.
- Fix verified security, correctness, regression, and evidence-truthfulness findings first.
- Add or update deterministic tests for fixed defects whenever practical.
- If a finding is false positive, leave code unchanged and explain the rebuttal with exact evidence in your final report.
- Do not weaken safety boundaries, broaden the roadmap range, merge, push, deploy, tag, publish, alter credentials, or fabricate evidence.
- Finish with a concise report listing fixed finding IDs, rejected finding IDs, changed files, commands run, and residual risks.

ROADMAP
{roadmap_excerpt(cfg, vr)}
"""


def polish_from_cross_validation(cfg: Config, vr: VersionRange, no_wait: bool) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    if not (root / "cross-validation-summary.json").exists():
        cross_validate_candidates(cfg, vr, no_wait)
    outputs: dict[str, Any] = {}
    for owner in cfg.competitors:
        reviewer = opponent_of(owner, cfg.competitors)
        review_path = root / "cross-validation" / f"{reviewer}-reviews-{owner}.json"
        if not review_path.exists():
            raise CompetitionError(f"missing cross-validation findings for {owner}: {review_path}")
        before = diff_stats(root / "worktrees" / owner, json.loads((root / "manifest.json").read_text(encoding="utf-8"))["base_commit"])
        output = run_agent(
            cfg, owner, "cross_polisher", cross_polish_prompt(cfg, vr, owner, reviewer),
            root / "worktrees" / owner,
            root / "candidates" / owner / ROLE_STAGE["cross_polisher"],
            not no_wait,
        )
        evaluation = checks(cfg, vr, owner)
        after = diff_stats(root / "worktrees" / owner, json.loads((root / "manifest.json").read_text(encoding="utf-8"))["base_commit"])
        outputs[owner] = {
            "reviewer": reviewer,
            "review_path": str(review_path),
            "output_chars": len(output),
            "model": role_agent(cfg, owner, "cross_polisher").model,
            "effort": role_agent(cfg, owner, "cross_polisher").effort,
            "before_diff": before,
            "after_diff": after,
            "evaluation": evaluation,
        }
    publish_candidate_evidence(cfg, vr)
    summary = {"polishers": outputs, "cross_validation": cross_validation_summary(root, cfg.competitors), "generated_at": now()}
    atomic_json(root / "cross-polish-result.json", summary)
    return summary


def judge_prompt(cfg: Config, vr: VersionRange, judge: str) -> str:
    winner_options = "|".join([*cfg.competitors, "tie", "none"])
    candidate_lines = ",\n    ".join(
        f'"{c}": {{"security": 0, "correctness": 0, "maintainability": 0, "performance": 0, "blocker_codes": [], "strengths": [], "weaknesses": []}}'
        for c in cfg.competitors
    )
    return f"""You are the {judge.upper()} comparative judge for a checkpoint competition covering v{vr.start}-v{vr.end}.
Read both candidate evidence packages under shared-context/artifacts/candidates, including post-polish patches and deterministic evaluations. Also read shared-context/artifacts/cross-validation for opponent findings and candidate-owner polish evidence when present.
Do not edit the repository. Judge both candidates against the roadmap, security boundaries, cross-validation findings, correctness, maintainability, performance evidence, test quality, documentation truthfulness, and honest scope.

{ADVERSARIAL_EVALUATOR_STANCE}

Return exactly one JSON object with this schema:
{{
  "recommended_winner": "{winner_options}",
  "candidates": {{
    {candidate_lines}
  }},
  "shared_findings": [],
  "cross_validation_considered": [],
  "convergence_plan": []
}}
Scores are 0-100. blocker_codes may only use: {sorted(SECURITY_BLOCKERS)}. Treat unresolved critical/high cross-validation findings as blockers unless candidate polish evidence convincingly fixes or rebuts them.
Do not add prose outside the JSON object.
"""


def parse_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise CompetitionError("worker did not return a JSON object")
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise CompetitionError(f"worker returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CompetitionError("worker JSON result must be an object")
    return value


def review(cfg: Config, vr: VersionRange, no_wait: bool) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    if not (root / "candidate-evidence.json").exists():
        publish_candidate_evidence(cfg, vr)

    def execute(name: str) -> tuple[str, dict[str, Any]]:
        text = run_agent(
            cfg, name, "judge", judge_prompt(cfg, vr, name),
            root / "worktrees" / name,
            root / "judges" / name,
            not no_wait,
        )
        data = parse_json_object(text)
        atomic_json(root / "judges" / f"{name}.json", data)
        return name, data

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(execute, name) for name in cfg.competitors]
        return dict(future.result() for future in concurrent.futures.as_completed(futures))


def _candidate_score_from_judges(cfg: Config, name: str, judgements: dict[str, Any]) -> dict[str, Any]:
    score_rows: list[dict[str, float]] = []
    blockers: set[str] = set()
    for judgement in judgements.values():
        candidate = judgement.get("candidates", {}).get(name, {})
        if not isinstance(candidate, dict):
            continue
        score_rows.append({
            key: float(candidate.get(key, 0))
            for key in ("security", "correctness", "maintainability", "performance")
        })
        blockers.update(set(map(str, candidate.get("blocker_codes", []))) & SECURITY_BLOCKERS)
    if not score_rows:
        scores = {key: 0.0 for key in ("security", "correctness", "maintainability", "performance")}
    else:
        scores = {
            key: sum(row[key] for row in score_rows) / len(score_rows)
            for key in score_rows[0]
        }
    total = sum(scores[key] * cfg.weights.get(key, 0.0) for key in scores)
    return {"scores": scores, "weighted_total": round(total, 3), "blockers": sorted(blockers)}


def evaluate(cfg: Config, vr: VersionRange) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    evaluations = {}
    for name in cfg.competitors:
        path = root / "candidates" / name / "evaluation.json"
        evaluations[name] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else checks(cfg, vr, name)
    judgements: dict[str, Any] = {}
    for judge in cfg.competitors:
        path = root / "judges" / f"{judge}.json"
        if not path.exists():
            raise CompetitionError("run review before evaluate")
        judgements[judge] = json.loads(path.read_text(encoding="utf-8"))
    candidates = {}
    for name in cfg.competitors:
        scored = _candidate_score_from_judges(cfg, name, judgements)
        eligible = evaluations[name]["all_checks_pass"] and not scored["blockers"]
        candidates[name] = {
            **scored,
            "eligible": eligible,
            "evaluation": evaluations[name],
        }
    eligible_rows = [(row["weighted_total"], name) for name, row in candidates.items() if row["eligible"]]
    winner = max(eligible_rows)[1] if eligible_rows else None
    report = {
        "range_id": vr.key,
        "provisional_winner": winner,
        "candidates": candidates,
        "judgements": judgements,
        "cross_validation": cross_validation_summary(root, cfg.competitors) if (root / "cross-validation").exists() else None,
        "cross_polish": json.loads((root / "cross-polish-result.json").read_text(encoding="utf-8")) if (root / "cross-polish-result.json").exists() else None,
        "human_gate_required": True,
        "automatic_merge": False,
        "generated_at": now(),
    }
    atomic_json(root / "comparison-report.json", report)
    return report


def _create_converged_worktree(cfg: Config, vr: VersionRange, winner: str) -> Path:
    root = campaign_root(cfg, vr)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    wt = root / "worktrees" / "converged"
    if wt.exists():
        return wt
    pr_raw = manifest.get("pr_workflow") if isinstance(manifest.get("pr_workflow"), dict) else {}
    pr_active = bool(pr_raw.get("enabled") or pr_raw.get("push") or pr_raw.get("open_pr"))
    branch = (
        pr_branch_name(actor="commander", mode="competitive", slug=str(pr_raw.get("slug") or f"{cfg.repo.name}-{vr.key}"), stamp=f"{vr.key}-converged")
        if pr_active
        else f"{cfg.branch_prefix}/{cfg.repo.name}/{vr.key}/converged"
    )
    if run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cfg.repo).returncode == 0:
        raise CompetitionError(f"branch already exists: {branch}")
    git(cfg.repo, "worktree", "add", "-b", branch, str(wt), manifest["base_commit"])
    evidence = json.loads((root / "candidate-evidence.json").read_text(encoding="utf-8"))
    patch_path = Path(evidence[winner]["patch"])
    if patch_path.stat().st_size:
        cp = run(["git", "apply", "--3way", "--whitespace=nowarn", str(patch_path)], wt)
        if cp.returncode:
            raise CompetitionError(f"failed to apply provisional winner patch: {(cp.stderr or cp.stdout).strip()}")
    atomic_json(root / "convergence" / "state.json", {
        "status": "seeded", "provisional_winner": winner,
        "branch": branch, "worktree": str(wt), "updated_at": now(),
    })
    return wt


def convergence_prompt(cfg: Config, vr: VersionRange, name: str, winner: str) -> str:
    return f"""You are the {name.upper()} convergence engineer for checkpoint range v{vr.start}-v{vr.end}.
The current worktree is seeded from provisional winner `{winner}`. Read both candidate patches, cross-validation artifacts, candidate polish evidence, evaluations, and both judge reports from the shared context artifacts.

{cfg.role_matrix['converger'][name].instructions}

CONVERGENCE RULES
- Work only in the current convergence worktree.
- Keep the strongest verified parts of the provisional winner.
- Incorporate superior, compatible ideas from the other candidate when evidence supports them.
- Resolve blocker findings from cross-validation and judges, and preserve deterministic tests and honest documentation.
- Add or modify files as needed, but do not expand beyond v{vr.start}-v{vr.end}.
- Do not merge, push, deploy, alter credentials, operate hardware, tag, publish, or release.
- Finish with a concise report of adopted ideas, rejected ideas, changed files, checks, and remaining risk.

ROADMAP
{roadmap_excerpt(cfg, vr)}
"""


def publish_converged_evidence(
    cfg: Config,
    vr: VersionRange,
    worktree: Path,
    records: list[dict[str, Any]],
    diff: dict[str, Any],
) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    artifacts = root / "shared-context" / "artifacts" / "converged"
    artifacts.mkdir(parents=True, exist_ok=True)
    patch = _patch_for(worktree, manifest["base_commit"])
    patch_path = artifacts / "converged.patch"
    patch_path.write_text(patch, encoding="utf-8")
    payload = {
        "range_id": vr.key,
        "worktree": str(worktree),
        "patch": str(patch_path),
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "diff": diff,
        "checks": records,
        "all_checks_pass": all(row["returncode"] == 0 for row in records),
        "git_status": git(worktree, "status", "--short"),
        "generated_at": now(),
    }
    atomic_json(artifacts / "converged.json", payload)
    atomic_json(root / "converged-evidence.json", payload)
    return payload


def final_verification_prompt(cfg: Config, vr: VersionRange, name: str) -> str:
    return f"""You are the {name.upper()} final verifier for the converged implementation covering v{vr.start}-v{vr.end}.
Read shared-context/artifacts/converged/converged.json, the converged patch, cross-validation artifacts, candidate polish evidence, judge reports, and deterministic check results. This is a read-only adversarial verification pass.

{cfg.role_matrix['final_verifier'][name].instructions}

{ADVERSARIAL_EVALUATOR_STANCE}

FINAL VERIFICATION RULES
- Do not edit files or run destructive commands.
- Verify whether cross-validation and judge findings were actually resolved.
- Identify remaining vulnerabilities, correctness bugs, regressions, missing tests, overbroad roadmap work, or unsupported claims.
- Prefer exact file/path/function evidence and concrete reproduction commands when possible.
- Do not merge, push, deploy, tag, publish, alter credentials, operate hardware, or fabricate evidence.

Return exactly one JSON object with this schema:
{{
  "verifier": "{name}",
  "verdict": "pass|needs_fix|blocker",
  "blockers": [],
  "findings": [
    {{
      "severity": "critical|high|medium|low|info",
      "category": "security|correctness|regression|test_quality|performance|maintainability|documentation|roadmap_scope|evidence_truthfulness",
      "code": "short_snake_case_code",
      "file": "relative/path/or.empty",
      "evidence": "specific evidence, command, diff hunk, or reasoning",
      "recommended_fix": "bounded fix recommendation",
      "confidence": 0.0
    }}
  ],
  "release_readiness_notes": [],
  "recommended_next_fixes": []
}}
Do not add prose outside the JSON object.

ROADMAP
{roadmap_excerpt(cfg, vr)}
"""


def _normalize_final_verification(data: dict[str, Any], verifier: str) -> dict[str, Any]:
    normalized = _normalize_cross_review({**data, "findings": data.get("findings", [])}, verifier, "converged")
    explicit_blockers = [str(x) for x in data.get("blockers", []) if isinstance(x, str)]
    verdict = str(data.get("verdict") or normalized["verdict"]).casefold()
    if normalized["must_fix_before_judging"] or explicit_blockers:
        verdict = "blocker"
    elif verdict not in {"pass", "needs_fix", "blocker"}:
        verdict = normalized["verdict"]
    return {
        "schema_version": 1,
        "verifier": verifier,
        "verdict": verdict,
        "blockers": sorted(set(explicit_blockers + normalized["must_fix_before_judging"])),
        "findings": normalized["findings"],
        "release_readiness_notes": data.get("release_readiness_notes", []) if isinstance(data.get("release_readiness_notes", []), list) else [],
        "recommended_next_fixes": data.get("recommended_next_fixes", []) if isinstance(data.get("recommended_next_fixes", []), list) else [],
        "generated_at": now(),
    }


def final_verify_converged(cfg: Config, vr: VersionRange, worktree: Path, no_wait: bool) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    before = hashlib.sha256(_patch_for(worktree, manifest["base_commit"]).encode("utf-8")).hexdigest()
    results: dict[str, Any] = {}
    for name in cfg.competitors:
        text = run_agent(
            cfg, name, "final_verifier", final_verification_prompt(cfg, vr, name),
            worktree,
            root / "final-verification" / name,
            not no_wait,
        )
        after = hashlib.sha256(_patch_for(worktree, manifest["base_commit"]).encode("utf-8")).hexdigest()
        if after != before:
            raise CompetitionError(f"final verifier {name} modified the converged worktree during read-only verification")
        data = _normalize_final_verification(parse_json_object(text), name)
        atomic_json(root / "final-verification" / f"{name}.json", data)
        atomic_json(root / "shared-context" / "artifacts" / "final-verification" / f"{name}.json", data)
        results[name] = data
    summary = {
        "verifiers": results,
        "verdict": "blocker" if any(row.get("verdict") == "blocker" for row in results.values()) else ("needs_fix" if any(row.get("verdict") == "needs_fix" for row in results.values()) else "pass"),
        "blockers": sorted({blocker for row in results.values() for blocker in row.get("blockers", [])}),
        "generated_at": now(),
    }
    atomic_json(root / "final-verification-summary.json", summary)
    return summary


def converge(cfg: Config, vr: VersionRange, no_wait: bool) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    report_path = root / "comparison-report.json"
    if not report_path.exists():
        raise CompetitionError("run evaluate before converge")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    winner = report.get("provisional_winner")
    if winner not in cfg.competitors:
        raise CompetitionError("no eligible provisional winner; convergence cannot start")
    wt = _create_converged_worktree(cfg, vr, winner)
    outputs = {}
    for name in cfg.competitors:
        output = run_agent(
            cfg, name, "converger", convergence_prompt(cfg, vr, name, winner),
            wt, root / "convergence" / name, not no_wait,
        )
        outputs[name] = {"output_chars": len(output), "model": role_agent(cfg, name, "converger").model,
                         "effort": role_agent(cfg, name, "converger").effort}
    records = []
    for cmd in cfg.checks:
        cp = run(list(cmd), wt, 7200)
        records.append({
            "command": list(cmd), "returncode": cp.returncode,
            "stdout": cp.stdout[-12000:], "stderr": cp.stderr[-12000:],
        })
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    diff = diff_stats(wt, manifest["base_commit"])
    converged_evidence = publish_converged_evidence(cfg, vr, wt, records, diff)
    final_verification = final_verify_converged(cfg, vr, wt, no_wait)
    result = {
        "range_id": vr.key,
        "provisional_winner": winner,
        "converged_worktree": str(wt),
        "convergers": outputs,
        "all_checks_pass": all(row["returncode"] == 0 for row in records),
        "checks": records,
        "diff": diff,
        "converged_evidence": converged_evidence,
        "final_verification": final_verification,
        "ready_for_human_review": all(row["returncode"] == 0 for row in records) and final_verification.get("verdict") == "pass",
        "human_review_required": True,
        "automatic_merge": False,
        "generated_at": now(),
    }
    atomic_json(root / "converged-result.json", result)
    report["convergence"] = result
    atomic_json(report_path, report)
    return result


def run_campaign(cfg: Config, vr: VersionRange, no_wait: bool, pr_options: PRWorkflowOptions | None = None) -> dict[str, Any]:
    validate(cfg)
    root = campaign_root(cfg, vr)
    pr_options = pr_options or PRWorkflowOptions()
    if not (root / "manifest.json").exists():
        prepare(cfg, vr, pr_options=pr_options)
    terms = dangerous_intent(roadmap_excerpt(cfg, vr))
    if terms:
        require_approval(
            root, "dangerous-intent", "roadmap includes safety/security-sensitive work",
            {"matched_terms": terms, "range": vr.key},
        )
    rounds = []
    for role in CANDIDATE_ROLES:
        rounds.append({"role": role, "results": run_role_round(cfg, vr, role, no_wait)})
    initial_evaluations = {name: checks(cfg, vr, name) for name in cfg.competitors}
    initial_evidence = publish_candidate_evidence(cfg, vr)
    cross_reviews = cross_validate_candidates(cfg, vr, no_wait)
    cross_polish = polish_from_cross_validation(cfg, vr, no_wait)
    evaluations = {name: checks(cfg, vr, name) for name in cfg.competitors}
    evidence = publish_candidate_evidence(cfg, vr)
    oversized = [
        {"agent": name, "diff": row["diff"]}
        for name, row in evidence.items()
        if row["diff"]["files"] >= cfg.massive_files or row["diff"]["lines"] >= cfg.massive_lines
    ]
    if oversized:
        require_approval(
            root, "massive-diff", "candidate diff exceeds configured threshold",
            {"thresholds": {"files": cfg.massive_files, "lines": cfg.massive_lines}, "candidates": oversized},
        )
    judgements = review(cfg, vr, no_wait)
    comparison = evaluate(cfg, vr)
    converged = converge(cfg, vr, no_wait)
    result = {
        "range_id": vr.key,
        "role_rounds": rounds,
        "initial_evaluations": initial_evaluations,
        "initial_evidence": initial_evidence,
        "cross_reviews": cross_reviews,
        "cross_polish": cross_polish,
        "evaluations": evaluations,
        "judgements": judgements,
        "comparison_report": str(root / "comparison-report.json"),
        "provisional_winner": comparison.get("provisional_winner"),
        "converged_result": converged,
        "human_review_required": True,
        "automatic_merge": False,
    }
    final_state = json.loads((root / "convergence" / "state.json").read_text(encoding="utf-8")) if (root / "convergence" / "state.json").exists() else {}
    converged_wt = root / "worktrees" / "converged"
    converged_branch = str(final_state.get("branch") or "")
    if pr_options.active and converged_wt.exists() and converged_branch:
        result["pull_request"] = _finalize_competition_pr(
            cfg=cfg, vr=vr, options=pr_options, worktree=converged_wt,
            branch=converged_branch, result=result, no_wait=no_wait,
        )
    atomic_json(root / "result.json", result)
    return result


def status(cfg: Config, vr: VersionRange) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    return {
        "repo": str(cfg.repo),
        "range_id": vr.key,
        "exists": root.exists(),
        "manifest": json.loads((root / "manifest.json").read_text()) if (root / "manifest.json").exists() else None,
        "approvals": {
            path.stem: json.loads(path.read_text())
            for path in (root / "approvals").glob("*.json")
        } if (root / "approvals").exists() else {},
        "cross_validation": json.loads((root / "cross-validation-summary.json").read_text()) if (root / "cross-validation-summary.json").exists() else None,
        "cross_polish": json.loads((root / "cross-polish-result.json").read_text()) if (root / "cross-polish-result.json").exists() else None,
        "report": json.loads((root / "comparison-report.json").read_text()) if (root / "comparison-report.json").exists() else None,
        "converged": json.loads((root / "converged-result.json").read_text()) if (root / "converged-result.json").exists() else None,
        "final_verification": json.loads((root / "final-verification-summary.json").read_text()) if (root / "final-verification-summary.json").exists() else None,
    }


def approve(cfg: Config, vr: VersionRange, phase: str, note: str) -> dict[str, Any]:
    root = campaign_root(cfg, vr)
    path = approval_path(root, phase)
    if not path.exists():
        raise CompetitionError(f"no pending approval for phase {phase}")
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update({"approved": True, "approved_at": now(), "note": note})
    atomic_json(path, data)
    return data


def worker_summary(cfg: Config) -> dict[str, Any]:
    return {
        name: {
            "runtime": agent.runtime,
            "executable": agent.command[0],
            "provider": agent.provider,
            "base_model": agent.model or "CLI configured default",
            "base_effort": agent.effort,
            "prompt_transport": agent.prompt_transport,
            "output_format": agent.output_format,
            "capabilities": list(agent.capabilities),
            "roles": {
                role: {
                    "model": cfg.role_matrix[role][name].model or "CLI configured default",
                    "effort": cfg.role_matrix[role][name].effort,
                    "instructions": cfg.role_matrix[role][name].instructions,
                }
                for role in ALL_ROLES
            },
        }
        for name, agent in cfg.agents.items()
    }


def evaluate_goal(
    cfg: Config,
    condition: str,
    judge: str | None = None,
    dry_run: bool = False,
    no_wait: bool = False,
) -> dict[str, Any]:
    """Evaluate a ``/goal`` stop condition with a fresh-model judge over the checks.

    The judge is one of the two competitors (default: the first), evaluated with
    its ``judge`` role profile. The configured deterministic checks provide
    evidence and form a hard floor: a failed check vetoes a "met" verdict.
    """
    condition = condition.strip()
    if not condition:
        raise CompetitionError("--condition must be a non-empty stop condition")
    judge = judge or cfg.competitors[0]
    if judge not in cfg.competitors:
        raise CompetitionError(f"--judge must be one of the configured competitors {list(cfg.competitors)}")
    if not (cfg.repo / ".git").exists():
        raise CompetitionError(f"not a Git checkout: {cfg.repo}")
    results = run_deterministic_checks(cfg.repo, cfg.checks)
    base_prompt = stop_condition_prompt(condition, results)
    if dry_run:
        return {
            "condition": condition,
            "judge": judge,
            "judge_model": cfg.role_matrix["judge"][judge].model or cfg.agents[judge].model or "CLI configured default",
            "dry_run": True,
            "deterministic_all_passed": deterministic_all_passed(results),
            "checks": [r.to_dict() for r in results],
            "prompt_chars": len(base_prompt),
        }
    stamp = dt.datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stage_dir = cfg.state_dir / "goal" / f"goal-{stamp}"
    output = run_agent(cfg, judge, "judge", base_prompt, cfg.repo, stage_dir, not no_wait)
    verdict = parse_stop_verdict(output, results)
    verdict["condition"] = condition
    verdict["judge"] = judge
    atomic_json(stage_dir / "goal-verdict.json", verdict)
    return verdict


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument(
        "--roadmap",
        type=Path,
        help="explicit roadmap markdown file to drive this run; overrides the "
        "config plan and is honored even if it lives outside docs/ or is not "
        "named *roadmap*.md",
    )
    ap.add_argument("--repo", type=Path, help="external target Git repository; overrides config")
    ap.add_argument(
        "command",
        choices=["preflight", "prepare", "run", "resume", "cross-validate", "cross-polish", "review", "evaluate", "converge", "final-verify", "status", "approve", "workers", "goal"],
    )
    ap.add_argument("--target", type=int)
    ap.add_argument("--from-version", type=int)
    ap.add_argument("--to-version", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--preview-chars", type=int, default=800)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="checkpoint and exit on provider quota exhaustion")
    ap.add_argument("--phase", choices=["dangerous-intent", "massive-diff"])
    ap.add_argument("--note", default="")
    ap.add_argument("--condition", help="natural-language stop condition for the 'goal' command")
    ap.add_argument("--judge", help="competitor that judges a 'goal' condition (default: first competitor)")
    ap.add_argument("--dry-run", action="store_true", help="for 'goal': run checks and build the prompt without invoking the judge model")
    _add_pr_args(ap)
    ns = ap.parse_args(argv)
    try:
        cfg = load_config(ns.config.resolve(), ns.repo)
        if getattr(ns, "roadmap", None) is not None:
            cfg = dataclasses.replace(cfg, plan=resolve(cfg.repo, str(ns.roadmap)))
        if ns.command == "workers":
            print(json.dumps(worker_summary(cfg), indent=2, sort_keys=True))
            return 0
        if ns.command == "preflight":
            print(json.dumps(roadmap_preflight(cfg, ns.preview_chars, ns.verbose), indent=2, sort_keys=True))
            return 0
        if ns.command == "goal":
            if not ns.condition:
                raise CompetitionError("--condition is required for 'goal'")
            print(json.dumps(
                evaluate_goal(cfg, ns.condition, ns.judge, ns.dry_run, ns.no_wait),
                indent=2, sort_keys=True,
            ))
            return 0
        vr = resolve_range(ns.target, ns.from_version, ns.to_version)
        pr_options = _make_pr_options_from_args(ns, mode="competitive")
        if ns.command == "prepare":
            result = prepare(cfg, vr, ns.force, pr_options=pr_options)
        elif ns.command in {"run", "resume"}:
            result = run_campaign(cfg, vr, ns.no_wait, pr_options=pr_options)
        elif ns.command == "cross-validate":
            result = cross_validate_candidates(cfg, vr, ns.no_wait)
        elif ns.command == "cross-polish":
            result = polish_from_cross_validation(cfg, vr, ns.no_wait)
        elif ns.command == "review":
            result = review(cfg, vr, ns.no_wait)
        elif ns.command == "evaluate":
            result = evaluate(cfg, vr)
        elif ns.command == "converge":
            result = converge(cfg, vr, ns.no_wait)
        elif ns.command == "final-verify":
            root = campaign_root(cfg, vr)
            converged_path = root / "worktrees" / "converged"
            if not converged_path.exists():
                raise CompetitionError("run converge before final-verify")
            result = final_verify_converged(cfg, vr, converged_path, ns.no_wait)
        elif ns.command == "status":
            result = status(cfg, vr)
        elif ns.command == "approve":
            if not ns.phase:
                raise CompetitionError("--phase is required")
            result = approve(cfg, vr, ns.phase, ns.note)
        else:
            raise CompetitionError("unsupported command")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (CompetitionError, OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"competition error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
