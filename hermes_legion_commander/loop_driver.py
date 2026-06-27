"""Loop driver: the scheduling layer that turns a one-shot campaign into a loop.

This is loop engineering's defining move -- scheduling on the harness -- with the
two guards the practice depends on:

* run-until-met: each iteration first asks a fresh model whether the stop
  condition holds (the /goal check); if it does, the loop stops instead of doing
  redundant work. "Run until met" replaces a fixed version range.
* a budget circuit-breaker: hard caps (max turns, max consecutive failures, and a
  cumulative shadow-cost ceiling) convert an open-ended overnight risk into a
  bounded one, so a single bug spinning idle cannot burn an entire quota.

The control logic here is pure and dependency-injected: the goal check, the turn
of work, the per-turn cost read, the state persistence, and sleeping are all
passed in as callables, so the loop's decisions can be unit-tested without a live
model. Each mode (council/checkpoint) supplies the real callables.

Two scheduling shapes share this core:

* local: the process loops in-place, sleeping ``interval_seconds`` between turns.
  Buys frequency and local-file access at the cost of keeping the machine on.
* cloud/CI: ``single_turn`` runs exactly one turn per invocation and returns; a
  cron/schedule trigger provides the repetition, so the loop runs with the
  machine off. Buys true autonomy at the cost of a coarser interval and a fresh
  checkout each run -- which is why loop state is persisted and resumed.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable


@dataclasses.dataclass(frozen=True)
class LoopLimits:
    """Hard caps that bound an otherwise open-ended loop (the circuit-breaker)."""

    max_turns: int = 10
    max_consecutive_failures: int = 3
    max_cost_usd: float | None = None  # cumulative shadow-cost ceiling (estimate-based)
    interval_seconds: int = 3600

    def validate(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        if self.max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be >= 1")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be > 0 when set")
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0")


@dataclasses.dataclass
class LoopState:
    """Durable loop progress, resumed across cloud single-turn invocations."""

    run_id: str
    condition: str
    turn: int = 0
    consecutive_failures: int = 0
    cumulative_cost_usd: float = 0.0
    condition_met: bool = False
    stopped: bool = False
    stopped_reason: str | None = None
    history: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopState":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def stop_decision(state: LoopState, limits: LoopLimits, condition_met: bool) -> tuple[bool, str | None]:
    """Decide whether to stop before doing another turn of work.

    Order matters: a met condition stops first (no redundant work), then the
    circuit-breaker caps. Pure and total -- this is the function under test.
    """
    if condition_met:
        return True, "stop-condition-met"
    if state.turn >= limits.max_turns:
        return True, "max-turns-reached"
    if state.consecutive_failures >= limits.max_consecutive_failures:
        return True, "max-consecutive-failures"
    if limits.max_cost_usd is not None and state.cumulative_cost_usd >= limits.max_cost_usd:
        return True, "cost-budget-exceeded"
    return False, None


def load_or_init_state(path: Path, run_id: str, condition: str) -> LoopState:
    """Resume an existing loop (cloud single-turn) or start a fresh one."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("run_id") == run_id:
            state = LoopState.from_dict(data)
            # A new invocation of a previously-stopped loop is a no-op caller's
            # concern; we surface the stopped state as-is.
            state.condition = condition
            return state
    return LoopState(run_id=run_id, condition=condition)


