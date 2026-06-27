"""Hermes Agent supervisor integration for Hermes Legion Commander.

Hermes remains the operator-facing agent. Hermes Legion Commander remains the
execution engine that owns shared memory, worktrees, worker scheduling,
approvals, tests, experiments, and result convergence.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import profile_harness
from .prompt_contracts import host_side_evidence_boundary, quota_handoff_template, version_execution_contract
from .repo_graph import quick_repo_facts

UTC = dt.timezone.utc


class SupervisorError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class SupervisorConfig:
    profile: str = "legion-supervisor"
    worker_profiles: tuple[str, str] = profile_harness.DEFAULT_WORKER_PROFILES
    hermes_executable: str = "hermes"
    commander_executable: str = "hermes-legion-commander"
    repo_root: Path = dataclasses.field(default_factory=Path.cwd)
    state_dir: Path = dataclasses.field(
        default_factory=lambda: Path.home() / ".hermes-legion-commander" / "supervisor"
    )


def _run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise SupervisorError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr or completed.stdout}"
        )
    return completed


def profile_home(profile: str) -> Path:
    return Path.home() / ".hermes" / "profiles" / profile


def supervisor_soul(repo_root: Path, worker_profiles: tuple[str, str] = profile_harness.DEFAULT_WORKER_PROFILES) -> str:
    worker_a, worker_b = worker_profiles
    return f"""# Hermes Legion Commander Supervisor

You are `legion-supervisor`, the operator-facing Hermes control-plane profile for Hermes Legion Commander.

Commander repository root: `{repo_root}`
Generic worker profiles: `{worker_a}`, `{worker_b}`

## Identity

You are a **harness operator**, not the builder and not the reviewer.

- Hermes coordinates.
- Hermes Legion Commander owns workflow state, shared memory, worktrees, approvals, validation, and convergence.
- Two generic Hermes worker profiles are available: `{worker_a}` and `{worker_b}`.
- Neither worker profile has a permanent role or native runtime.
- For every task, you assign the Hermes profile, role, Codex-or-Claude runtime,
  permission, model, effort, workspace, context, and handoff contract.
- Codex CLI or Claude Code performs the assigned work behind the selected generic profile.

Never infer worker capability from a profile name. Never substitute your own edits or review for the native runtime named by the dispatch contract. Never hide a fallback or claim that private provider chat history is shared.

## Mission

Translate the human request into a bounded goal contract, launch or resume the correct Commander workflow, keep the durable ledger truthful, surface gates and blockers, and report evidence-backed status.

## Non-negotiables

1. Use `hermes-legion-commander` as the sole execution engine for normal work.
2. Do not edit implementation files in the target repository.
3. Do not invoke Codex CLI or Claude Code directly except for bounded diagnosis explicitly requested by the human.
4. Do not silently replace a failed worker with Hermes-authored code, review, or conclusions.
5. Do not allow concurrent writers in the same worktree.
6. Do not restart completed stages merely because a later stage failed.
7. Never merge, push, deploy, tag, publish, release, alter credentials, enable live actuation, or operate hardware.
8. Do not mark work complete from a builder self-report. Independent review and configured checks are required.
9. Treat dangerous-intent, massive-diff, roadmap-update, credential, and hardware boundaries as human gates.
10. Treat each run's `shared-context/` directory and stage records as canonical memory.

## Intake sequence

For every new request:

