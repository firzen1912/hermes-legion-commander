"""Generic Hermes worker profiles and dispatch contracts.

The two Hermes worker profiles are deliberately role-neutral and runtime-neutral.
The supervisor assigns the task role, native CLI, permissions, model, effort,
workspace, shared context, and handoff requirements for every dispatch.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .prompt_contracts import host_side_evidence_boundary, per_version_recipe, quota_handoff_template, version_execution_contract

UTC = dt.timezone.utc
DEFAULT_WORKER_PROFILES = ("legion-worker-a", "legion-worker-b")
VALID_MODES = ("council", "competition", "alternating")
VALID_RUNTIMES = ("codex", "claude")
VALID_PERMISSIONS = ("read-only", "workspace-write")
VALID_ROLES = (
    "roadmap_plan_reviewer",
    "researcher",
    "literature_reviewer",
    "prototyper",
    "code_polisher",
    "security_assurance",
    "validation_artifacts",
    "judge",
    "converger",
    "iteration_documenter",
    "evidence_reconciler",
)


class ProfileHarnessError(RuntimeError):
    pass


def profile_home(profile: str) -> Path:
    return Path.home() / ".hermes" / "profiles" / profile


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ProfileHarnessError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr or completed.stdout}"
        )
    return completed


def generic_worker_soul(profile: str, repo_root: Path) -> str:
    return f"""# Hermes Legion Generic Worker

You are `{profile}`, one of two interchangeable Hermes harness-worker profiles
managed by `legion-supervisor`.

Commander repository root: `{repo_root}`

## Identity

You are a **role-neutral harness operator**.

Your profile name does not make you a builder, reviewer, researcher, security
specialist, Codex worker, or Claude worker. The supervisor assigns those facts
for each task through an explicit dispatch contract.

A valid dispatch contract must identify:

- operating mode: council, competition, or alternating;
- assigned role;
- native runtime: Codex CLI or Claude Code;
- workspace and canonical shared-context snapshot;
- read-only or workspace-write permission;
- model and effort when specified;
- objective, constraints, acceptance criteria, forbidden actions, checks, and
  required handoff evidence.

Do not infer a role or native runtime from your profile name.

## Non-negotiables

1. Call `kanban_show()` first when spawned by Hermes Kanban. Read the full task,
   parent handoffs, comments, and dispatch contract.
2. Use `$HERMES_KANBAN_WORKSPACE` when present. Otherwise use only the absolute
   workspace in the dispatch contract.
3. Read repository instructions such as `AGENTS.md`, `CLAUDE.md`,
   `CONTRIBUTING.md`, and scoped instruction files before launching a native CLI.
4. Do not implement, review, research, or patch files using Hermes-native editing
   tools. Hermes is the harness. The assigned native CLI performs the role.
5. Launch only the native runtime named in the dispatch contract:
   - `codex` means Codex CLI;
   - `claude` means Claude Code.
6. Do not silently substitute the other runtime. A fallback is allowed only when
   the supervisor explicitly issues a new dispatch contract authorizing it.
7. Respect the assigned permission:
   - `read-only`: inspect and run non-mutating checks; do not edit;
   - `workspace-write`: edits are allowed only inside the assigned worktree.
8. Never inspect or modify sibling candidate worktrees in competition mode.
9. Treat the shared-context snapshot as read-only. It is canonical memory, not a
   scratch directory.
10. Keep disposable prompt or adapter files under `.tmp/` inside the assigned
    workspace.
11. Do not merge, push, deploy, tag, publish, release, alter credentials, enable
    live actuation, or operate hardware.
12. If the contract is missing, contradictory, or ambiguous, return
    `NEEDS_HUMAN`; do not guess.

## Runtime procedure

### Codex CLI assignment

Package the assigned role and goal contract for Codex. Prefer the configured
noninteractive Codex workflow. Capture the exact command/session mode, changed
or reviewed files, commands, checks, compromises, and final message.

Codex may be assigned any role, including review-only roles. Obey the dispatch
permission rather than assuming Codex must edit.

### Claude Code assignment

Package the assigned role and goal contract for Claude Code. Use bounded
noninteractive operation and the exact allowed tools/permission from the
dispatch contract. Capture the command/session mode, changed or reviewed files,
commands, checks, findings, compromises, and final result.

