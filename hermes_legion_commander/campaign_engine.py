"""Generic campaign DAG planning and execution.

The engine is deliberately conservative: it never merges, pushes, deploys,
publishes, releases, changes credentials, or crosses a human gate.  Campaign
nodes may run arbitrary user-defined roles on arbitrary registered executors.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .executor_runtime import ExecutionResult, invoke_executor
from .skill_profile import select_stage_skills
from .legion import (
    AgentAssignment,
    CampaignGraph,
    CampaignNode,
    LegionError,
    RoleContract,
    StageKind,
    TeamPlanner,
)

UTC = dt.timezone.utc


@dataclass
class NodeState:
    id: str
    status: str = "PENDING"
    executor: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    artifact: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CampaignState:
    objective: str
    status: str = "PENDING"
    nodes: dict[str, NodeState] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: dt.datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: dt.datetime.now(UTC).isoformat())

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = dt.datetime.now(UTC).isoformat()
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def graph_from_assignments(assignments: Iterable[AgentAssignment], roles: dict[str, RoleContract]) -> CampaignGraph:
    """Build a safe default fan-out/fan-in graph from arbitrary assignments.

    Research-like roles run first, builders next, independent reviewers after the
    builders, then a final checkpoint. Operators can replace this graph entirely
    with [[campaign.nodes]] in TOML.
    """
    assignments = list(assignments)
    graph = CampaignGraph()
    research_ids: list[str] = []
    build_ids: list[str] = []
    review_ids: list[str] = []
    for assignment in assignments:
        role = roles[assignment.role]
        lowered = role.id.casefold() + " " + role.objective.casefold()
        if any(word in lowered for word in ("review", "verify", "assurance", "audit", "judge")):
            bucket = review_ids
            kind = StageKind.REVIEW
        elif any(word in lowered for word in ("research", "literature", "investigat")):
            bucket = research_ids
            kind = StageKind.AGENT
        else:
            bucket = build_ids
            kind = StageKind.AGENT
        node_id = f"agent:{assignment.id}"
        graph.add(CampaignNode(
            id=node_id,
            kind=kind,
            role=assignment.role,
            executor=assignment.executor,
            objective=assignment.objective,
            depends_on=(),
            required_outputs=("stage-result.md",),
        ))
        bucket.append(node_id)

    # Rebuild nodes with dependency seams; dataclasses are frozen.
    rewritten: dict[str, CampaignNode] = {}
    for node_id, node in graph.nodes.items():
        if node_id in build_ids and research_ids:
            deps = tuple(research_ids)
        elif node_id in review_ids:
            deps = tuple(build_ids or research_ids)
        else:
            deps = node.depends_on
        rewritten[node_id] = CampaignNode(
            id=node.id,
            kind=node.kind,
            role=node.role,
            executor=node.executor,
            objective=node.objective,
            depends_on=deps,
            required_outputs=node.required_outputs,
            human_approval=node.human_approval,
            metadata=node.metadata,
        )
    graph.nodes = rewritten
    terminal_deps = tuple(review_ids or build_ids or research_ids)
    graph.add(CampaignNode(
        id="checkpoint:final",
        kind=StageKind.CHECKPOINT,
        depends_on=terminal_deps,
        objective="Persist the campaign frontier and present evidence for human-controlled next action.",
    ))
    graph.validate()
    return graph


class CampaignEngine:
    def __init__(
        self,
        *,
        objective: str,
        planner: TeamPlanner,
        roles: Iterable[RoleContract],
        graph: CampaignGraph,
        repo: Path,
        state_dir: Path,
    ) -> None:
        self.objective = objective
        self.planner = planner
        self.roles = {role.id: role for role in roles}
        self.graph = graph
        self.repo = repo.resolve()
        self.state_dir = state_dir.resolve()
        self.graph.validate()

    def _assignment_for_node(self, node: CampaignNode) -> AgentAssignment:
        if not node.role:
            raise LegionError(f"node {node.id!r} has no role")
        role = self.roles.get(node.role)
        if role is None:
            raise LegionError(f"node {node.id!r} references unknown role {node.role!r}")
        if node.executor:
            if node.executor not in self.planner.registry.executors:
                raise LegionError(f"node {node.id!r} references unknown executor {node.executor!r}")
            executor = node.executor
        else:
            executor = self.planner.registry.select(role).id
        return AgentAssignment(
            id=node.id,
            role=role.id,
            executor=executor,
            objective=node.objective or role.objective,
            permissions=role.permissions,
        )

    def plan(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for node_id in self.graph.topological_order():
            node = self.graph.nodes[node_id]
            executor = node.executor
            if node.kind in {StageKind.AGENT, StageKind.REVIEW} and executor is None:
                executor = self._assignment_for_node(node).executor
            rows.append({
                "id": node.id,
                "kind": node.kind.value,
                "role": node.role,
                "executor": executor,
                "depends_on": list(node.depends_on),
                "human_approval": node.human_approval,
                "objective": node.objective,
            })
        return {
            "objective": self.objective,
            "repository": str(self.repo),
            "team_policy": self.planner.policy.value,
            "executor_count": len(self.planner.registry.executors),
            "role_count": len(self.roles),
            "nodes": rows,
            "safety": {
                "automatic_merge": False,
                "automatic_push": False,
                "automatic_deploy": False,
                "automatic_release": False,
                "human_gates_are_sovereign": True,
            },
        }

    def run(self, *, timeout: int = 900) -> CampaignState:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        artifacts = self.state_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        state_path = self.state_dir / "campaign-state.json"
        state = CampaignState(
            objective=self.objective,
            status="RUNNING",
            nodes={node_id: NodeState(id=node_id) for node_id in self.graph.nodes},
        )
        state.write(state_path)
        failed = False
        for node_id in self.graph.topological_order():
            node = self.graph.nodes[node_id]
            node_state = state.nodes[node_id]
            blockers = [dep for dep in node.depends_on if state.nodes[dep].status != "PASS"]
            if blockers:
                node_state.status = "BLOCKED"
                node_state.reason = "dependency not PASS: " + ", ".join(blockers)
                failed = True
                state.write(state_path)
                continue
            if node.kind is StageKind.HUMAN_GATE:
                node_state.status = "NEEDS_HUMAN"
                node_state.reason = "explicit human approval required"
                state.status = "NEEDS_HUMAN"
                state.write(state_path)
                return state
            if node.kind is StageKind.CHECKPOINT:
                node_state.status = "PASS"
                node_state.completed_at = dt.datetime.now(UTC).isoformat()
                state.write(state_path)
                continue
            if node.kind in {StageKind.VALIDATION, StageKind.SYNTHESIS} and not node.role:
                # Pure orchestration markers are recorded rather than guessed.
                node_state.status = "NEEDS_HUMAN" if node.human_approval else "PASS"
                node_state.reason = "orchestration marker; no executor role bound"
                node_state.completed_at = dt.datetime.now(UTC).isoformat()
                if node_state.status == "NEEDS_HUMAN":
                    state.status = "NEEDS_HUMAN"
                    state.write(state_path)
                    return state
                state.write(state_path)
                continue

            assignment = self._assignment_for_node(node)
            role = self.roles[assignment.role]
            node_state.executor = assignment.executor
            node_state.status = "RUNNING"
            node_state.started_at = dt.datetime.now(UTC).isoformat()
            state.write(state_path)
            dependency_artifacts = [state.nodes[dep].artifact for dep in node.depends_on if state.nodes[dep].artifact]
            prompt = self._stage_prompt(node, role, dependency_artifacts)
            try:
                active_skills = select_stage_skills(
                    f"{role.id} {role.objective} {node.objective}",
                    role.required_skills,
                    limit=3,
                )
                result = invoke_executor(
                    self.planner.registry,
                    assignment.executor,
                    prompt,
                    cwd=self.repo,
                    timeout=timeout,
                    active_skills=active_skills,
                )
                artifact = artifacts / (node.id.replace("/", "-").replace(":", "-") + ".md")
                artifact.write_text(result.output.rstrip() + "\n", encoding="utf-8")
                verdict = self._explicit_verdict(result.output)
                node_state.status = verdict
                node_state.artifact = str(artifact.relative_to(self.state_dir))
                node_state.metadata = self._result_metadata(result)
                node_state.metadata["active_skills"] = list(active_skills)
                if verdict == "BLOCKED":
                    node_state.reason = "executor returned explicit BLOCKED verdict"
                    failed = True
                elif verdict == "NEEDS_HUMAN":
                    node_state.reason = "executor returned explicit NEEDS_HUMAN verdict"
                    state.status = "NEEDS_HUMAN"
                    node_state.completed_at = dt.datetime.now(UTC).isoformat()
                    state.write(state_path)
                    return state
            except Exception as exc:  # execution boundary; persist exact blocker
                node_state.status = "BLOCKED"
                node_state.reason = str(exc)
                failed = True
            node_state.completed_at = dt.datetime.now(UTC).isoformat()
            state.write(state_path)
        state.status = "BLOCKED" if failed else "PASS"
        state.write(state_path)
        return state

    def _stage_prompt(self, node: CampaignNode, role: RoleContract, dependency_artifacts: list[str | None]) -> str:
        return "\n".join([
            "# HERMES LEGION COMMANDER ROLE CONTRACT",
            f"Campaign objective: {self.objective}",
            f"Stage: {node.id}",
            f"Role: {role.id}",
            f"Role objective: {role.objective}",
            f"Permissions: {', '.join(sorted(role.permissions))}",
            f"Responsibilities: {'; '.join(role.responsibilities) if role.responsibilities else 'bounded to role objective'}",
            f"Acceptance criteria: {'; '.join(role.acceptance_criteria) if role.acceptance_criteria else 'evidence-backed completion'}",
            f"Dependency artifacts: {', '.join(str(x) for x in dependency_artifacts) if dependency_artifacts else 'none'}",
            "Repository-local instructions and safety policy remain authoritative.",
            "Do not merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.",
            "Return an explicit PASS, BLOCKED, or NEEDS_HUMAN verdict with evidence.",
            "",
            "# CURRENT STAGE OBJECTIVE",
            node.objective or role.objective,
        ])

    @staticmethod
    def _explicit_verdict(output: str) -> str:
        """Normalize the stage's explicit semantic verdict; do not equate exit 0 with PASS."""
        upper = output.upper()
        for verdict in ("NEEDS_HUMAN", "BLOCKED", "PASS"):
            if re.search(rf"(?m)^\s*(?:STATUS|VERDICT)?\s*[:=-]?\s*{verdict}\b", upper):
                return verdict
        # Missing semantic verdict is a contract failure, not implicit success.
        return "BLOCKED"

    @staticmethod
    def _result_metadata(result: ExecutionResult) -> dict[str, Any]:
        return {
            "provider": result.provider,
            "model": result.model,
            "runtime": result.runtime,
            "usage": result.usage,
            "transport": result.metadata.get("transport"),
        }