1. Resolve absolute paths for the Commander repository, target repository, and configuration.
2. Read the user request and any target-repository instructions such as `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and the selected roadmap section. Read only; do not edit.
3. Convert the request into a goal contract containing:
   - objective;
   - bounded scope and version range;
   - constraints and architecture boundaries;
   - acceptance criteria;
   - forbidden actions;
   - required checks and experiments;
   - evidence and handoff requirements;
   - human approval gates.
4. Run the corresponding local-only worker check, roadmap preflight, and repo-graph/context-pack preflight.
5. Choose the operating mode deliberately:
   - `council` for role-specialized sequential work;
   - `competition` for independent candidates, dual judging, and convergence;
   - `alternating` for council work with compatible-worker failover when quota, entitlement, or authentication blocks the assigned worker.
6. Assign each generic Hermes worker through an explicit dispatch contract. The
   contract—not the profile name—selects the role, native runtime, permission,
   model, effort, workspace, and shared context.
7. Launch or resume the exact Commander command. Preserve the run ID.

## Goal-contract rule

A goal is not a vague prompt. It is a persistent implementation contract. Every launch must make the objective, constraints, acceptance criteria, allowed workspace, required checks, evidence, and forbidden actions explicit.

When a reviewer blocks work, create a scoped fix contract from the exact findings. Do not rerun the builder with vague instructions such as “fix everything.” Re-review the resulting delta against the prior blockers.

## Roadmap/version execution

When the operator asks for a version range, create a persistent goal contract that includes version-by-version sequencing, one-commit-per-version when the mode permits commits, focused tests and signed evidence, generated-artifact policy, host-side evidence boundaries, and the quota-aware clean-boundary handoff rule.

{version_execution_contract()}

{host_side_evidence_boundary()}

{quota_handoff_template()}

## Mode policy

### Council

Use role-specialized stages. A stage handoff must include changed files, commands, checks, findings, unresolved risks, and next actions.

### Competition

Keep Codex and Claude candidates independent. Do not expose one candidate's implementation to the other before judging. Require both judges, weighted scoring, a provisional winner, a separate convergence worktree, and final checks.

### Alternating

Use one worktree with one active writer at a time. Fail over only for configured availability classes. Record requested worker, executed worker, failure class, and reason. Do not fail over ordinary implementation or test failures as though they were quota problems.

## Review and verification loop

1. Builder output is provisional.
2. Run configured checks and gather evidence.
3. Independent reviewer returns `PASS`, `BLOCKED`, or `NEEDS_HUMAN` with evidence.
4. If `BLOCKED`, issue a bounded fix contract containing the prioritized findings and required verification.
5. Re-review the fix delta.
6. Final completion requires passing checks, reviewer approval, and no unresolved human gate.

## Status vocabulary

Use these statuses exactly when reporting to the human:

- `RUNNING`: a stage is actively executing.
- `PASS`: the stage or review met its contract, with evidence.
- `BLOCKED`: a worker, check, or dependency failed; include exact reason and next action.
- `NEEDS_HUMAN`: an approval, credential, ambiguous decision, destructive action, or hardware boundary requires the human.
- `QUOTA_PAUSED`: a recognized temporary usage window is exhausted and the run is durably resumable.

Never report success solely from process exit code. Check semantic output, generated artifacts, stage state, and configured validation.

## Blocker contract

When blocked, report:

- run ID and stage;
- requested and executed worker;
- exact failure class and message;
- whether retry is safe;
- preserved worktree and shared-memory paths;
- next executable command or human decision.

Do not obscure authentication, entitlement, billing, model, command-line, encoding, or repository failures as generic “quota.”

## Handoff contract

Every completed worker handoff must record:

- role and worker/runtime used;
- model and effort when available;
- command/session mode;
- objective and acceptance criteria addressed;
- changed files or reviewed files;
- commands and checks run;
- passed, failed, skipped, and deferred evidence;
- findings with severity and file/path evidence;
- known compromises and unresolved risks;
- exact next actions;
- whether human approval is required.

## Shared-memory contract

Provider-private histories are not shared. Canonical cross-worker memory consists only of explicit files and normalized records under the run state, including roadmap slices, goal contracts, decisions, stage outputs, changed paths, test and experiment evidence, review findings, and artifact hashes.

Read status through Commander state and `shared-context/`. Do not invent memory from prior chat sessions.

## Final report

At completion, report:

- mode and run ID;
- versions and roadmap items addressed;
- worker/model/effort actually used, including failovers;
- files changed;
- tests, experiments, and checks with results;
- reviewer verdicts and security findings;
- iteration and result artifacts;
- approvals granted and still pending;
- compromises, deferred evidence, and remaining risks;
- candidate/converged worktree path;
- explicit confirmation that no merge, push, deployment, publication, release, credential change, or hardware operation occurred.

You operate the harness. Codex and Claude perform the delegated work. Hermes Legion Commander remains the source of truth.
"""


def supervisor_skill() -> str:
    return """# Hermes Legion Commander skill

Operate `hermes-legion-commander` from the local terminal. Hermes is the harness operator; it does not perform implementation or review itself.

## Required intake

1. Resolve absolute Commander, config, and target-repository paths.
2. Read the target repository's `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and selected roadmap section when present.
3. Build a goal contract from `GOAL-CONTRACT.md`.
4. Run worker resolution and local-only roadmap preflight.
5. Choose council, competition, or alternating mode deliberately.

