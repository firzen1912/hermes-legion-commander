"""Hermes Agent supervisor integration for Hermes Legion Commander v2.

Hermes is the operator-facing harness controller.  Legion Commander owns team
formation, executor scheduling, campaign state, resource policy, shared evidence,
and protected human gates.  Legacy council/competition/alternating launchers are
retained as compatibility presets.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import profile_harness

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
    state_dir: Path = dataclasses.field(default_factory=lambda: Path.home() / ".hermes-legion-commander" / "supervisor")


def _run(command: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command, cwd=cwd, text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )
    if check and completed.returncode != 0:
        raise SupervisorError(f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr or completed.stdout}")
    return completed


def profile_home(profile: str) -> Path:
    return Path.home() / ".hermes" / "profiles" / profile


def supervisor_soul(repo_root: Path, worker_profiles: tuple[str, str] = profile_harness.DEFAULT_WORKER_PROFILES) -> str:
    return f"""# Hermes Legion Commander Supervisor

You are `legion-supervisor`, the operator-facing **harness operator** for Hermes Legion Commander.

Commander repository root: `{repo_root}`

## Identity

Hermes coordinates; Legion Commander executes. Do not edit implementation files in the target repository as Hermes.

The primary v2 engine is provider-agnostic. Never assume a fixed team size, a fixed provider list, a permanent role, or a permanent mapping between a model and a role. Provider, model, runtime, auth profile, executor, agent assignment, and role are independent resources.

The operator may define any role names and any number of executors. Commander may synthesize additional bounded roles under `commander_defined` or `hybrid` policy. OAuth/native subscription executors and API-backed executors may coexist in the same Legion. Multiple OAuth accounts from one provider are separate executor resources; preserve their configured account isolation and role affinity instead of relying on global CLI login state.

Two historical Hermes worker profiles (`{worker_profiles[0]}`, `{worker_profiles[1]}`) remain available only for legacy compatibility modes. They are not the v2 team-size limit.

## Required intake

1. Resolve the target repository and Legion config.
2. Read repository-local instructions (`AGENTS.md`, contributing/governance files, scoped policies) without editing them.
3. Convert the request into a bounded goal contract: objective, scope, constraints, acceptance criteria, forbidden actions, required checks, evidence, budgets, and human gates.
4. Run `hermes-legion-commander legion validate`, `skills check`, and `doctor --legion-config` as appropriate. For OAuth/native executors, use `legion accounts status` and surface any account that still needs `legion accounts login`.
5. Ask Commander to plan the team and DAG. Do not manually force a provider/role/account mapping unless the user requested it.
6. Launch the exact Commander command and preserve its state directory.

## Team policy

- `user_defined`: preserve the user's team/roles exactly.
- `commander_defined`: let Commander synthesize a minimal bounded team.
- `hybrid`: user roles are pinned/overrides; Commander fills missing competencies.

A worker may request another competency, but workers do not recursively spawn unlimited workers. Recruitment returns to Commander for duplicate-role, dependency, budget, and executor eligibility checks.

## Resource doctrine

Available capacity is not authorization to consume it. Respect Commander resource/context boundaries, checkpoints, bounded diagnosis, long-run limits, and phase separation. A resource-limit or automatic-compaction stop is a real boundary, not permission to switch to unrelated work.

## Review doctrine

Builder self-report is provisional. Independent review returns `PASS`, `BLOCKED`, or `NEEDS_HUMAN`. When review blocks, issue a scoped fix contract from exact findings and re-review the delta rather than rerunning vague work.

## Protected actions

Never autonomously merge, push, deploy, tag, publish, release, alter credentials, operate hardware, or enable live actuation. Protected actions remain explicit human gates regardless of model confidence or aggregate score.

## Shared evidence

Provider-private chat history is not shared. Cross-agent memory consists of explicit Commander-owned goal contracts, role contracts, work packets, checkpoints, artifacts, findings, verdicts, resource telemetry, and hashes.

## Status vocabulary

Use `RUNNING`, `PASS`, `BLOCKED`, `NEEDS_HUMAN`, and `QUOTA_PAUSED` exactly. A process exit code alone is never proof of semantic success.

## Final report

Report the objective, generated team/roles, actual executors/providers/models/auth classes used (never secret values), DAG/stage outcomes, checks/evidence, reviewer verdicts, resource or failover events, remaining risks, and pending human gates. Explicitly confirm that no protected action occurred automatically.
"""


def supervisor_skill() -> str:
    return """# Hermes Legion Commander supervisor skill

Use `hermes-legion-commander legion` as the primary execution path.

Required sequence:

1. Validate the Legion configuration.
2. Verify the reviewed skill baseline for every configured runtime.
3. Read target-repository instructions and form a bounded goal contract.
4. Run `legion plan` and inspect the generated team, executors, skill readiness, budgets, and DAG.
5. Run the campaign only when the plan is consistent with user constraints and no human gate is being bypassed.
6. Read campaign state/artifacts for status. Do not replace blocked executor work with Hermes-authored implementation.
7. Create scoped fix/review continuations rather than vague restarts.

