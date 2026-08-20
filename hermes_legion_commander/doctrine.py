"""Repository-agnostic development doctrine for sustained multi-agent work.

The defaults are derived from operational lessons learned while developing
HIVEAS, but this module contains no HIVEAS-specific paths, roles, or domain
rules. Target-repository instructions may add stricter requirements; they must
not silently weaken explicit human gates or configured hard resource limits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Phase(str, Enum):
    DISCOVERY = "discovery"
    RESEARCH = "research"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    QUALIFICATION = "qualification"
    EVIDENCE = "evidence"
    RECORDING = "recording"
    INTEGRATION = "integration"


VALIDATION_LADDER = (
    "static_or_source_probe",
    "focused_regression",
    "subsystem_smoke_or_integration",
    "broader_suite",
    "long_run_benchmark_simulation_or_evidence",
)

PROTECTED_ACTIONS = frozenset({
    "merge",
    "push",
    "deploy",
    "tag",
    "publish",
    "release",
    "credential_change",
    "hardware_operation",
    "live_actuation",
})


@dataclass(frozen=True)
class ResourceDoctrine:
    reserve_percent: float = 25.0
    no_new_unattended_work_below_percent: float = 35.0
    invocation_checkpoint_percent: float = 4.0
    invocation_hard_stop_percent: float = 5.0
    daily_unattended_max_percent: float = 10.0

    def can_start(self, *, remaining_percent: float | None, spent_today_percent: float = 0.0) -> tuple[bool, str]:
        if spent_today_percent >= self.daily_unattended_max_percent:
            return False, "daily unattended allowance cap reached"
        if remaining_percent is None:
            return True, "allowance telemetry unavailable; fallback stage/context limits apply"
        if remaining_percent <= self.no_new_unattended_work_below_percent:
            return False, "remaining allowance is in protected recovery reserve"
        if remaining_percent <= self.reserve_percent:
            return False, "configured reserve reached"
        return True, "ok"

    def session_boundary(self, consumed_percent: float | None) -> str:
        if consumed_percent is None:
            return "UNKNOWN"
        if consumed_percent >= self.invocation_hard_stop_percent:
            return "HARD_STOP"
        if consumed_percent >= self.invocation_checkpoint_percent:
            return "CHECKPOINT"
        return "CONTINUE"


@dataclass(frozen=True)
class ContextDoctrine:
    checkpoint_percent: float = 20.0
    hard_boundary_percent: float = 25.0
    automatic_compaction_is_stop: bool = True

    def boundary(self, *, used_percent: float | None, compacted: bool = False) -> str:
        if compacted and self.automatic_compaction_is_stop:
            return "HARD_STOP"
        if used_percent is None:
            return "UNKNOWN"
        if used_percent >= self.hard_boundary_percent:
            return "HARD_STOP"
        if used_percent >= self.checkpoint_percent:
            return "CHECKPOINT"
        return "CONTINUE"


@dataclass(frozen=True)
class DiagnosisDoctrine:
    max_failed_material_attempts: int = 2
    max_long_runs_per_stage: int = 2
    one_major_requirement_per_stage: bool = True

    def diagnosis_boundary(self, failed_attempts: int) -> str:
        return "CHECKPOINT" if failed_attempts >= self.max_failed_material_attempts else "CONTINUE"

    def long_run_boundary(self, long_runs: int) -> str:
        return "CHECKPOINT" if long_runs >= self.max_long_runs_per_stage else "CONTINUE"


@dataclass(frozen=True)
class DevelopmentDoctrine:
    resources: ResourceDoctrine = field(default_factory=ResourceDoctrine)
    context: ContextDoctrine = field(default_factory=ContextDoctrine)
    diagnosis: DiagnosisDoctrine = field(default_factory=DiagnosisDoctrine)
    validation_ladder: tuple[str, ...] = VALIDATION_LADDER
    protected_actions: frozenset[str] = PROTECTED_ACTIONS
    separate_expensive_phases: bool = True
    independent_review_required_for_completion: bool = True
    builder_self_report_is_provisional: bool = True
    repository_instructions_are_authoritative: bool = True

    def requires_human(self, actions: Iterable[str]) -> set[str]:
        return set(actions) & set(self.protected_actions)

    def validate_phase_transition(self, current: Phase, next_phase: Phase, *, checkpointed: bool) -> tuple[bool, str]:
        expensive = {Phase.IMPLEMENTATION, Phase.REVIEW, Phase.QUALIFICATION, Phase.EVIDENCE, Phase.RECORDING}
        if self.separate_expensive_phases and current in expensive and next_phase in expensive and current != next_phase and not checkpointed:
            return False, "expensive phase transition requires a persisted checkpoint/fresh stage"
        return True, "ok"


DEFAULT_DOCTRINE = DevelopmentDoctrine()
