"""Provider-agnostic Legion orchestration primitives.

This module deliberately separates provider, model, runtime, authentication,
executor, agent, role, and campaign topology.  The legacy council/competition
commands remain compatibility presets; new orchestration should build on these
primitives instead of branching on vendor names.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable


class LegionError(ValueError):
    """Raised when a Legion contract is internally inconsistent."""


class AuthKind(str, Enum):
    OAUTH = "oauth"
    API_KEY = "api_key"
    NATIVE = "native"
    NONE = "none"


class TeamPolicy(str, Enum):
    USER_DEFINED = "user_defined"
    COMMANDER_DEFINED = "commander_defined"
    HYBRID = "hybrid"


class StageKind(str, Enum):
    AGENT = "agent"
    REVIEW = "review"
    VALIDATION = "validation"
    SYNTHESIS = "synthesis"
    CHECKPOINT = "checkpoint"
    HUMAN_GATE = "human_gate"


@dataclass(frozen=True)
class AuthProfile:
    id: str
    kind: AuthKind
    provider: str
    source: str = "native"
    secret_ref: str | None = None
    email: str | None = None
    account_label: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise LegionError("auth profile id is required")
        if not self.provider.strip() and self.kind is not AuthKind.NONE:
            raise LegionError(f"auth profile {self.id!r} requires provider")
        if self.kind is AuthKind.API_KEY:
            if not self.secret_ref:
                raise LegionError(
                    f"API auth profile {self.id!r} must reference a secret source; inline keys are forbidden"
                )
            lowered = self.secret_ref.lower()
            if lowered.startswith(("sk-", "key-", "api-")):
                raise LegionError(
                    f"API auth profile {self.id!r} appears to contain a credential; use env/keyring/secret-manager reference"
                )


@dataclass(frozen=True)
class RuntimeAdapter:
    id: str
    provider: str
    transport: str
    command: tuple[str, ...] = ()
    login_command: tuple[str, ...] = ()
    auth_status_command: tuple[str, ...] = ()
    endpoint: str | None = None
    capabilities: frozenset[str] = frozenset()
    auth_kinds: frozenset[AuthKind] = frozenset({AuthKind.NATIVE})
    skill_roots: tuple[str, ...] = ()
    output_format: str = "text"
    prompt_transport: str = "argument"
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise LegionError("runtime id is required")
        if not self.provider.strip():
            raise LegionError(f"runtime {self.id!r} requires provider")
        if self.transport not in {"cli", "api", "local", "mcp", "custom"}:
            raise LegionError(f"runtime {self.id!r} has unsupported transport {self.transport!r}")
        if self.transport == "cli" and not self.command:
            raise LegionError(f"CLI runtime {self.id!r} requires a command template")
        if self.transport == "api" and not self.endpoint and not self.command:
            raise LegionError(f"API runtime {self.id!r} requires endpoint or command adapter")
        if not self.skill_roots:
            raise LegionError(
                f"runtime {self.id!r} must declare at least one skill root so every executor can access the reviewed Legion skill baseline"
            )


@dataclass
class ResourceBudget:
    subscription_remaining_percent: float | None = None
    reserve_percent: float = 25.0
    no_new_work_below_percent: float = 35.0
    spent_today_percent: float = 0.0
    spent_session_percent: float = 0.0
    daily_unattended_max_percent: float = 10.0
    session_checkpoint_percent: float = 4.0
    session_hard_stop_percent: float = 5.0
    api_soft_budget_usd: float | None = None
    api_hard_budget_usd: float | None = None
    api_spend_usd: float = 0.0
    max_parallel: int = 1
    cooldown_until: str | None = None
    enabled: bool = True

    def usable(self, *, estimated_api_cost_usd: float = 0.0, now: datetime | None = None) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if self.cooldown_until:
            try:
                cooldown = datetime.fromisoformat(self.cooldown_until.replace("Z", "+00:00"))
                current = now or datetime.now(timezone.utc)
                if cooldown.tzinfo is None:
                    cooldown = cooldown.replace(tzinfo=timezone.utc)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                if current < cooldown:
                    return False, f"cooldown active until {cooldown.isoformat()}"
            except ValueError:
                return False, "invalid cooldown timestamp"
        if self.spent_today_percent >= self.daily_unattended_max_percent:
            return False, "daily unattended allowance cap reached"
        if self.spent_session_percent >= self.session_hard_stop_percent:
            return False, "session allowance hard-stop reached"
        if self.spent_session_percent >= self.session_checkpoint_percent:
            return False, "session checkpoint boundary reached"
        if self.subscription_remaining_percent is not None:
            if self.subscription_remaining_percent <= self.no_new_work_below_percent:
                return False, "protected recovery reserve reached"
            if self.subscription_remaining_percent <= self.reserve_percent:
                return False, "subscription reserve reached"
        if self.api_hard_budget_usd is not None:
            if self.api_spend_usd + max(0.0, estimated_api_cost_usd) > self.api_hard_budget_usd:
                return False, "API hard budget would be exceeded"
        return True, "ok"


@dataclass(frozen=True)
class Executor:
    id: str
    provider: str
    model: str
    runtime: str
    auth_profile: str
    capabilities: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()
    skill_roots: tuple[str, ...] = ()
    priority: int = 100
    labels: frozenset[str] = frozenset()
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        for name, value in (
            ("id", self.id),
            ("provider", self.provider),
            ("model", self.model),
            ("runtime", self.runtime),
            ("auth_profile", self.auth_profile),
        ):
            if not str(value).strip():
                raise LegionError(f"executor {self.id!r} requires {name}")


@dataclass(frozen=True)
class RoleContract:
    id: str
    objective: str
    responsibilities: tuple[str, ...] = ()
    required_capabilities: frozenset[str] = frozenset()
    required_skills: frozenset[str] = frozenset()
    allowed_auth_kinds: frozenset[AuthKind] = frozenset(AuthKind)
    preferred_providers: tuple[str, ...] = ()
    forbidden_providers: frozenset[str] = frozenset()
    preferred_executors: tuple[str, ...] = ()
    allowed_executors: frozenset[str] = frozenset()
    forbidden_executors: frozenset[str] = frozenset()
    required_executor_labels: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset({"repo_read"})
    acceptance_criteria: tuple[str, ...] = ()
    independent_from_roles: frozenset[str] = frozenset()
    min_agents: int = 1
    max_agents: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise LegionError("role id is required")
        if not self.objective.strip():
            raise LegionError(f"role {self.id!r} requires objective")
        if self.min_agents < 0 or self.max_agents < 1 or self.min_agents > self.max_agents:
            raise LegionError(f"role {self.id!r} has invalid agent cardinality")


@dataclass(frozen=True)
class AgentAssignment:
    id: str
    role: str
    executor: str
    objective: str
    permissions: frozenset[str]
    work_packet: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignNode:
    id: str
    kind: StageKind
    depends_on: tuple[str, ...] = ()
    role: str | None = None
    executor: str | None = None
    objective: str = ""
    required_outputs: tuple[str, ...] = ()
    human_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id.strip():
            raise LegionError("campaign node id is required")
        if self.kind in {StageKind.AGENT, StageKind.REVIEW} and not self.role:
            raise LegionError(f"{self.kind.value} node {self.id!r} requires a role")
        if self.kind is StageKind.HUMAN_GATE and not self.human_approval:
            raise LegionError(f"human gate {self.id!r} must require explicit human approval")


@dataclass
class CampaignGraph:
    nodes: dict[str, CampaignNode] = field(default_factory=dict)

    def add(self, node: CampaignNode) -> None:
        node.validate()
        if node.id in self.nodes:
            raise LegionError(f"duplicate campaign node {node.id!r}")
        self.nodes[node.id] = node

    def validate(self) -> None:
        for node in self.nodes.values():
            node.validate()
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise LegionError(f"node {node.id!r} depends on unknown node {dep!r}")
                if dep == node.id:
                    raise LegionError(f"node {node.id!r} cannot depend on itself")
        self.topological_order()

    def topological_order(self) -> list[str]:
        incoming = {node_id: set(node.depends_on) for node_id, node in self.nodes.items()}
        ready = sorted(node_id for node_id, deps in incoming.items() if not deps)
        ordered: list[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for other in sorted(incoming):
                if node_id in incoming[other]:
                    incoming[other].remove(node_id)
                    if not incoming[other] and other not in ordered and other not in ready:
                        ready.append(other)
                        ready.sort()
        if len(ordered) != len(self.nodes):
            cycle = sorted(node_id for node_id, deps in incoming.items() if deps)
            raise LegionError(f"campaign graph contains dependency cycle involving: {', '.join(cycle)}")
        return ordered


@dataclass(frozen=True)
class RecruitmentRequest:
    requested_by: str
    role: RoleContract
    reason: str
    estimated_api_cost_usd: float = 0.0


class ExecutorRegistry:
    """Mutable executor pool with capability- and budget-aware selection."""

    def __init__(self) -> None:
        self.auth_profiles: dict[str, AuthProfile] = {}
        self.runtimes: dict[str, RuntimeAdapter] = {}
        self.executors: dict[str, Executor] = {}
        self.budgets: dict[str, ResourceBudget] = {}

    def register_auth(self, profile: AuthProfile) -> None:
        profile.validate()
        if profile.id in self.auth_profiles:
            raise LegionError(f"duplicate auth profile {profile.id!r}")
        self.auth_profiles[profile.id] = profile

    def register_runtime(self, runtime: RuntimeAdapter) -> None:
        runtime.validate()
        if runtime.id in self.runtimes:
            raise LegionError(f"duplicate runtime {runtime.id!r}")
        self.runtimes[runtime.id] = runtime

    def register_executor(self, executor: Executor, budget: ResourceBudget | None = None) -> None:
        executor.validate()
        if executor.id in self.executors:
            raise LegionError(f"duplicate executor {executor.id!r}")
        runtime = self.runtimes.get(executor.runtime)
        auth = self.auth_profiles.get(executor.auth_profile)
        if runtime is None:
            raise LegionError(f"executor {executor.id!r} references unknown runtime {executor.runtime!r}")
        if auth is None:
            raise LegionError(f"executor {executor.id!r} references unknown auth profile {executor.auth_profile!r}")
        if executor.provider != runtime.provider:
            raise LegionError(
                f"executor {executor.id!r} provider {executor.provider!r} does not match runtime provider {runtime.provider!r}"
            )
        if auth.kind not in runtime.auth_kinds:
            raise LegionError(
                f"runtime {runtime.id!r} does not support auth kind {auth.kind.value!r} required by executor {executor.id!r}"
            )
        if auth.provider not in {executor.provider, "*"} and auth.kind is not AuthKind.NONE:
            raise LegionError(
                f"executor {executor.id!r} provider does not match auth profile provider {auth.provider!r}"
            )
        self.executors[executor.id] = executor
        self.budgets[executor.id] = budget or ResourceBudget(enabled=executor.enabled)

    def validate(self) -> None:
        for executor_id, executor in self.executors.items():
            if executor_id not in self.budgets:
                raise LegionError(f"executor {executor_id!r} has no resource budget")
            executor.validate()

    def eligible(
        self,
        role: RoleContract,
        *,
        estimated_api_cost_usd: float = 0.0,
        exclude_executors: Iterable[str] = (),
        exclude_providers: Iterable[str] = (),
    ) -> list[tuple[Executor, str]]:
        role.validate()
        excluded = set(exclude_executors) | set(role.forbidden_executors)
        excluded_providers = set(exclude_providers) | set(role.forbidden_providers)
        rows: list[tuple[int, str, Executor, str]] = []
        for executor in self.executors.values():
            if not executor.enabled or executor.id in excluded or executor.provider in excluded_providers:
                continue
            if role.allowed_executors and executor.id not in role.allowed_executors:
                continue
            if role.required_executor_labels and not role.required_executor_labels.issubset(executor.labels):
                continue
            runtime = self.runtimes[executor.runtime]
            auth = self.auth_profiles[executor.auth_profile]
            budget = self.budgets[executor.id]
            usable, reason = budget.usable(estimated_api_cost_usd=estimated_api_cost_usd if auth.kind is AuthKind.API_KEY else 0.0)
            if not usable:
                continue
            capabilities = set(executor.capabilities) | set(runtime.capabilities)
            if not role.required_capabilities.issubset(capabilities):
                continue
            if not role.required_skills.issubset(executor.skills):
                continue
            if auth.kind not in role.allowed_auth_kinds:
                continue
            executor_bonus = 0
            if role.preferred_executors:
                try:
                    executor_bonus = 10_000 - role.preferred_executors.index(executor.id) * 500
                except ValueError:
                    executor_bonus = 0
            provider_bonus = 0
            if role.preferred_providers:
                try:
                    provider_bonus = 1000 - role.preferred_providers.index(executor.provider) * 100
                except ValueError:
                    provider_bonus = 0
            score = executor_bonus + provider_bonus - executor.priority
            if (
                auth.kind is AuthKind.API_KEY
                and budget.api_soft_budget_usd is not None
                and budget.api_spend_usd + max(0.0, estimated_api_cost_usd) >= budget.api_soft_budget_usd
            ):
                # Soft budget remains eligible for overflow, but strongly prefer
                # other usable executors first. Hard budget is enforced above.
                score -= 10_000
                reason = "API soft budget reached; overflow only"
            rows.append((score, executor.id, executor, reason))
        rows.sort(key=lambda row: (-row[0], row[1]))
        return [(executor, reason) for _, _, executor, reason in rows]

    def select(
        self,
        role: RoleContract,
        *,
        estimated_api_cost_usd: float = 0.0,
        exclude_executors: Iterable[str] = (),
        exclude_providers: Iterable[str] = (),
    ) -> Executor:
        candidates = self.eligible(
            role,
            estimated_api_cost_usd=estimated_api_cost_usd,
            exclude_executors=exclude_executors,
            exclude_providers=exclude_providers,
        )
        if not candidates:
            raise LegionError(f"no eligible executor for role {role.id!r}")
        return candidates[0][0]


class TeamPlanner:
    """Deterministic team planner that supports user, Commander, and hybrid roles.

    The planner's role synthesis is intentionally small and auditable.  A model or
    operator may supply richer arbitrary RoleContracts; the scheduler treats them
    identically.  This avoids baking product-specific role names into Commander.
    """

    def __init__(self, registry: ExecutorRegistry, policy: TeamPolicy = TeamPolicy.HYBRID) -> None:
        self.registry = registry
        self.policy = policy

    @staticmethod
    def synthesize_roles(objective: str, *, risk: str = "normal") -> list[RoleContract]:
        text = objective.casefold()
        roles: list[RoleContract] = [
            RoleContract(
                id="builder",
                objective="Produce the smallest correct implementation or artifact that satisfies the goal.",
                required_capabilities=frozenset({"repo_read", "repo_write"}),
                permissions=frozenset({"repo_read", "repo_write", "shell"}),
                acceptance_criteria=("bounded scope", "focused validation"),
            ),
            RoleContract(
                id="independent-reviewer",
                objective="Challenge the builder result independently and report evidence-backed defects or PASS.",
                required_capabilities=frozenset({"repo_read"}),
                permissions=frozenset({"repo_read", "shell"}),
                independent_from_roles=frozenset({"builder"}),
                acceptance_criteria=("independent review", "explicit verdict"),
            ),
        ]
        if any(word in text for word in ("research", "paper", "state of the art", "literature")):
            roles.insert(0, RoleContract(
                id="research-specialist",
                objective="Establish source-grounded external and repository-local facts needed by the goal.",
                required_capabilities=frozenset({"repo_read"}),
                permissions=frozenset({"repo_read", "web"}),
            ))
        if any(word in text for word in ("architecture", "design", "migration", "cross-module", "cross module")):
            roles.insert(0, RoleContract(
                id="architecture-specialist",
                objective="Map architectural seams, constraints, and integration risks before implementation.",
                required_capabilities=frozenset({"repo_read"}),
                permissions=frozenset({"repo_read", "shell"}),
            ))
        if risk == "high":
            roles.insert(1, RoleContract(
                id="independent-candidate",
                objective="Produce an independent candidate solution without relying on the primary builder implementation.",
                required_capabilities=frozenset({"repo_read", "repo_write"}),
                permissions=frozenset({"repo_read", "repo_write", "shell"}),
                min_agents=2,
                max_agents=2,
            ))
        if any(word in text for word in ("security", "crypto", "auth", "credential", "trust")) or risk == "high":
            roles.append(RoleContract(
                id="adversarial-assurance",
                objective="Perform adversarial security, trust-boundary, and failure-mode review.",
                required_capabilities=frozenset({"repo_read"}),
                permissions=frozenset({"repo_read", "shell"}),
                independent_from_roles=frozenset({"builder", "independent-candidate"}),
            ))
        if any(word in text for word in ("test", "validation", "coverage", "regression", "qualification")):
            roles.append(RoleContract(
                id="validation-specialist",
                objective="Design and execute the cheapest discriminating validation needed for the requested change.",
                required_capabilities=frozenset({"repo_read", "shell"}),
                permissions=frozenset({"repo_read", "shell"}),
            ))
        if any(word in text for word in ("performance", "benchmark", "scale", "latency", "throughput")):
            roles.append(RoleContract(
                id="performance-verifier",
                objective="Measure and challenge performance claims with reproducible evidence.",
                required_capabilities=frozenset({"repo_read", "shell"}),
                permissions=frozenset({"repo_read", "shell"}),
            ))
        return roles

    def plan(
        self,
        objective: str,
        *,
        user_roles: Iterable[RoleContract] = (),
        risk: str = "normal",
    ) -> tuple[list[RoleContract], list[AgentAssignment]]:
        supplied = list(user_roles)
        if self.policy is TeamPolicy.USER_DEFINED:
            roles = supplied
        elif self.policy is TeamPolicy.COMMANDER_DEFINED:
            roles = self.synthesize_roles(objective, risk=risk)
        else:
            role_map = {role.id: role for role in self.synthesize_roles(objective, risk=risk)}
            for role in supplied:
                role_map[role.id] = role
            roles = list(role_map.values())
        if not roles:
            raise LegionError("team plan has no roles")

        assignments: list[AgentAssignment] = []
        role_executor: dict[str, str] = {}
        role_provider: dict[str, str] = {}
        for role in roles:
            role.validate()
            exclude_executors: set[str] = set()
            exclude_providers: set[str] = set()
            for independent_role in role.independent_from_roles:
                if independent_role in role_executor:
                    exclude_executors.add(role_executor[independent_role])
                    # Prefer provider diversity for independent review when possible;
                    # if no alternative provider exists, retry with executor-only separation.
                    exclude_providers.add(role_provider[independent_role])
            candidates = self.registry.eligible(
                role,
                exclude_executors=exclude_executors,
                exclude_providers=exclude_providers,
            )
            if not candidates and exclude_providers:
                candidates = self.registry.eligible(role, exclude_executors=exclude_executors)
            if not candidates:
                raise LegionError(f"no eligible executor for role {role.id!r}")
            count = min(max(role.min_agents, 1), role.max_agents, len(candidates))
            for index, (executor, _) in enumerate(candidates[:count], start=1):
                assignment_id = role.id if count == 1 else f"{role.id}-{index}"
                assignments.append(AgentAssignment(
                    id=assignment_id,
                    role=role.id,
                    executor=executor.id,
                    objective=role.objective,
                    permissions=role.permissions,
                ))
                role_executor.setdefault(role.id, executor.id)
                role_provider.setdefault(role.id, executor.provider)
        return roles, assignments

    def approve_recruitment(self, request: RecruitmentRequest, existing_roles: Iterable[str]) -> AgentAssignment:
        if request.role.id in set(existing_roles):
            raise LegionError(f"recruitment request duplicates existing role {request.role.id!r}")
        executor = self.registry.select(request.role, estimated_api_cost_usd=request.estimated_api_cost_usd)
        return AgentAssignment(
            id=f"recruited-{request.role.id}",
            role=request.role.id,
            executor=executor.id,
            objective=request.role.objective,
            permissions=request.role.permissions,
            metadata={"requested_by": request.requested_by, "reason": request.reason},
        )