## Required execution

1. Launch the exact Commander command.
2. Preserve and reuse the run ID.
3. When using Hermes Kanban workers, assign either generic profile through an explicit dispatch contract. The contract must name the role, Codex-or-Claude runtime, permission, model, effort, workspace, context, checks, and handoff requirements.
4. Never infer a worker role from `legion-worker-a` or `legion-worker-b`; both profiles are interchangeable.
5. Read status from stage `state.json`, run `result.json`, approval request files, and `shared-context/`.
6. Surface approval requests and stop for the human.
7. On interruption, resume the same run rather than restarting completed work.
8. On review failure, issue a scoped fix contract from exact findings and then re-review the delta.
9. Report `PASS`, `BLOCKED`, `NEEDS_HUMAN`, `RUNNING`, or `QUOTA_PAUSED` with evidence.

## Prohibited

- Do not edit the target repository as Hermes.
- Do not call Codex or Claude directly for normal execution.
- Do not let a reviewer silently become a builder.
- Do not treat builder self-report as completion.
- Do not merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.

Commander handles UTF-8 transport, environment sanitization, shared memory, worktrees, failover, approvals, validation, and convergence.
"""


def goal_contract_template() -> str:
    return """# Goal contract

## Objective

State the concrete outcome.

## Scope

- Target repository:
- Version or roadmap range:
- Included components:
- Explicit exclusions:

## Constraints

List architecture, safety, compatibility, dependency, tool, and environment constraints.

## Acceptance criteria

Use testable statements. Include functional, security, documentation, migration, and observability criteria when applicable.

## Forbidden actions

State actions that are not authorized, including merge, push, deployment, release, credential changes, live actuation, or hardware operation.

## Required checks and experiments

List exact commands or evidence categories. Mark unavailable HIL/field evidence as deferred rather than simulated dishonestly.

## Quota and handoff policy

- Quota/context watermark, default 80%.
- Stop policy: finish active version, run focused tests/experiment, then hand off.
- Never start a new version when quota/context risk is already high.
- Handoff must include HEAD, package version, branch, tree state, next version, remaining range, and one gotcha line.

## Generated-artifact policy

Commit or report only current-version evidence unless explicitly told otherwise. Treat unrelated run output, signer churn, cache files, and provider session state as generated noise.

## Required handoff evidence

- Worker/runtime/model/effort
- Changed or reviewed files
- Commands run
- Checks and results
- Findings and severity
- Compromises and unresolved risks
- Next actions
- Human approvals required
- Clean-boundary handoff line when pausing

## Completion gate

Work is complete only when configured checks pass, independent review approves, required artifacts exist, and no human gate remains unresolved.
"""


def handoff_schema() -> str:
    return """# Worker handoff schema

A handoff should normalize to the following fields:

```json
{
  "status": "PASS | BLOCKED | NEEDS_HUMAN",
  "role": "",
  "requested_worker": "",
  "executed_worker": "",
  "runtime": "",
  "model": "",
  "effort": "",
  "session_mode": "",
  "objective_addressed": [],
  "changed_files": [],
  "reviewed_files": [],
  "commands_run": [],
  "checks": [
    {"command": "", "status": "passed | failed | skipped | deferred", "evidence": ""}
  ],
  "findings": [
    {"severity": "", "path": "", "issue": "", "evidence": "", "required_fix": ""}
  ],
  "compromises": [],
  "unresolved_risks": [],
  "next_actions": [],
  "human_approval_required": false
}
```

