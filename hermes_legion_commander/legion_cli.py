"""CLI for provider-agnostic Legion team planning and campaign DAG execution."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .campaign_engine import CampaignEngine, graph_from_assignments
from .executor_runtime import run_account_action
from .legion import LegionError, TeamPlanner
from .legion_config import LegionConfig, load
from .skill_profile import roots_from_registry, verify_roots


CONFIG_EXAMPLE = r'''# Hermes Legion Commander provider-agnostic configuration
[legion]
team_policy = "hybrid" # user_defined | commander_defined | hybrid

# ACCOUNT PROFILES
# One OAuth/native profile per login. `email` is an operator-facing identifier;
# credentials stay in the native CLI credential store and never enter prompts.
[[auth_profiles]]
id = "chatgpt-simulation-account"
kind = "oauth"
provider = "openai"
source = "native"
email = "simulation@example.com"
account_label = "ChatGPT simulation"
[auth_profiles.environment]
CODEX_HOME = "~/.local/share/hermes-legion-commander/accounts/chatgpt-simulation/codex"

[[auth_profiles]]
id = "chatgpt-capability-account"
kind = "oauth"
provider = "openai"
source = "native"
email = "capability@example.com"
account_label = "ChatGPT capability"
[auth_profiles.environment]
CODEX_HOME = "~/.local/share/hermes-legion-commander/accounts/chatgpt-capability/codex"

[[auth_profiles]]
id = "claude-debt-account"
kind = "oauth"
provider = "anthropic"
source = "native"
email = "debt@example.com"
account_label = "Claude debt"
[auth_profiles.environment]
CLAUDE_CONFIG_DIR = "~/.local/share/hermes-legion-commander/accounts/claude-debt/claude"

[[auth_profiles]]
id = "claude-review-account"
kind = "oauth"
provider = "anthropic"
source = "native"
email = "review@example.com"
account_label = "Claude review"
[auth_profiles.environment]
CLAUDE_CONFIG_DIR = "~/.local/share/hermes-legion-commander/accounts/claude-review/claude"

# API accounts can coexist with OAuth accounts. Raw keys are never stored here.
[[auth_profiles]]
id = "openai-api-overflow"
kind = "api_key"
provider = "openai"
source = "environment"
secret_ref = "env:OPENAI_API_KEY_OVERFLOW"
account_label = "OpenAI API overflow"

# RUNTIME ADAPTERS
[[runtimes]]
id = "codex-cli"
provider = "openai"
transport = "cli"
command = ["codex", "exec", "--json", "{model_args}", "-"]
login_command = ["codex", "login"]
auth_status_command = ["codex", "login", "status"]
prompt_transport = "stdin"
output_format = "codex-jsonl"
auth_kinds = ["oauth", "api_key", "native"]
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.agents/skills"]

[[runtimes]]
id = "claude-code"
provider = "anthropic"
transport = "cli"
command = ["claude", "-p", "{prompt}", "{model_args}"]
login_command = ["claude", "auth", "login"]
auth_status_command = ["claude", "auth", "status"]
prompt_transport = "argument"
output_format = "text"
auth_kinds = ["oauth", "api_key", "native"]
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.agents/skills"]

# EXECUTORS
# Each account is an independent schedulable resource. Executor-specific skill
# roots keep the reviewed 86-skill baseline inside each isolated native CLI home.
[[executors]]
id = "chatgpt-simulation"
provider = "openai"
model = "default"
runtime = "codex-cli"
auth_profile = "chatgpt-simulation-account"
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.local/share/hermes-legion-commander/accounts/chatgpt-simulation/codex/skills"]
labels = ["simulation", "chatgpt"]
priority = 10
[executors.budget]
subscription_remaining_percent = 100
reserve_percent = 25
max_parallel = 1

[[executors]]
id = "chatgpt-capability"
provider = "openai"
model = "default"
runtime = "codex-cli"
auth_profile = "chatgpt-capability-account"
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.local/share/hermes-legion-commander/accounts/chatgpt-capability/codex/skills"]
labels = ["capability", "chatgpt"]
priority = 10
[executors.budget]
subscription_remaining_percent = 100
reserve_percent = 25
max_parallel = 1

[[executors]]
id = "claude-debt"
provider = "anthropic"
model = "default"
runtime = "claude-code"
auth_profile = "claude-debt-account"
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.local/share/hermes-legion-commander/accounts/claude-debt/claude/skills"]
labels = ["debt", "claude"]
priority = 10
[executors.budget]
subscription_remaining_percent = 100
reserve_percent = 25
max_parallel = 1

[[executors]]
id = "claude-review"
provider = "anthropic"
model = "default"
runtime = "claude-code"
auth_profile = "claude-review-account"
capabilities = ["repo_read", "repo_write", "shell"]
skill_roots = ["~/.local/share/hermes-legion-commander/accounts/claude-review/claude/skills"]
labels = ["review", "claude"]
priority = 10
[executors.budget]
subscription_remaining_percent = 100
reserve_percent = 25
max_parallel = 1

# ROLES
# `allowed_executors` pins a role to a specific account. Use
# `preferred_executors` when controlled failover is desired instead.
[[roles]]
id = "simulation-and-demo"
objective = "Own simulation, demo, scenario, and visualization work."
required_capabilities = ["repo_read", "repo_write"]
allowed_executors = ["chatgpt-simulation"]
permissions = ["repo_read", "repo_write", "shell"]

[[roles]]
id = "capability-improvement"
objective = "Own capability implementation and optimization."
required_capabilities = ["repo_read", "repo_write"]
allowed_executors = ["chatgpt-capability"]
permissions = ["repo_read", "repo_write", "shell"]

[[roles]]
id = "debt-clearance"
objective = "Identify and clear bounded technical debt work packets."
required_capabilities = ["repo_read", "repo_write"]
allowed_executors = ["claude-debt"]
permissions = ["repo_read", "repo_write", "shell"]

[[roles]]
id = "independent-review-and-integration"
objective = "Independently review and prepare merge-ready integration results."
required_capabilities = ["repo_read"]
allowed_executors = ["claude-review"]
permissions = ["repo_read", "shell"]
independent_from_roles = ["simulation-and-demo", "capability-improvement", "debt-clearance"]

# Optional arbitrary DAG. Omit it to let Commander build a safe graph from the
# planned roles and assignments. Role names and account mappings are never fixed.
'''


def _build(config: LegionConfig, objective: str, repo: Path, state_dir: Path) -> tuple[CampaignEngine, list[dict[str, object]]]:
    planner = TeamPlanner(config.registry, config.policy)
    roles, assignments = planner.plan(objective, user_roles=config.roles)
    role_map = {role.id: role for role in roles}
    graph = config.campaign if config.campaign.nodes else graph_from_assignments(assignments, role_map)
    engine = CampaignEngine(
        objective=objective,
        planner=planner,
        roles=roles,
        graph=graph,
        repo=repo,
        state_dir=state_dir,
    )
    assignment_rows = [
        {
            "id": assignment.id,
            "role": assignment.role,
            "executor": assignment.executor,
            "objective": assignment.objective,
            "permissions": sorted(assignment.permissions),
        }
        for assignment in assignments
    ]
    return engine, assignment_rows


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander legion",
        description="Plan and run arbitrary multi-provider, multi-role Legion campaign DAGs.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("config-example", help="Print a provider/auth/runtime/executor/role configuration example")
    validate = sub.add_parser("validate", help="Validate a Legion TOML configuration")
    validate.add_argument("--config", type=Path, required=True)
    roster = sub.add_parser("roster", help="Print registered auth, runtime, and executor resources")
    roster.add_argument("--config", type=Path, required=True)
    accounts = sub.add_parser("accounts", help="Manage isolated OAuth/native account profiles")
    account_sub = accounts.add_subparsers(dest="account_command", required=True)
    account_list = account_sub.add_parser("list", help="List configured account/executor mappings")
    account_list.add_argument("--config", type=Path, required=True)
    for action in ("login", "status"):
        account_action = account_sub.add_parser(action, help=f"{action.capitalize()} one isolated executor account")
        account_action.add_argument("--config", type=Path, required=True)
        account_action.add_argument("--executor", required=True)
        account_action.add_argument("--timeout", type=int, default=300 if action == "login" else 60)
    for name in ("plan", "run"):
        cmd = sub.add_parser(name, help=f"{name.capitalize()} a generic Legion campaign")
        cmd.add_argument("--config", type=Path, required=True)
        cmd.add_argument("--repo", type=Path, required=True)
        cmd.add_argument("--objective", required=True)
        cmd.add_argument("--state-dir", type=Path, default=Path(".legion-state"))
        if name == "run":
            cmd.add_argument("--timeout", type=int, default=900)
    return p


def cli_main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "config-example":
        print(CONFIG_EXAMPLE.rstrip())
        return 0
    try:
        config = load(args.config)
        if args.command == "accounts":
            if args.account_command == "list":
                rows = []
                for executor_id, executor in sorted(config.registry.executors.items()):
                    profile = config.registry.auth_profiles[executor.auth_profile]
                    if profile.kind.value not in {"oauth", "native"}:
                        continue
                    rows.append({
                        "executor": executor_id,
                        "provider": executor.provider,
                        "runtime": executor.runtime,
                        "auth_profile": profile.id,
                        "account_label": profile.account_label or profile.id,
                        "configured_email": profile.email,
                        "isolation_environment": profile.environment,
                        "labels": sorted(executor.labels),
                    })
                print(json.dumps(rows, indent=2, sort_keys=True))
                return 0
            result = run_account_action(
                config.registry, args.executor, args.account_command, timeout=args.timeout,
                interactive=args.account_command == "login",
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 2
        if args.command == "validate":
            print(json.dumps({
                "ok": True,
                "team_policy": config.policy.value,
                "auth_profiles": len(config.registry.auth_profiles),
                "runtimes": len(config.registry.runtimes),
                "executors": len(config.registry.executors),
                "roles": len(config.roles),
                "campaign_nodes": len(config.campaign.nodes),
            }, indent=2, sort_keys=True))
            return 0
        if args.command == "roster":
            print(json.dumps({
                "auth_profiles": {
                    profile_id: {
                        "kind": profile.kind.value,
                        "provider": profile.provider,
                        "source": profile.source,
                        "email": profile.email,
                        "account_label": profile.account_label or profile.id,
                        "isolation_environment": sorted(profile.environment),
                        "secret_ref": "configured" if profile.secret_ref else None,
                    }
                    for profile_id, profile in config.registry.auth_profiles.items()
                },
                "runtimes": {
                    runtime_id: {
                        "provider": runtime.provider,
                        "transport": runtime.transport,
                        "auth_kinds": sorted(kind.value for kind in runtime.auth_kinds),
                        "capabilities": sorted(runtime.capabilities),
                        "skill_roots": list(runtime.skill_roots),
                    }
                    for runtime_id, runtime in config.registry.runtimes.items()
                },
                "executors": {
                    executor_id: {
                        "provider": executor.provider,
                        "model": executor.model,
                        "runtime": executor.runtime,
                        "auth_profile": executor.auth_profile,
                        "skill_roots": list(executor.skill_roots),
                        "labels": sorted(executor.labels),
                        "enabled": executor.enabled,
                    }
                    for executor_id, executor in config.registry.executors.items()
                },
            }, indent=2, sort_keys=True))
            return 0
        skill_checks = verify_roots(roots_from_registry(config.registry))
        engine, assignments = _build(config, args.objective, args.repo, args.state_dir)
        if args.command == "plan":
            plan = engine.plan()
            plan["assignments"] = assignments
            plan["skill_profile"] = {
                "ok": bool(skill_checks) and all(check.ok for check in skill_checks),
                "roots": [check.root for check in skill_checks],
                "missing_by_root": {check.root: list(check.missing) for check in skill_checks if check.missing},
            }
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        if not skill_checks or not all(check.ok for check in skill_checks):
            raise LegionError(
                "reviewed Legion skill baseline is not synchronized for every runtime; run `hermes-legion-commander skills install --config <file>` first"
            )
        state = engine.run(timeout=args.timeout)
        print(json.dumps({
            "status": state.status,
            "state_dir": str(args.state_dir.resolve()),
            "nodes": {node_id: node.status for node_id, node in state.nodes.items()},
        }, indent=2, sort_keys=True))
        return 0 if state.status == "PASS" else 2
    except (LegionError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(cli_main())