def run_loop(
    state: LoopState,
    limits: LoopLimits,
    *,
    check_goal: Callable[[], dict[str, Any]],
    run_turn: Callable[[], dict[str, Any]],
    read_turn_cost: Callable[[dict[str, Any]], float],
    persist: Callable[[LoopState], None],
    sleep_fn: Callable[[int], None],
    single_turn: bool = False,
    log: Callable[[str], None] = lambda _m: None,
) -> LoopState:
    """Drive turns until the stop condition is met or the circuit-breaker trips.

    ``check_goal`` returns a verdict dict with a ``met`` key. ``run_turn`` does one
    turn of work (a campaign) and returns a dict; ``passed`` defaults to True when
    absent. ``read_turn_cost`` extracts the turn's shadow cost. ``persist`` writes
    state after every transition so a crash or a cloud invocation can resume.
    """
    limits.validate()
    if state.stopped:
        log(f"loop already stopped: {state.stopped_reason}")
        return state

    while True:
        verdict = check_goal()
        met = bool(verdict.get("met"))
        state.condition_met = met

        stop, reason = stop_decision(state, limits, met)
        if stop:
            state.stopped = True
            state.stopped_reason = reason
            state.history.append({"turn": state.turn, "event": "stop", "reason": reason, "goal_met": met})
            persist(state)
            log(f"loop stopped after turn {state.turn}: {reason}")
            return state

        state.turn += 1
        log(f"starting turn {state.turn}/{limits.max_turns}")
        try:
            result = run_turn()
            passed = bool(result.get("passed", True))
            cost = float(read_turn_cost(result) or 0.0)
            state.cumulative_cost_usd = round(state.cumulative_cost_usd + cost, 6)
            state.consecutive_failures = 0 if passed else state.consecutive_failures + 1
            state.history.append({
                "turn": state.turn,
                "event": "turn",
                "passed": passed,
                "cost_usd": cost,
                "cumulative_cost_usd": state.cumulative_cost_usd,
                "run_dir": str(result.get("run_dir") or ""),
            })
        except Exception as exc:  # a failed turn must not kill the loop; it counts as a failure
            state.consecutive_failures += 1
            state.history.append({"turn": state.turn, "event": "turn-error", "error": str(exc)})
            log(f"turn {state.turn} errored: {exc}")
        persist(state)

        if single_turn:
            log("single-turn invocation complete; scheduler provides the next turn")
            return state
        sleep_fn(limits.interval_seconds)


def cloud_workflow_yaml(
    *,
    cron: str,
    config_path: str,
    condition: str,
    from_version: int,
    to_version: int,
    max_turns: int,
    workflow_name: str = "hermes-legion-loop",
) -> str:
    """Generate a GitHub Actions workflow that runs one loop turn per schedule tick.

    The cron trigger is the scheduler (machine-off autonomy); each run executes a
    single turn and the committed loop state carries progress to the next run.
    """
    safe_condition = condition.replace('"', '\\"')
    return f"""# Generated by hermes-legion-commander: one loop turn per scheduled run.
# Machine-off autonomy: the cron schedule is the scheduler. Loop state is committed
# back so each run resumes where the last left off. Pull requests are opened, never
# auto-merged -- the human review point stays installed.
name: {workflow_name}

on:
  schedule:
    - cron: "{cron}"
  workflow_dispatch: {{}}

permissions:
  contents: write
  pull-requests: write

concurrency:
  group: {workflow_name}
  cancel-in-progress: false

jobs:
  turn:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install hermes-legion-commander
        run: pip install --break-system-packages hermes-legion-commander
      - name: Run one loop turn
        env:
          # Provide whatever credentials your configured worker CLIs require, e.g.
          # ANTHROPIC_API_KEY / OPENAI_API_KEY, as repository secrets.
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          OPENAI_API_KEY: ${{{{ secrets.OPENAI_API_KEY }}}}
        run: |
          hermes-legion-commander council \\
            --config {config_path} \\
            loop \\
            --condition "{safe_condition}" \\
            --from-version {from_version} \\
            --to-version {to_version} \\
            --max-turns {max_turns} \\
            --single-turn
      - name: Persist loop state and any opened proposals
        run: |
          git config user.name "hermes-legion-loop"
          git config user.email "hermes-legion-loop@users.noreply.github.com"
          git add -A
          git diff --cached --quiet || git commit -m "loop: turn state $(date -u +%FT%TZ)"
          git push || true
"""