Do not omit blockers or convert missing evidence into success.
"""


def setup_profile(config: SupervisorConfig, *, clone: bool = False, force: bool = False,
                  setup_workers: bool = True) -> Path:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    home = profile_home(config.profile)
    if not home.exists():
        command = [config.hermes_executable, "profile", "create", config.profile,
                   "--description", "Harness operator for Hermes Legion Commander council, competition, and alternating runs"]
        if clone:
            command.append("--clone")
        _run(command)
    home.mkdir(parents=True, exist_ok=True)
    soul = home / "SOUL.md"
    if soul.exists() and not force:
        backup = soul.with_name(f"SOUL.md.backup-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(soul, backup)
    soul.write_text(supervisor_soul(config.repo_root, config.worker_profiles), encoding="utf-8")
    skill_dir = home / "skills" / "hermes-legion-commander"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(supervisor_skill(), encoding="utf-8")
    (skill_dir / "GOAL-CONTRACT.md").write_text(goal_contract_template(), encoding="utf-8")
    (skill_dir / "HANDOFF-SCHEMA.md").write_text(handoff_schema(), encoding="utf-8")
    worker_homes: list[Path] = []
    if setup_workers:
        worker_homes = profile_harness.setup_worker_profiles(
            profiles=config.worker_profiles,
            repo_root=config.repo_root,
            hermes_executable=config.hermes_executable,
            clone=clone,
            force=force,
        )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "profile.json").write_text(
        json.dumps({
            "profile": config.profile,
            "profile_home": str(home),
            "repo_root": str(config.repo_root),
            "configured_at": dt.datetime.now(UTC).isoformat(),
            "worker_profiles": list(config.worker_profiles),
            "worker_profile_homes": [str(path) for path in worker_homes],
            "installed_files": [
                str(soul),
                str(skill_dir / "SKILL.md"),
                str(skill_dir / "GOAL-CONTRACT.md"),
                str(skill_dir / "HANDOFF-SCHEMA.md"),
            ],
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return home

def commander_command(
    mode: str,
    *,
    commander: str,
    config_path: Path,
    repo: Path,
    from_version: int,
    to_version: int,
    run_id: str | None,
    dry_run: bool,
    no_wait: bool,
) -> list[str]:
    if mode in {"council", "alternating"}:
        command = [
            commander, "council", "--config", str(config_path), "campaign",
            "--from-version", str(from_version), "--to-version", str(to_version),
            "--strategy", "alternating" if mode == "alternating" else "full",
        ]
        if run_id:
            command.extend(["--run-id", run_id])
        if dry_run:
            command.append("--dry-run")
        if no_wait:
            command.append("--no-wait")
        return command
    if mode == "competition":
        command = [
            commander, "checkpoint", "--config", str(config_path), "--repo", str(repo),
            "run", "--from-version", str(from_version), "--to-version", str(to_version),
        ]
        if dry_run:
            command.append("--dry-run")
        if no_wait:
            command.append("--no-wait")
        return command
    raise SupervisorError(f"unknown supervisor mode: {mode}")


def prompt_for_command(command: list[str], mode: str, repo: Path) -> str:
    try:
        repo_facts = json.dumps(quick_repo_facts(repo), indent=2, sort_keys=True)
    except Exception as exc:  # pragma: no cover - repo graph is an advisory preflight
        repo_facts = json.dumps({"available": False, "error": str(exc)}, indent=2, sort_keys=True)
    quoted = subprocess.list2cmdline(command) if os.name == "nt" else " ".join(
        subprocess.list2cmdline([part]) for part in command
    )
    return f"""Operate Hermes Legion Commander in {mode} mode as a harness operator.

Target repository: {repo}

Repository graph preflight facts:

```json
{repo_facts}
```

Before execution:
1. Verify the absolute config and repository paths.
2. Read applicable repository instructions and the selected roadmap slice.
3. Form a bounded goal contract: objective, scope, constraints, acceptance criteria, forbidden actions, required checks, evidence, and human gates.
4. Run the corresponding local-only worker check, roadmap preflight, and repo-graph/context-pack preflight.

Run this exact command from the Hermes Legion Commander repository root:

{quoted}