Claude may be assigned implementation roles. Obey the dispatch permission rather
than assuming Claude must remain read-only.

## Mode behavior

### Council

Perform only the assigned stage. Read prior normalized handoffs from the shared
context. Do not perform later roles unless the supervisor issues another task.

### Competition

Operate only in the assigned candidate worktree. Keep the candidate independent
until the judging stage. During judging, inspect only the evidence paths
explicitly granted by the supervisor.

### Alternating

Use one active writer at a time. Read the previous handoff, continue only the
assigned bounded work, and release the workspace through a complete handoff.
Do not reinterpret a quota failover as permission to broaden scope.

## Versioned-roadmap execution

When the dispatch contract contains a roadmap version or version range, obey this execution contract:

{version_execution_contract()}

{per_version_recipe("<NN>")}

{host_side_evidence_boundary()}

{quota_handoff_template()}

## Blockers

If the native CLI is unavailable, unauthenticated, quota-blocked, billing-
blocked, crashes, requests missing human input, or violates the permission
boundary, report the exact failure class and evidence.

Use these statuses:

- `PASS`
- `BLOCKED`
- `NEEDS_HUMAN`
- `QUOTA_PAUSED`

A zero process exit code is not sufficient when semantic output reports an
error.

## Final handoff

Return one normalized handoff containing:

- status;
- mode and assigned role;
- profile;
- requested and executed native runtime;
- model, effort, and session mode;
- workspace and context paths;
- objective and acceptance criteria addressed;
- changed files and reviewed files;
- commands and checks with results;
- findings with severity and path evidence;
- compromises and unresolved risks;
- exact next actions;
- whether human approval is required.

You are an interchangeable harness worker. The supervisor assigns the role.
Codex or Claude performs the delegated work.
"""


def generic_worker_skill() -> str:
    return """# Generic Hermes Legion worker skill

This profile is not permanently bound to Codex, Claude, building, or reviewing.

## Required sequence

1. Read the full Kanban task with `kanban_show()` when available.
2. Locate and validate the dispatch contract.
3. Resolve the assigned workspace and shared-context snapshot.
4. Read repository instructions and the goal contract.
5. Verify the assigned role, native runtime, permission, model, and effort.
6. Launch the specified native CLI.
7. Monitor it and capture exact commands and semantic errors.
8. Return the normalized handoff schema.
9. Block rather than guessing when the contract is incomplete.

## Roadmap/version runs

When the task is a versioned roadmap implementation, enforce clean version boundaries, focused tests, signed evidence, roadmap status updates, generated-artifact discipline, and quota-aware handoff. Do not start the next version when the dispatch contract says quota/context is near the stop watermark.

## Runtime selection

- `native_runtime = "codex"`: operate Codex CLI.
- `native_runtime = "claude"`: operate Claude Code.

Either generic profile may operate either runtime.

## Permission selection

- `read-only`: do not edit implementation files.
- `workspace-write`: edit only inside the assigned worktree.

The dispatch contract, not the profile name, determines the role and permission.

## Safety

Never merge, push, deploy, publish, release, change credentials, or operate
hardware. Never write to canonical shared memory. Never inspect another
competition candidate unless judging access is explicitly granted.
"""


def dispatch_contract_template() -> str:
    return """# Generic worker dispatch contract

```json
{
  "schema_version": 1,
  "dispatch_id": "",
  "run_id": "",
  "mode": "council | competition | alternating",
  "stage": "",
  "role": "roadmap_plan_reviewer | researcher | literature_reviewer | prototyper | code_polisher | security_assurance | validation_artifacts | judge | converger | iteration_documenter | evidence_reconciler",
  "profile": "legion-worker-a | legion-worker-b",
  "native_runtime": "codex | claude",
  "permission": "read-only | workspace-write",
  "workspace": "",
  "shared_context": "",
  "prompt_file": "",
  "output_file": "",
  "candidate": "",
  "model": "",
  "effort": "low | medium | high",
  "allow_runtime_fallback": false,
  "objective": "",
  "constraints": [],
  "acceptance_criteria": [],
  "forbidden_actions": [],
  "required_checks": [],
  "commit_policy": "commander_uncommitted | commit_per_version | no_commits",
  "quota_watermark": "80%",
  "stop_policy": "finish_active_version_then_handoff",
  "generated_artifact_policy": "commit_current_version_evidence_only",
  "host_side_evidence_policy": "never_machine_award_physical_or_independent_gates",
  "required_handoff_fields": []
}
```

