"""Tests for the loop driver: stop decisions, circuit-breaker caps, run-until-met,
single-turn/cloud resume, and the workflow generator. All pure/injected -- no CLI."""
import json
from pathlib import Path

import pytest

from hermes_legion_commander import loop_driver as ld


def _runner(goal_sequence, turn_results=None, costs=None):
    """Build injected callables that return scripted goal verdicts/turn results."""
    goals = list(goal_sequence)
    turns = list(turn_results or [])
    cost_list = list(costs or [])
    calls = {"goal": 0, "turn": 0, "sleeps": 0}

    def check_goal():
        i = min(calls["goal"], len(goals) - 1)
        calls["goal"] += 1
        return goals[i]

    def run_turn():
        r = turns[calls["turn"]] if calls["turn"] < len(turns) else {"passed": True}
        calls["turn"] += 1
        return r

    def read_turn_cost(result):
        if cost_list:
            idx = min(calls["turn"] - 1, len(cost_list) - 1)
            return cost_list[idx]
        return float(result.get("cost", 0.0))

    def sleep_fn(_s):
        calls["sleeps"] += 1

    return check_goal, run_turn, read_turn_cost, sleep_fn, calls


def test_stop_decision_condition_met_first():
    s = ld.LoopState(run_id="r", condition="c", turn=0)
    assert ld.stop_decision(s, ld.LoopLimits(), True) == (True, "stop-condition-met")


def test_stop_decision_max_turns():
    s = ld.LoopState(run_id="r", condition="c", turn=5)
    assert ld.stop_decision(s, ld.LoopLimits(max_turns=5), False) == (True, "max-turns-reached")


def test_stop_decision_max_consecutive_failures():
    s = ld.LoopState(run_id="r", condition="c", turn=2, consecutive_failures=3)
    assert ld.stop_decision(s, ld.LoopLimits(max_consecutive_failures=3), False) == (True, "max-consecutive-failures")


def test_stop_decision_cost_budget():
    s = ld.LoopState(run_id="r", condition="c", turn=1, cumulative_cost_usd=12.0)
    assert ld.stop_decision(s, ld.LoopLimits(max_cost_usd=10.0), False) == (True, "cost-budget-exceeded")


def test_stop_decision_continue():
    s = ld.LoopState(run_id="r", condition="c", turn=1, consecutive_failures=0, cumulative_cost_usd=1.0)
    assert ld.stop_decision(s, ld.LoopLimits(max_turns=10, max_cost_usd=100.0), False) == (False, None)


def test_limits_validation():
    with pytest.raises(ValueError):
        ld.LoopLimits(max_turns=0).validate()
    with pytest.raises(ValueError):
        ld.LoopLimits(max_consecutive_failures=0).validate()
    with pytest.raises(ValueError):
        ld.LoopLimits(max_cost_usd=0).validate()


def test_run_loop_stops_immediately_when_goal_already_met():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner([{"met": True}])
    final = ld.run_loop(
        state, ld.LoopLimits(),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    assert final.stopped and final.stopped_reason == "stop-condition-met"
    assert final.turn == 0  # no work done
    assert calls["turn"] == 0


def test_run_loop_runs_until_met():
    # Not met for two turns, then met on the third goal check.
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner(
        [{"met": False}, {"met": False}, {"met": True}]
    )
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    assert final.stopped_reason == "stop-condition-met"
    assert final.turn == 2  # two turns of work before the goal was met
    assert calls["sleeps"] == 2


def test_run_loop_max_turns_circuit_breaker():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner([{"met": False}])
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=3),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    assert final.stopped_reason == "max-turns-reached"
    assert final.turn == 3


def test_run_loop_consecutive_failures_breaker():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner(
        [{"met": False}], turn_results=[{"passed": False}] * 5
    )
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10, max_consecutive_failures=2),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    assert final.stopped_reason == "max-consecutive-failures"
    assert final.consecutive_failures == 2


def test_run_loop_failure_then_success_resets_streak():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner(
        [{"met": False}, {"met": False}, {"met": True}],
        turn_results=[{"passed": False}, {"passed": True}],
    )
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10, max_consecutive_failures=2),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    # fail then succeed resets the streak, so the breaker never trips; goal met ends it.
    assert final.stopped_reason == "stop-condition-met"
    assert final.consecutive_failures == 0


def test_run_loop_cost_budget_breaker_accumulates():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner(
        [{"met": False}], turn_results=[{"passed": True}] * 5, costs=[4.0, 4.0, 4.0]
    )
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10, max_cost_usd=10.0),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn,
    )
    # turn1 -> 4, turn2 -> 8, turn3 -> 12 ; next pre-turn check sees 12 >= 10 -> stop
    assert final.stopped_reason == "cost-budget-exceeded"
    assert final.cumulative_cost_usd >= 10.0


def test_run_loop_single_turn_does_one_turn_and_returns():
    state = ld.LoopState(run_id="r", condition="c")
    check_goal, run_turn, read_cost, sleep_fn, calls = _runner([{"met": False}])
    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10),
        check_goal=check_goal, run_turn=run_turn, read_turn_cost=read_cost,
        persist=lambda s: None, sleep_fn=sleep_fn, single_turn=True,
    )
    assert final.turn == 1
    assert final.stopped is False  # not stopped; scheduler drives the next turn
    assert calls["sleeps"] == 0  # single turn never sleeps


def test_run_loop_turn_exception_counts_as_failure_not_crash():
    state = ld.LoopState(run_id="r", condition="c")

    def boom():
        raise RuntimeError("worker exploded")

    final = ld.run_loop(
        state, ld.LoopLimits(max_turns=10, max_consecutive_failures=1),
        check_goal=lambda: {"met": False}, run_turn=boom, read_turn_cost=lambda r: 0.0,
        persist=lambda s: None, sleep_fn=lambda s: None,
    )
    assert final.stopped_reason == "max-consecutive-failures"
    assert any(h["event"] == "turn-error" for h in final.history)


def test_state_roundtrip_and_resume(tmp_path):
    path = tmp_path / "loop-state.json"
    state = ld.LoopState(run_id="abc", condition="c", turn=2, cumulative_cost_usd=5.0)
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    resumed = ld.load_or_init_state(path, "abc", "c")
    assert resumed.turn == 2
    assert resumed.cumulative_cost_usd == 5.0
    # A different run_id starts fresh rather than resuming the wrong loop.
    fresh = ld.load_or_init_state(path, "different", "c")
    assert fresh.turn == 0


def test_cloud_workflow_yaml_has_schedule_and_single_turn():
    yaml = ld.cloud_workflow_yaml(
        cron="0 6 * * *", config_path="config/x.toml", condition='tests pass "fully"',
        from_version=51, to_version=57, max_turns=1,
    )
    assert "cron: \"0 6 * * *\"" in yaml
    assert "--single-turn" in yaml
    assert "--from-version 51" in yaml
    assert "never" in yaml.lower()  # documents no auto-merge
    assert '\\"fully\\"' in yaml  # quote in condition is escaped