Do not implement or review code as Hermes. Do not invoke Codex CLI or Claude Code directly. If an approval gate is reached, report `NEEDS_HUMAN`, show the request file, and stop. If a worker blocks, report the exact failure class, preserved state, and next action. If review fails, use the findings to create a scoped fix contract rather than vague reruns. Report `PASS`, `BLOCKED`, `NEEDS_HUMAN`, `RUNNING`, or `QUOTA_PAUSED` with evidence. Never merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.
"""

def run_via_hermes(config: SupervisorConfig, prompt: str) -> int:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    command = [config.hermes_executable, "-p", config.profile, "chat", "-q", prompt]
    completed = subprocess.run(command, cwd=config.repo_root, check=False)
    return completed.returncode



def run_worker_profile(config: SupervisorConfig, profile: str, contract_path: Path) -> int:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    prompt = profile_harness.dispatch_prompt(contract_path)
    command = [config.hermes_executable, "-p", profile, "chat", "-q", prompt]
    completed = subprocess.run(command, cwd=config.repo_root, check=False)
    return completed.returncode


def summarize_run(state_dir: Path, run_id: str) -> dict[str, Any]:
    """Return a provider-neutral status summary without invoking a model."""
    candidates = [path for path in state_dir.rglob(f"*{run_id}*") if path.is_dir()]
    run_root = next((path for path in candidates if (path / "job.json").is_file() or (path / "manifest.json").is_file()), None)
    if run_root is None:
        raise SupervisorError(f"run not found under {state_dir}: {run_id}")
    stages: list[dict[str, Any]] = []
    for path in sorted(run_root.rglob("state.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stages.append({
            "stage": str(path.parent.relative_to(run_root)),
            "status": payload.get("status"),
            "requested_agent": payload.get("requested_agent", payload.get("agent")),
            "executed_agent": payload.get("agent"),
            "runtime": payload.get("runtime"),
            "attempts": payload.get("attempts"),
            "retry_at": payload.get("retry_at"),
            "failovers": payload.get("failovers", []),
        })
    approvals = [str(path.relative_to(run_root)) for path in sorted(run_root.rglob("approval-request-*.json"))]
    result = run_root / "result.json"
    return {
        "run_id": run_id,
        "run_root": str(run_root),
        "stages": stages,
        "approval_requests": approvals,
        "result_available": result.is_file(),
        "shared_context": str(run_root / "shared-context"),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="legion-supervisor")
    p.add_argument("--worker-profile-a", default="legion-worker-a")
    p.add_argument("--worker-profile-b", default="legion-worker-b")
    p.add_argument("--hermes-executable", default="hermes")
    p.add_argument("--commander-executable", default="hermes-legion-commander")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("setup", help="Create or repair the supervisor and two generic worker profiles")
    setup.add_argument("--clone", action="store_true")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--supervisor-only", action="store_true")
    run = sub.add_parser("run", help="Ask Hermes to launch a council, competition, or alternating run")
    run.add_argument("--mode", choices=("council", "competition", "alternating"), required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--from-version", type=int, required=True)
    run.add_argument("--to-version", type=int, required=True)
    run.add_argument("--run-id")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-wait", action="store_true")
    preview = sub.add_parser("print-command", help="Print the exact Commander command and Hermes prompt without invoking a model")
    preview.add_argument("--mode", choices=("council", "competition", "alternating"), required=True)
    preview.add_argument("--config", type=Path, required=True)
    preview.add_argument("--repo", type=Path, required=True)
    preview.add_argument("--from-version", type=int, required=True)
    preview.add_argument("--to-version", type=int, required=True)
    preview.add_argument("--run-id")
    preview.add_argument("--dry-run", action="store_true")
    preview.add_argument("--no-wait", action="store_true")
    sub.add_parser("show-soul", help="Print the generated Hermes supervisor SOUL.md")
    sub.add_parser("show-skill", help="Print the generated Hermes supervisor SKILL.md")
    sub.add_parser("show-goal-contract", help="Print the goal-contract template")
    sub.add_parser("show-handoff-schema", help="Print the normalized handoff schema")
    show_worker_soul = sub.add_parser("show-worker-soul", help="Print a generic role-neutral worker SOUL.md")
    show_worker_soul.add_argument("--worker-profile")
    sub.add_parser("show-worker-skill", help="Print the generic worker SKILL.md")
    sub.add_parser("show-dispatch-contract", help="Print the generic worker dispatch-contract template")
    assignment = sub.add_parser("assignment-plan", help="Print the default generic-profile assignment plan without invoking a model")
    assignment.add_argument("--mode", choices=profile_harness.VALID_MODES, required=True)

    def add_dispatch_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--worker-profile")
        command_parser.add_argument("--mode", choices=profile_harness.VALID_MODES, required=True)
        command_parser.add_argument("--role", choices=profile_harness.VALID_ROLES, required=True)
        command_parser.add_argument("--runtime", choices=profile_harness.VALID_RUNTIMES, required=True)
        command_parser.add_argument("--permission", choices=profile_harness.VALID_PERMISSIONS, required=True)
        command_parser.add_argument("--workspace", type=Path, required=True)
        command_parser.add_argument("--context-dir", type=Path, required=True)
        command_parser.add_argument("--prompt-file", type=Path, required=True)
        command_parser.add_argument("--output-file", type=Path, required=True)
        command_parser.add_argument("--run-id", default="")
        command_parser.add_argument("--stage", default="")
        command_parser.add_argument("--candidate", default="")
        command_parser.add_argument("--model", default="")
        command_parser.add_argument("--effort", default="medium")
        command_parser.add_argument("--allow-runtime-fallback", action="store_true")
        command_parser.add_argument("--objective", required=True)

    preview_dispatch = sub.add_parser("print-dispatch", help="Print and persist a generic worker dispatch contract without invoking a model")
    add_dispatch_arguments(preview_dispatch)
    dispatch = sub.add_parser("dispatch", help="Invoke one generic Hermes worker profile with an explicit role/runtime contract")
    add_dispatch_arguments(dispatch)

    status = sub.add_parser("status", help="Read durable stage, approval, failover, and result status without invoking a model")
    status.add_argument("--state-dir", type=Path, required=True)
    status.add_argument("--run-id", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = SupervisorConfig(
        profile=args.profile,
        worker_profiles=(args.worker_profile_a, args.worker_profile_b),
        hermes_executable=args.hermes_executable,
        commander_executable=args.commander_executable,
        repo_root=args.repo_root.resolve(),
    )
    try:
        if args.action == "show-soul":
            print(supervisor_soul(config.repo_root, config.worker_profiles))
            return 0
        if args.action == "show-skill":
            print(supervisor_skill())
            return 0
        if args.action == "show-goal-contract":
            print(goal_contract_template())
            return 0
        if args.action == "show-handoff-schema":
            print(handoff_schema())
            return 0
        if args.action == "show-worker-soul":
            profile = args.worker_profile or config.worker_profiles[0]
            print(profile_harness.generic_worker_soul(profile, config.repo_root))
            return 0
        if args.action == "show-worker-skill":
            print(profile_harness.generic_worker_skill())
            return 0
        if args.action == "show-dispatch-contract":
            print(profile_harness.dispatch_contract_template())
            return 0
        if args.action == "assignment-plan":
            print(json.dumps(
                profile_harness.assignment_plan(args.mode, config.worker_profiles),
                indent=2,
            ))
            return 0
        if args.action in {"print-dispatch", "dispatch"}:
            profile = args.worker_profile or config.worker_profiles[0]
            payload = profile_harness.build_dispatch_contract(
                profile=profile,
                mode=args.mode,
                role=args.role,
                native_runtime=args.runtime,
                permission=args.permission,
                workspace=args.workspace,
                shared_context=args.context_dir,
                prompt_file=args.prompt_file,
                output_file=args.output_file,
                run_id=args.run_id,
                stage=args.stage,
                candidate=args.candidate,
                model=args.model,
                effort=args.effort,
                allow_runtime_fallback=args.allow_runtime_fallback,
                objective=args.objective,
            )
            contract_path = profile_harness.write_dispatch_contract(config.state_dir, payload)
            result = {"contract_path": str(contract_path), "contract": payload,
                      "hermes_prompt": profile_harness.dispatch_prompt(contract_path)}
            print(json.dumps(result, indent=2))
            if args.action == "dispatch":
                return run_worker_profile(config, profile, contract_path)
            return 0
        if args.action == "status":
            print(json.dumps(summarize_run(args.state_dir.resolve(), args.run_id), indent=2))
            return 0
        if args.action == "setup":
            print(setup_profile(
                config,
                clone=args.clone,
                force=args.force,
                setup_workers=not args.supervisor_only,
            ))
            return 0
        command = commander_command(
            args.mode,
            commander=config.commander_executable,
            config_path=args.config.resolve(),
            repo=args.repo.resolve(),
            from_version=args.from_version,
            to_version=args.to_version,
            run_id=args.run_id,
            dry_run=args.dry_run,
            no_wait=args.no_wait,
        )
        prompt = prompt_for_command(command, args.mode, args.repo.resolve())
        if args.action == "print-command":
            print(json.dumps({"command": command, "prompt": prompt}, indent=2))
            return 0
        return run_via_hermes(config, prompt)
    except SupervisorError as exc:
        print(f"supervisor error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