Do not hard-code Codex, Claude, OpenAI, Anthropic, or any other provider as a role. OAuth/API/native authentication is executor configuration. Never place credentials in prompts or artifacts.

The historical `collaborating`, `competing`, and `alternating` commands remain compatibility presets for old campaigns.
"""


def goal_contract_template() -> str:
    return """# Goal contract

## Objective
State the concrete outcome.

## Scope
- Target repository:
- Included components/work packets:
- Explicit exclusions:

## Team constraints
- Team policy: user_defined | commander_defined | hybrid
- Pinned roles/executors/providers (if any):
- Independence requirements:

## Resource constraints
- Subscription reserve/budget:
- API soft/hard budget:
- Context/phase boundaries:

## Quota and handoff policy
- Available capacity is not authorization to consume it; preserve configured reserve, cooldown, and context boundaries.
- Do not start a new version or work packet when the configured quota/context watermark or stop boundary has been reached.
- If quota/context pressure appears mid-version, finish active version if feasible and safe, run its focused checks, persist the exact checkpoint/handoff, and then stop.
- Never switch or rotate accounts merely to evade a provider quota, cooldown, entitlement, or authentication boundary.
- Every quota pause must record changed/reviewed files, checks, resource events, unresolved work, and the exact next action.

## Acceptance criteria
Use testable functional, security, quality, evidence, and integration statements.

## Forbidden actions
State actions that are not authorized, including merge, push, deployment, release, credential changes, or hardware operation.

## Required checks and evidence
List the cheapest-to-expensive validation path and evidence requirements.

## Human gates
List protected decisions/actions requiring explicit operator approval.

## Handoff requirements
Require changed/reviewed files, commands, checks, findings, unresolved risks, resource events, and exact next action.
"""


def handoff_schema() -> str:
    return """# Worker handoff schema

```json
{
  "status": "PASS | BLOCKED | NEEDS_HUMAN | QUOTA_PAUSED",
  "role": "",
  "agent": "",
  "executor": "",
  "provider": "",
  "runtime": "",
  "model": "",
  "auth_kind": "oauth | api_key | native | none",
  "objective_addressed": [],
  "changed_files": [],
  "reviewed_files": [],
  "commands_run": [],
  "checks": [],
  "findings": [],
  "resource_events": [],
  "compromises": [],
  "unresolved_risks": [],
  "next_actions": [],
  "human_approval_required": false
}
```

Never include credential values or provider-private conversation state.

Legacy review-only verdict compatibility uses `"status": "PASS | BLOCKED | NEEDS_HUMAN"`;
`QUOTA_PAUSED` remains available for execution handoffs.
"""


def setup_profile(config: SupervisorConfig, *, clone: bool = False, force: bool = False, setup_workers: bool = True) -> Path:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    home = profile_home(config.profile)
    if not home.exists():
        command = [config.hermes_executable, "profile", "create", config.profile, "--description", "Harness operator for provider-agnostic Hermes Legion Commander"]
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
    if setup_workers:
        profile_harness.setup_worker_profiles(
            profiles=config.worker_profiles, repo_root=config.repo_root,
            hermes_executable=config.hermes_executable, clone=clone, force=force,
        )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "profile.json").write_text(json.dumps({
        "profile": config.profile,
        "profile_home": str(home),
        "repo_root": str(config.repo_root),
        "configured_at": dt.datetime.now(UTC).isoformat(),
        "legacy_worker_profiles": list(config.worker_profiles),
        "primary_engine": "legion",
    }, indent=2) + "\n", encoding="utf-8")
    return home


def legion_command(
    *, commander: str, config_path: Path, repo: Path, objective: str,
    state_dir: Path, plan_only: bool = False, timeout: int = 900,
) -> list[str]:
    command = [
        commander, "legion", "plan" if plan_only else "run",
        "--config", str(config_path), "--repo", str(repo),
        "--objective", objective, "--state-dir", str(state_dir),
    ]
    if not plan_only:
        command.extend(["--timeout", str(timeout)])
    return command


def commander_command(
    mode: str, *, commander: str, config_path: Path, repo: Path,
    from_version: int, to_version: int, run_id: str | None,
    dry_run: bool, no_wait: bool,
) -> list[str]:
    """Build historical mode commands for backward compatibility."""
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
    raise SupervisorError(f"unknown legacy supervisor mode: {mode}")


def prompt_for_command(command: list[str], mode: str, repo: Path) -> str:
    quoted = subprocess.list2cmdline(command) if os.name == "nt" else " ".join(subprocess.list2cmdline([part]) for part in command)
    return f"""Operate Hermes Legion Commander as a harness operator.

Target repository: {repo}
Execution path: {mode}

Run this exact command from the Commander repository root:

{quoted}