The profile must fail closed when mode, role, runtime, permission, workspace,
shared context, or objective is missing.
"""


def handoff_schema() -> str:
    return """# Generic worker handoff schema

```json
{
  "status": "PASS | BLOCKED | NEEDS_HUMAN | QUOTA_PAUSED",
  "dispatch_id": "",
  "run_id": "",
  "mode": "",
  "stage": "",
  "role": "",
  "profile": "",
  "requested_runtime": "",
  "executed_runtime": "",
  "model": "",
  "effort": "",
  "session_mode": "",
  "workspace": "",
  "shared_context": "",
  "objective_addressed": [],
  "acceptance_criteria_addressed": [],
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
"""


def assignment_plan(mode: str, profiles: tuple[str, str] = DEFAULT_WORKER_PROFILES) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ProfileHarnessError(f"unsupported mode: {mode}")
    a, b = profiles
    if mode == "council":
        return {
            "mode": mode,
            "profiles": list(profiles),
            "assignments": [
                {"stage": "roadmap-plan-review", "profile": a, "runtime": "codex", "permission": "read-only"},
                {"stage": "research", "profile": a, "runtime": "codex", "permission": "workspace-write"},
                {"stage": "literature-review", "profile": b, "runtime": "claude", "permission": "workspace-write"},
                {"stage": "prototype", "profile": a, "runtime": "codex", "permission": "workspace-write"},
                {"stage": "code-polish", "profile": b, "runtime": "claude", "permission": "workspace-write"},
                {"stage": "security-assurance", "profile": b, "runtime": "claude", "permission": "workspace-write"},
                {"stage": "validation-artifacts", "profile": a, "runtime": "codex", "permission": "workspace-write"},
            ],
            "rule": "Roles are defaults only; the supervisor may swap profile or runtime per stage.",
        }
    if mode == "competition":
        return {
            "mode": mode,
            "profiles": list(profiles),
            "assignments": [
                {"candidate": "candidate-a", "profile": a, "runtime": "codex", "roles": "all", "permission": "workspace-write"},
                {"candidate": "candidate-b", "profile": b, "runtime": "claude", "roles": "all", "permission": "workspace-write"},
                {"stage": "judge-candidate-a", "profile": b, "runtime": "claude", "permission": "read-only"},
                {"stage": "judge-candidate-b", "profile": a, "runtime": "codex", "permission": "read-only"},
                {"stage": "convergence-pass-1", "profile": a, "runtime": "codex", "permission": "workspace-write"},
                {"stage": "convergence-pass-2", "profile": b, "runtime": "claude", "permission": "workspace-write"},
            ],
            "rule": "Profiles and runtimes may be swapped by the supervisor while candidate isolation remains mandatory.",
        }
    return {
        "mode": mode,
        "profiles": list(profiles),
        "assignments": [
            {"turn": 1, "profile": a, "runtime": "codex", "permission": "workspace-write"},
            {"turn": 2, "profile": b, "runtime": "claude", "permission": "workspace-write"},
            {"failover": "either profile may operate either runtime when explicitly redispatched"},
        ],
        "rule": "One active writer at a time; every handoff updates canonical shared memory.",
    }


def build_dispatch_contract(
    *,
    profile: str,
    mode: str,
    role: str,
    native_runtime: str,
    permission: str,
    workspace: Path,
    shared_context: Path,
    prompt_file: Path,
    output_file: Path,
    run_id: str = "",
    stage: str = "",
    candidate: str = "",
    model: str = "",
    effort: str = "medium",
    allow_runtime_fallback: bool = False,
    objective: str = "",
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    forbidden_actions: list[str] | None = None,
    required_checks: list[str] | None = None,
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ProfileHarnessError(f"invalid mode: {mode}")
    if role not in VALID_ROLES:
        raise ProfileHarnessError(f"invalid role: {role}")
    if native_runtime not in VALID_RUNTIMES:
        raise ProfileHarnessError(f"invalid native runtime: {native_runtime}")
    if permission not in VALID_PERMISSIONS:
        raise ProfileHarnessError(f"invalid permission: {permission}")
    if not objective.strip():
        raise ProfileHarnessError("dispatch objective must not be empty")
    return {
        "schema_version": 1,
        "dispatch_id": uuid.uuid4().hex,
        "created_at": dt.datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "mode": mode,
        "stage": stage,
        "role": role,
        "profile": profile,
        "native_runtime": native_runtime,
        "permission": permission,
        "workspace": str(workspace.resolve()),
        "shared_context": str(shared_context.resolve()),
        "prompt_file": str(prompt_file.resolve()),
        "output_file": str(output_file.resolve()),
        "candidate": candidate,
        "model": model,
        "effort": effort,
        "allow_runtime_fallback": bool(allow_runtime_fallback),
        "commit_policy": "commander_uncommitted",
        "quota_watermark": "80%",
        "stop_policy": "finish_active_version_then_handoff",
        "generated_artifact_policy": "commit_current_version_evidence_only",
        "host_side_evidence_policy": "never_machine_award_physical_or_independent_gates",
        "objective": objective,
        "constraints": list(constraints or []),
        "acceptance_criteria": list(acceptance_criteria or []),
        "forbidden_actions": list(forbidden_actions or []),
        "required_checks": list(required_checks or []),
        "required_handoff_fields": [
            "status", "dispatch_id", "run_id", "mode", "stage", "role",
            "profile", "requested_runtime", "executed_runtime", "workspace",
            "objective_addressed", "changed_files", "reviewed_files",
            "commands_run", "checks", "findings", "compromises",
            "unresolved_risks", "next_actions", "human_approval_required",
        ],
    }


def write_dispatch_contract(state_dir: Path, payload: dict[str, Any]) -> Path:
    dispatch_dir = state_dir / "dispatches"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    path = dispatch_dir / f"{payload['dispatch_id']}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def dispatch_prompt(contract_path: Path) -> str:
    return f"""Execute the generic Hermes Legion worker dispatch contract at:

{contract_path.resolve()}

Read the contract before acting. The contract assigns your role, native runtime,
permission, workspace, shared context, model, effort, objective, checks, and
handoff requirements. Do not infer any of those from your profile name.

Return the normalized handoff as the final response.
"""


def setup_worker_profile(
    *,
    profile: str,
    repo_root: Path,
    hermes_executable: str = "hermes",
    clone: bool = False,
    force: bool = False,
) -> Path:
    if shutil.which(hermes_executable) is None:
        raise ProfileHarnessError(f"Hermes executable not found: {hermes_executable}")
    home = profile_home(profile)
    if not home.exists():
        command = [
            hermes_executable, "profile", "create", profile,
            "--description", "Generic role-neutral harness worker for Hermes Legion Commander",
        ]
        if clone:
            command.append("--clone")
        _run(command)
    home.mkdir(parents=True, exist_ok=True)
    soul = home / "SOUL.md"
    if soul.exists() and not force:
        backup = soul.with_name(
            f"SOUL.md.backup-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy2(soul, backup)
    soul.write_text(generic_worker_soul(profile, repo_root), encoding="utf-8")
    skill_dir = home / "skills" / "hermes-legion-worker"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(generic_worker_skill(), encoding="utf-8")
    (skill_dir / "DISPATCH-CONTRACT.md").write_text(
        dispatch_contract_template(), encoding="utf-8"
    )
    (skill_dir / "HANDOFF-SCHEMA.md").write_text(
        handoff_schema(), encoding="utf-8"
    )
    return home


def setup_worker_profiles(
    *,
    profiles: tuple[str, str] = DEFAULT_WORKER_PROFILES,
    repo_root: Path,
    hermes_executable: str = "hermes",
    clone: bool = False,
    force: bool = False,
) -> list[Path]:
    if len(set(profiles)) != 2:
        raise ProfileHarnessError("exactly two distinct generic worker profiles are required")
    return [
        setup_worker_profile(
            profile=profile,
            repo_root=repo_root,
            hermes_executable=hermes_executable,
            clone=clone,
            force=force,
        )
        for profile in profiles
    ]
