"""Stop-condition (``/goal``) evaluation: a fresh-model maker-checker for loops.

This implements the loop-engineering ``/goal`` primitive. The completion of a
turn is decided by a *fresh* model -- one that did not produce the work -- rather
than by the generator grading its own homework. Evaluation has two layers:

1. Deterministic checks (the configured verification commands) are run to gather
   real evidence. These form a hard floor: if any configured check fails, the
   condition cannot be considered met, regardless of what the model claims. A
   model cannot talk a red test into being green.
2. A fresh model reads that evidence and decides whether the natural-language
   stop condition holds, defaulting to "not met" unless the evidence proves
   otherwise.

The module is deliberately side-effect-light and model-agnostic: running the
judge model is left to each mode (council/checkpoint), which already owns worker
invocation. Everything here -- check execution, prompt construction, and verdict
parsing -- is pure enough to unit-test without a live CLI.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from typing import Any


@dataclasses.dataclass(frozen=True)
class CheckResult:
    command: tuple[str, ...]
    passed: bool
    returncode: int
    output: str  # trailing slice of combined stdout/stderr

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "passed": self.passed,
            "returncode": self.returncode,
            "output": self.output,
        }


def run_deterministic_checks(
    repo: Path,
    checks: tuple[tuple[str, ...], ...],
    timeout: int = 600,
    max_output_chars: int = 4000,
) -> tuple[CheckResult, ...]:
    """Run each configured check command in ``repo``, capturing pass/fail + output.

    A command that cannot be launched (missing executable, OS error) or times out
    is recorded as a failed check rather than raising, so a broken check command
    surfaces as "not met" instead of crashing the loop.
    """
    results: list[CheckResult] = []
    for command in checks:
        if not command:
            continue
        try:
            cp = subprocess.run(
                list(command),
                cwd=repo,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            combined = ((cp.stdout or "") + (("\n" + cp.stderr) if cp.stderr else "")).strip()
            results.append(
                CheckResult(
                    command=tuple(command),
                    passed=cp.returncode == 0,
                    returncode=cp.returncode,
                    output=combined[-max_output_chars:],
                )
            )
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(
                    command=tuple(command),
                    passed=False,
                    returncode=-1,
                    output=f"check timed out after {timeout}s"[-max_output_chars:],
                )
            )
        except OSError as exc:
            results.append(
                CheckResult(
                    command=tuple(command),
                    passed=False,
                    returncode=-1,
                    output=f"check could not run: {exc}"[-max_output_chars:],
                )
            )
    return tuple(results)


def deterministic_all_passed(results: tuple[CheckResult, ...]) -> bool:
    """True only when at least one check ran and all checks passed."""
    return bool(results) and all(r.passed for r in results)


def stop_condition_prompt(condition: str, results: tuple[CheckResult, ...]) -> str:
    """Build the fresh-model judge prompt with the deterministic check evidence."""
    blocks = []
    for r in results:
        status = "PASSED" if r.passed else "FAILED"
        blocks.append(
            f"$ {' '.join(r.command)}\n[{status}] exit={r.returncode}\n{r.output or '(no output)'}"
        )
    evidence = "\n\n".join(blocks) if blocks else "(no deterministic checks were configured)"
    return f"""You are a FRESH evaluator deciding whether a stop condition has been met. You did not produce
the work under review, and you must not assume it is complete. Decide strictly from the evidence below.

STOP CONDITION (decide whether this holds):
{condition}

DETERMINISTIC CHECK EVIDENCE (real command output):
{evidence}

RULES:
- Default to NOT met. Report met=true only when the evidence positively establishes every part of the
  condition. If any required check failed, or the evidence does not cover part of the condition, it is
  not met.
- Judge only what the evidence shows, never stated intent. You may inspect the worktree read-only for
  more evidence, but do not edit, run destructive commands, merge, push, or deploy anything.

Return exactly one JSON object and nothing else:
{{
  "met": true_or_false,
  "reasons": ["each satisfied part of the condition, with the evidence that establishes it"],
  "unmet": ["each part of the condition not yet established, and what evidence is missing"]
}}
"""


def parse_stop_verdict(text: str, results: tuple[CheckResult, ...]) -> dict[str, Any]:
    """Parse the fresh model's JSON verdict and combine it with the deterministic floor.

    The deterministic result is authoritative as a floor: if any configured check
    failed, ``met`` is forced to ``False`` even if the model claims success.
    """
    match = re.search(r"\{.*\}", text, re.S)
    model_met = False
    reasons: list[str] = []
    unmet: list[str] = []
    parsed_ok = False
    if match:
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            parsed_ok = True
            model_met = bool(obj.get("met", False))
            raw_reasons = obj.get("reasons", [])
            raw_unmet = obj.get("unmet", [])
            reasons = [str(x) for x in raw_reasons] if isinstance(raw_reasons, list) else []
            unmet = [str(x) for x in raw_unmet] if isinstance(raw_unmet, list) else []

    det_passed = deterministic_all_passed(results)
    # Floor: a failed deterministic check vetoes a "met" verdict.
    met = bool(model_met and (det_passed or not results))
    if model_met and results and not det_passed:
        unmet = [*unmet, "a configured deterministic check failed; the condition cannot be met until it passes"]
    return {
        "met": met,
        "model_met": model_met,
        "deterministic_all_passed": det_passed,
        "verdict_parsed": parsed_ok,
        "reasons": reasons,
        "unmet": unmet,
        "checks": [r.to_dict() for r in results],
    }