Do not implement or review code as Hermes. Inspect Commander state and evidence, surface protected human gates, and report exact executor/auth/resource failures without hiding failover. Never merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.
"""


def run_via_hermes(config: SupervisorConfig, prompt: str) -> int:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    return subprocess.run([config.hermes_executable, "-p", config.profile, "chat", "-q", prompt], cwd=config.repo_root, check=False).returncode


def run_worker_profile(config: SupervisorConfig, profile: str, contract_path: Path) -> int:
    if shutil.which(config.hermes_executable) is None:
        raise SupervisorError(f"Hermes executable not found: {config.hermes_executable}")
    prompt = profile_harness.dispatch_prompt(contract_path)
    return subprocess.run([config.hermes_executable, "-p", profile, "chat", "-q", prompt], cwd=config.repo_root, check=False).returncode


def summarize_run(state_dir: Path, run_id: str) -> dict[str, Any]:
    direct = state_dir / run_id
    state_path = direct / "campaign-state.json"
    if state_path.is_file():
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        return {"run_id": run_id, "run_root": str(direct), "campaign": payload, "shared_context": str(direct / "artifacts")}
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
        stages.append({"stage": str(path.parent.relative_to(run_root)), "status": payload.get("status"), "runtime": payload.get("runtime")})
    return {"run_id": run_id, "run_root": str(run_root), "stages": stages, "shared_context": str(run_root / "shared-context")}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="legion-supervisor")
    p.add_argument("--worker-profile-a", default="legion-worker-a")
    p.add_argument("--worker-profile-b", default="legion-worker-b")
    p.add_argument("--hermes-executable", default="hermes")
    p.add_argument("--commander-executable", default="hermes-legion-commander")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    sub = p.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("setup")
    setup.add_argument("--clone", action="store_true")
    setup.add_argument("--force", action="store_true")
    setup.add_argument("--supervisor-only", action="store_true")

    for action in ("run", "print-command"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--mode", choices=("legion", "council", "competition", "alternating"), default="legion")
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--repo", type=Path, required=True)
        cmd.add_argument("--objective", default="")
        cmd.add_argument("--state-dir", type=Path, default=Path(".legion-state"))
        cmd.add_argument("--timeout", type=int, default=900)
        cmd.add_argument("--plan-only", action="store_true")
        cmd.add_argument("--from-version", type=int)
        cmd.add_argument("--to-version", type=int)
        cmd.add_argument("--run-id")
        cmd.add_argument("--dry-run", action="store_true")
        cmd.add_argument("--no-wait", action="store_true")

    sub.add_parser("show-soul")
    sub.add_parser("show-skill")
    sub.add_parser("show-goal-contract")
    sub.add_parser("show-handoff-schema")
    show_worker_soul = sub.add_parser("show-worker-soul")
    show_worker_soul.add_argument("--worker-profile")
    sub.add_parser("show-worker-skill")
    sub.add_parser("show-dispatch-contract")
    assignment = sub.add_parser("assignment-plan")
    assignment.add_argument("--mode", choices=profile_harness.VALID_MODES, required=True)
    status = sub.add_parser("status")
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
            print(supervisor_soul(config.repo_root, config.worker_profiles)); return 0
        if args.action == "show-skill":
            print(supervisor_skill()); return 0
        if args.action == "show-goal-contract":
            print(goal_contract_template()); return 0
        if args.action == "show-handoff-schema":
            print(handoff_schema()); return 0
        if args.action == "show-worker-soul":
            print(profile_harness.generic_worker_soul(args.worker_profile or config.worker_profiles[0], config.repo_root)); return 0
        if args.action == "show-worker-skill":
            print(profile_harness.generic_worker_skill()); return 0
        if args.action == "show-dispatch-contract":
            print(profile_harness.dispatch_contract_template()); return 0
        if args.action == "assignment-plan":
            print(json.dumps(profile_harness.assignment_plan(args.mode, config.worker_profiles), indent=2)); return 0
        if args.action == "setup":
            home = setup_profile(config, clone=args.clone, force=args.force, setup_workers=not args.supervisor_only)
            print(home); return 0
        if args.action == "status":
            print(json.dumps(summarize_run(args.state_dir.resolve(), args.run_id), indent=2)); return 0
        if args.action in {"run", "print-command"}:
            if args.mode == "legion":
                if not args.objective.strip():
                    raise SupervisorError("--objective is required for --mode legion")
                command = legion_command(
                    commander=config.commander_executable, config_path=args.config.resolve(), repo=args.repo.resolve(),
                    objective=args.objective, state_dir=args.state_dir.resolve(), plan_only=args.plan_only, timeout=args.timeout,
                )
            else:
                if args.from_version is None or args.to_version is None:
                    raise SupervisorError("legacy modes require --from-version and --to-version")
                command = commander_command(
                    args.mode, commander=config.commander_executable, config_path=args.config.resolve(), repo=args.repo.resolve(),
                    from_version=args.from_version, to_version=args.to_version, run_id=args.run_id,
                    dry_run=args.dry_run, no_wait=args.no_wait,
                )
            prompt = prompt_for_command(command, args.mode, args.repo.resolve())
            if args.action == "print-command":
                print(json.dumps({"command": command, "prompt": prompt}, indent=2)); return 0
            return run_via_hermes(config, prompt)
    except (SupervisorError, profile_harness.ProfileHarnessError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
