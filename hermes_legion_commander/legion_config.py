"""TOML configuration loader for provider-agnostic Legion campaigns."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config_toml import loads as toml_loads
from .legion import (
    AuthKind,
    AuthProfile,
    CampaignGraph,
    CampaignNode,
    Executor,
    ExecutorRegistry,
    LegionError,
    ResourceBudget,
    RoleContract,
    RuntimeAdapter,
    StageKind,
    TeamPolicy,
)


def _default_skill_set() -> frozenset[str]:
    # Lazy import avoids coupling the core datamodel to installer dependencies.
    from .skill_profile import EXPECTED_SKILLS
    return frozenset(EXPECTED_SKILLS)


@dataclass
class LegionConfig:
    policy: TeamPolicy
    registry: ExecutorRegistry
    roles: list[RoleContract]
    campaign: CampaignGraph
    raw: dict[str, Any]


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    raise LegionError(f"expected string or list, got {type(value).__name__}")


def _auth_kinds(value: Any) -> frozenset[AuthKind]:
    if value is None:
        return frozenset(AuthKind)
    return frozenset(AuthKind(str(item)) for item in _strings(value))


def _table_list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise LegionError(f"[[{key}]] must be an array of tables")
    return value


def load_dict(raw: dict[str, Any]) -> LegionConfig:
    legion_table = raw.get("legion", {})
    if not isinstance(legion_table, dict):
        raise LegionError("[legion] must be a table")
    policy = TeamPolicy(str(legion_table.get("team_policy", TeamPolicy.HYBRID.value)))

    registry = ExecutorRegistry()
    for row in _table_list(raw, "auth_profiles"):
        profile = AuthProfile(
            id=str(row.get("id", "")),
            kind=AuthKind(str(row.get("kind", "native"))),
            provider=str(row.get("provider", "*")),
            source=str(row.get("source", "native")),
            secret_ref=str(row["secret_ref"]) if row.get("secret_ref") is not None else None,
            email=str(row["email"]) if row.get("email") is not None else None,
            account_label=str(row.get("account_label", "")),
            environment={str(k): str(v) for k, v in row.get("environment", {}).items()} if isinstance(row.get("environment"), dict) else {},
            metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
        )
        registry.register_auth(profile)

    for row in _table_list(raw, "runtimes"):
        runtime = RuntimeAdapter(
            id=str(row.get("id", "")),
            provider=str(row.get("provider", "")),
            transport=str(row.get("transport", "cli")),
            command=_strings(row.get("command")),
            login_command=_strings(row.get("login_command")),
            auth_status_command=_strings(row.get("auth_status_command")),
            endpoint=str(row["endpoint"]) if row.get("endpoint") is not None else None,
            capabilities=frozenset(_strings(row.get("capabilities"))),
            auth_kinds=_auth_kinds(row.get("auth_kinds")),
            skill_roots=_strings(row.get("skill_roots")),
            output_format=str(row.get("output_format", "text")),
            prompt_transport=str(row.get("prompt_transport", "argument")),
            metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
        )
        registry.register_runtime(runtime)

    for row in _table_list(raw, "executors"):
        executor = Executor(
            id=str(row.get("id", "")),
            provider=str(row.get("provider", "")),
            model=str(row.get("model", "")),
            runtime=str(row.get("runtime", "")),
            auth_profile=str(row.get("auth_profile", "")),
            capabilities=frozenset(_strings(row.get("capabilities"))),
            skills=frozenset(_strings(row.get("skills"))) if row.get("skills") is not None else _default_skill_set(),
            skill_roots=_strings(row.get("skill_roots")),
            priority=int(row.get("priority", 100)),
            labels=frozenset(_strings(row.get("labels"))),
            enabled=bool(row.get("enabled", True)),
            metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
        )
        budget_row = row.get("budget", {}) if isinstance(row.get("budget"), dict) else {}
        budget = ResourceBudget(
            subscription_remaining_percent=(
                float(budget_row["subscription_remaining_percent"])
                if budget_row.get("subscription_remaining_percent") is not None else None
            ),
            reserve_percent=float(budget_row.get("reserve_percent", 25.0)),
            no_new_work_below_percent=float(budget_row.get("no_new_work_below_percent", 35.0)),
            spent_today_percent=float(budget_row.get("spent_today_percent", 0.0)),
            spent_session_percent=float(budget_row.get("spent_session_percent", 0.0)),
            daily_unattended_max_percent=float(budget_row.get("daily_unattended_max_percent", 10.0)),
            session_checkpoint_percent=float(budget_row.get("session_checkpoint_percent", 4.0)),
            session_hard_stop_percent=float(budget_row.get("session_hard_stop_percent", 5.0)),
            api_soft_budget_usd=(float(budget_row["api_soft_budget_usd"]) if budget_row.get("api_soft_budget_usd") is not None else None),
            api_hard_budget_usd=(float(budget_row["api_hard_budget_usd"]) if budget_row.get("api_hard_budget_usd") is not None else None),
            api_spend_usd=float(budget_row.get("api_spend_usd", 0.0)),
            max_parallel=int(budget_row.get("max_parallel", 1)),
            cooldown_until=str(budget_row["cooldown_until"]) if budget_row.get("cooldown_until") is not None else None,
            enabled=bool(budget_row.get("enabled", executor.enabled)),
        )
        registry.register_executor(executor, budget)

    roles: list[RoleContract] = []
    for row in _table_list(raw, "roles"):
        roles.append(RoleContract(
            id=str(row.get("id", "")),
            objective=str(row.get("objective", "")),
            responsibilities=_strings(row.get("responsibilities")),
            required_capabilities=frozenset(_strings(row.get("required_capabilities"))),
            required_skills=frozenset(_strings(row.get("required_skills"))),
            allowed_auth_kinds=_auth_kinds(row.get("allowed_auth_kinds")),
            preferred_providers=_strings(row.get("preferred_providers")),
            forbidden_providers=frozenset(_strings(row.get("forbidden_providers"))),
            preferred_executors=_strings(row.get("preferred_executors")),
            allowed_executors=frozenset(_strings(row.get("allowed_executors"))),
            forbidden_executors=frozenset(_strings(row.get("forbidden_executors"))),
            required_executor_labels=frozenset(_strings(row.get("required_executor_labels"))),
            permissions=frozenset(_strings(row.get("permissions")) or ("repo_read",)),
            acceptance_criteria=_strings(row.get("acceptance_criteria")),
            independent_from_roles=frozenset(_strings(row.get("independent_from_roles"))),
            min_agents=int(row.get("min_agents", 1)),
            max_agents=int(row.get("max_agents", 1)),
            metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
        ))

    graph = CampaignGraph()
    campaign_table = raw.get("campaign", {})
    if campaign_table is not None and not isinstance(campaign_table, dict):
        raise LegionError("[campaign] must be a table")
    nodes = campaign_table.get("nodes", []) if isinstance(campaign_table, dict) else []
    if nodes and (not isinstance(nodes, list) or not all(isinstance(row, dict) for row in nodes)):
        raise LegionError("[[campaign.nodes]] must be an array of tables")
    for row in nodes or []:
        graph.add(CampaignNode(
            id=str(row.get("id", "")),
            kind=StageKind(str(row.get("kind", "agent"))),
            depends_on=_strings(row.get("depends_on")),
            role=str(row["role"]) if row.get("role") is not None else None,
            executor=str(row["executor"]) if row.get("executor") is not None else None,
            objective=str(row.get("objective", "")),
            required_outputs=_strings(row.get("required_outputs")),
            human_approval=bool(row.get("human_approval", False)),
            metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
        ))
    graph.validate()
    registry.validate()
    for role in roles:
        role.validate()
    return LegionConfig(policy=policy, registry=registry, roles=roles, campaign=graph, raw=raw)


def load(path: Path) -> LegionConfig:
    return load_dict(toml_loads(path.read_text(encoding="utf-8")))
