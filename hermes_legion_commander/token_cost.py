"""Local prompt token estimation and shadow API cost accounting.

This module is deliberately provider-neutral and offline. It does not call the
OpenAI or Anthropic APIs. It estimates prompt size before Codex CLI / Claude Code
execution, stores the estimate for later learning, and computes the USD cost the
same prompt would approximately have incurred if sent through the vendors' public
API pricing instead of a ChatGPT/Claude subscription OAuth session.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
ESTIMATOR_VERSION = "2026.06.24-char-code-v1"
PRICING_SCHEMA_VERSION = 1

# Prices are USD per 1M tokens. Keep this table auditable and overridable via a
# project-local JSON file if vendor prices move before the package is upgraded.
# Official source snapshots used when this table was authored:
# - OpenAI API pricing page, accessed 2026-06-24.
# - Anthropic model/pricing docs, accessed 2026-06-24.
DEFAULT_MODEL_PRICES_USD_PER_MTOK: dict[str, dict[str, Any]] = {
    "openai:gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00, "source": "https://openai.com/api/pricing/"},
    "openai:gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00, "source": "https://openai.com/api/pricing/"},
    "openai:gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50, "source": "https://openai.com/api/pricing/"},
    "openai:gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00, "source": "https://developers.openai.com/api/docs/pricing"},
    "openai:gpt-5-codex": {"input": 1.25, "cached_input": 0.125, "output": 10.00, "source": "https://developers.openai.com/api/docs/models/gpt-5"},
    "openai:gpt-5": {"input": 1.25, "cached_input": 0.125, "output": 10.00, "source": "https://developers.openai.com/api/docs/models/gpt-5"},
    "openai:gpt-5-mini": {"input": 0.25, "cached_input": 0.025, "output": 2.00, "source": "https://developers.openai.com/api/docs/models/gpt-5"},
    "openai:gpt-5-nano": {"input": 0.05, "cached_input": 0.005, "output": 0.40, "source": "https://developers.openai.com/api/docs/models/gpt-5"},
    "anthropic:claude-opus-4-8": {"input": 5.00, "cached_input": 0.50, "output": 25.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-opus-4-7": {"input": 5.00, "cached_input": 0.50, "output": 25.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-opus-4-6": {"input": 5.00, "cached_input": 0.50, "output": 25.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-sonnet-4-6": {"input": 3.00, "cached_input": 0.30, "output": 15.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-sonnet-4-5": {"input": 3.00, "cached_input": 0.30, "output": 15.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-haiku-4-5": {"input": 1.00, "cached_input": 0.10, "output": 5.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
    "anthropic:claude-haiku-4-5-20251001": {"input": 1.00, "cached_input": 0.10, "output": 5.00, "source": "https://platform.claude.com/docs/en/about-claude/models/overview"},
}

OPENAI_FALLBACK = {"input": 1.25, "cached_input": 0.125, "output": 10.00, "source": "fallback:openai-gpt-5-class"}
ANTHROPIC_FALLBACK = {"input": 3.00, "cached_input": 0.30, "output": 15.00, "source": "fallback:anthropic-sonnet-class"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, row: dict[str, Any], *, max_rows: int = 2000) -> None:
    rows: list[str] = []
    if path.is_file():
        try:
            rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            rows = []
    rows.append(json.dumps(row, sort_keys=True))
    if max_rows > 0:
        rows = rows[-max_rows:]
    _atomic_write(path, "\n".join(rows) + "\n")


def estimate_text_tokens(text: str) -> dict[str, Any]:
    """Return a deterministic, local token estimate for natural language + code.

    The estimator intentionally overestimates code-like prompts slightly because
    punctuation, path names, JSON, and markdown fences tokenize less compactly
    than prose. It is not a vendor tokenizer and should be reconciled with any
    observed CLI usage after execution.
    """
    chars = len(text)
    utf8_bytes = len(text.encode("utf-8"))
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = chars - ascii_chars
    cjk_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af")
    code_markers = len(re.findall(r"[`{}()\[\];=<>/\\]|\b(def|class|import|from|function|const|let|var|return|async|await|pytest|test_)\b", text))
    path_markers = len(re.findall(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", text))
    jsonish = len(re.findall(r"\"[A-Za-z0-9_.-]+\"\s*:", text))
    line_count = text.count("\n") + (1 if text else 0)

    # Blend multiple signals. For English prose, chars/4 is common. For code,
    # line/path/punctuation density pushes tokens upward. For CJK, one char is
    # often closer to one token than four chars.
    char_based = chars / 4.0
    byte_based = utf8_bytes / 4.6
    cjk_adjustment = cjk_chars * 0.65
    code_adjustment = code_markers * 0.18 + path_markers * 1.4 + jsonish * 0.6 + max(0, line_count - 1) * 0.05
    estimate = max(char_based, byte_based) + cjk_adjustment + code_adjustment
    estimated = int(max(1, math.ceil(estimate))) if text else 0
    return {
        "schema_version": 1,
        "estimator_version": ESTIMATOR_VERSION,
        "method": "local char/byte/code-density heuristic; no provider API call",
        "chars": chars,
        "utf8_bytes": utf8_bytes,
        "line_count": line_count,
        "code_marker_count": code_markers,
        "path_marker_count": path_markers,
        "json_key_count": jsonish,
        "non_ascii_chars": non_ascii_chars,
        "cjk_chars": cjk_chars,
        "estimated_tokens": estimated,
        "confidence": "medium",
        "sha256": _sha256_text(text),
    }


def provider_for_runtime(runtime: str, provider: str | None = None) -> str:
    value = str(provider or "").strip().lower()
    runtime_value = str(runtime or "").strip().lower()
    if "anthropic" in value or runtime_value == "claude-code":
        return "anthropic"
    if "openai" in value or runtime_value == "codex-cli":
        return "openai"
    if "claude" in runtime_value:
        return "anthropic"
    return "openai"


def normalize_model_key(runtime: str, provider: str | None, model: str | None) -> tuple[str, dict[str, Any]]:
    vendor = provider_for_runtime(runtime, provider)
    model_id = str(model or "").strip()
    normalized = model_id.lower().replace("_", "-")
    if not normalized:
        fallback_key = f"{vendor}:<configured-default>"
        fallback_rates = ANTHROPIC_FALLBACK if vendor == "anthropic" else OPENAI_FALLBACK
        return fallback_key, {**fallback_rates, "matched": "provider_default_fallback", "model": model_id}
    direct_key = f"{vendor}:{normalized}"
    override_path = os.environ.get("HERMES_TOKEN_PRICE_OVERRIDES")
    if override_path:
        try:
            overrides = load_pricing_override(Path(override_path))
        except (OSError, json.JSONDecodeError):
            overrides = {}
        if direct_key in overrides:
            return direct_key, {**overrides[direct_key], "matched": "override_exact", "model": model_id, "source": str(Path(override_path))}
    if direct_key in DEFAULT_MODEL_PRICES_USD_PER_MTOK:
        return direct_key, {**DEFAULT_MODEL_PRICES_USD_PER_MTOK[direct_key], "matched": "exact", "model": model_id}
    # Pattern fallbacks keep estimates useful when vendors publish dated aliases.
    if vendor == "anthropic":
        if "haiku" in normalized:
            rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["anthropic:claude-haiku-4-5"]
            return "anthropic:claude-haiku-*", {**rates, "matched": "haiku_pattern", "model": model_id}
        if "opus" in normalized:
            rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["anthropic:claude-opus-4-8"]
            return "anthropic:claude-opus-*", {**rates, "matched": "opus_pattern", "model": model_id}
        rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["anthropic:claude-sonnet-4-6"]
        return "anthropic:claude-sonnet-*", {**rates, "matched": "sonnet_fallback", "model": model_id}
    if "mini" in normalized:
        rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["openai:gpt-5.4-mini"]
        return "openai:*mini*", {**rates, "matched": "mini_pattern", "model": model_id}
    if "codex" in normalized and "5.3" in normalized:
        rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["openai:gpt-5.3-codex"]
        return "openai:gpt-5.3-codex", {**rates, "matched": "codex_pattern", "model": model_id}
    if "codex" in normalized:
        rates = DEFAULT_MODEL_PRICES_USD_PER_MTOK["openai:gpt-5-codex"]
        return "openai:gpt-5-codex", {**rates, "matched": "codex_fallback", "model": model_id}
    return "openai:gpt-5-class", {**OPENAI_FALLBACK, "matched": "openai_fallback", "model": model_id}


def infer_auth_mode(runtime: str, provider: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    vendor = provider_for_runtime(runtime, provider)
    notes: list[str] = []
    if vendor == "anthropic":
        if any(env.get(name) for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
            mode = "cloud_provider_credentials"
            uses_subscription = False
            actual_billing = "cloud-provider account billing"
        elif env.get("ANTHROPIC_AUTH_TOKEN"):
            mode = "anthropic_bearer_token_or_gateway"
            uses_subscription = False
            actual_billing = "gateway/proxy dependent; not inferred"
        elif env.get("ANTHROPIC_API_KEY"):
            mode = "anthropic_api_key"
            uses_subscription = False
            actual_billing = "Anthropic API billing"
            notes.append("ANTHROPIC_API_KEY takes precedence over subscription OAuth in Claude Code non-interactive mode.")
        elif env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            mode = "claude_code_oauth_token"
            uses_subscription = True
            actual_billing = "Claude subscription included usage first, then usage credits if enabled"
        else:
            mode = "claude_subscription_oauth_or_cli_session"
            uses_subscription = True
            actual_billing = "Claude subscription included usage first, then usage credits if enabled"
        return {
            "provider": vendor,
            "mode": mode,
            "uses_subscription_or_oauth": uses_subscription,
            "actual_cli_billing": actual_billing,
            "api_key_env_present": bool(env.get("ANTHROPIC_API_KEY")),
            "oauth_token_env_present": bool(env.get("CLAUDE_CODE_OAUTH_TOKEN")),
            "notes": notes,
        }
    if env.get("OPENAI_API_KEY"):
        mode = "openai_api_key"
        uses_subscription = False
        actual_billing = "OpenAI Platform API billing"
        notes.append("OPENAI_API_KEY/API-key Codex auth uses standard API rates rather than ChatGPT plan credits.")
    elif env.get("CODEX_ACCESS_TOKEN"):
        mode = "chatgpt_oauth_access_token"
        uses_subscription = True
        actual_billing = "ChatGPT workspace/Codex plan entitlement"
    else:
        mode = "chatgpt_oauth_or_cli_session"
        uses_subscription = True
        actual_billing = "ChatGPT workspace/Codex plan entitlement"
    return {
        "provider": vendor,
        "mode": mode,
        "uses_subscription_or_oauth": uses_subscription,
        "actual_cli_billing": actual_billing,
        "api_key_env_present": bool(env.get("OPENAI_API_KEY")),
        "oauth_token_env_present": bool(env.get("CODEX_ACCESS_TOKEN")),
        "notes": notes,
    }


def learned_output_tokens(history_rows: list[dict[str, Any]], *, runtime: str, model: str, effort: str, scope_bucket: str) -> dict[str, Any] | None:
    values: list[int] = []
    normalized_model = str(model or "").lower()
    for row in history_rows[-500:]:
        row_runtime = str(row.get("runtime") or row.get("selected_runtime") or "")
        if row_runtime != runtime:
            continue
        row_model = str(row.get("model") or row.get("selected_model") or "").lower()
        if row_model and normalized_model and row_model != normalized_model:
            continue
        row_effort = str(row.get("effort") or row.get("selected_effort") or "")
        row_scope = str(row.get("scope_bucket") or ((row.get("scope") or {}) if isinstance(row.get("scope"), dict) else {}).get("scope_bucket") or "")
        if row_effort not in {"", effort} and row_scope not in {"", scope_bucket}:
            continue
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        output = row.get("output_tokens_observed") or usage.get("output_tokens") if isinstance(usage, dict) else None
        if output is None:
            output_chars = row.get("output_chars")
            try:
                output = int(float(output_chars)) // 4 if output_chars else None
            except (TypeError, ValueError):
                output = None
        try:
            value = int(float(output))
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    if not values:
        return None
    values.sort()
    avg = sum(values) / len(values)
    p25 = values[max(0, int(len(values) * 0.25) - 1)]
    p75 = values[min(len(values) - 1, int(len(values) * 0.75))]
    return {
        "source": "learned_history",
        "rows": len(values),
        "lower": int(max(1, p25)),
        "expected": int(max(1, round(avg))),
        "upper": int(max(p75, round(avg * 1.35))),
    }


def heuristic_output_tokens(input_tokens: int, *, effort: str, scope_bucket: str) -> dict[str, Any]:
    effort_ratio = {"low": 0.08, "medium": 0.14, "high": 0.24}.get(str(effort or "medium"), 0.14)
    scope_multiplier = {"tiny": 0.7, "small": 0.85, "medium": 1.0, "large": 1.25, "critical": 1.45}.get(str(scope_bucket or "medium"), 1.0)
    expected = int(max(500, min(64000, round(input_tokens * effort_ratio * scope_multiplier))))
    return {
        "source": "heuristic_by_input_effort_scope",
        "rows": 0,
        "lower": int(max(200, expected * 0.45)),
        "expected": expected,
        "upper": int(min(128000, max(expected + 250, expected * 2.2))),
    }


def estimate_api_cost_usd(
    *,
    runtime: str,
    provider: str | None,
    model: str | None,
    input_tokens: int,
    expected_output_tokens: int,
    cached_input_tokens: int = 0,
    lower_output_tokens: int | None = None,
    upper_output_tokens: int | None = None,
) -> dict[str, Any]:
    key, rates = normalize_model_key(runtime, provider, model)
    cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens or 0)))
    uncached = max(0, int(input_tokens or 0) - cached)
    lower = int(lower_output_tokens if lower_output_tokens is not None else expected_output_tokens)
    upper = int(upper_output_tokens if upper_output_tokens is not None else expected_output_tokens)

    def cost_for(output_tokens: int) -> float:
        return round(
            (uncached * float(rates["input"]) + cached * float(rates.get("cached_input", rates["input"])) + int(output_tokens) * float(rates["output"])) / 1_000_000,
            6,
        )

    return {
        "schema_version": PRICING_SCHEMA_VERSION,
        "currency": "USD",
        "billing_basis": "shadow_api_equivalent_for_subscription_or_oauth_cli",
        "model_price_key": key,
        "model_price_match": rates.get("matched"),
        "model": rates.get("model", model or ""),
        "rates_usd_per_1m_tokens": {
            "input": rates["input"],
            "cached_input": rates.get("cached_input"),
            "output": rates["output"],
        },
        "tokens": {
            "input_estimated": int(input_tokens or 0),
            "cached_input_estimated": cached,
            "uncached_input_estimated": uncached,
            "output_lower_estimated": lower,
            "output_expected_estimated": int(expected_output_tokens or 0),
            "output_upper_estimated": upper,
        },
        "cost_usd": {
            "input": round((uncached * float(rates["input"]) + cached * float(rates.get("cached_input", rates["input"]))) / 1_000_000, 6),
            "output_lower": round((lower * float(rates["output"])) / 1_000_000, 6),
            "output_expected": round((int(expected_output_tokens or 0) * float(rates["output"])) / 1_000_000, 6),
            "output_upper": round((upper * float(rates["output"])) / 1_000_000, 6),
            "total_lower": cost_for(lower),
            "total_expected": cost_for(int(expected_output_tokens or 0)),
            "total_upper": cost_for(upper),
        },
        "pricing_source": rates.get("source"),
        "pricing_note": "Static embedded public API price snapshot; update/override if vendor pricing changes.",
    }


def build_prompt_preflight(
    *,
    agent: Any,
    prompt: str,
    stage: str,
    scope_routing: dict[str, Any] | None = None,
    history_rows: list[dict[str, Any]] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    scope = (scope_routing or {}).get("scope") if isinstance(scope_routing, dict) else {}
    if not isinstance(scope, dict):
        scope = {}
    prompt_estimate = estimate_text_tokens(prompt)
    runtime = str(getattr(agent, "runtime", ""))
    provider = str(getattr(agent, "provider", ""))
    model = str(getattr(agent, "model", ""))
    effort = str(getattr(agent, "effort", "medium") or "medium")
    scope_bucket = str(scope.get("scope_bucket") or "medium")
    learned = learned_output_tokens(history_rows or [], runtime=runtime, model=model, effort=effort, scope_bucket=scope_bucket)
    output_estimate = learned or heuristic_output_tokens(int(prompt_estimate["estimated_tokens"]), effort=effort, scope_bucket=scope_bucket)
    cost = estimate_api_cost_usd(
        runtime=runtime,
        provider=provider,
        model=model,
        input_tokens=int(prompt_estimate["estimated_tokens"]),
        expected_output_tokens=int(output_estimate["expected"]),
        lower_output_tokens=int(output_estimate["lower"]),
        upper_output_tokens=int(output_estimate["upper"]),
    )
    auth = infer_auth_mode(runtime, provider, env)
    return {
        "schema_version": 1,
        "estimated_at": dt.datetime.now(UTC).isoformat(),
        "stage": stage,
        "agent": str(getattr(agent, "name", "")),
        "runtime": runtime,
        "provider": provider_for_runtime(runtime, provider),
        "model": model,
        "effort": effort,
        "auth": auth,
        "prompt": prompt_estimate,
        "output_tokens": output_estimate,
        "estimated_api_cost_usd": cost,
        "scope": {
            "scope_bucket": scope.get("scope_bucket"),
            "scope_score": scope.get("scope_score"),
            "task_types": scope.get("task_types", []),
            "risk_flags": scope.get("risk_flags", []),
        },
        "notes": [
            "Recorded before invoking the native CLI.",
            "Actual subscription/OAuth usage is governed by the native vendor CLI and plan limits; this is a shadow API-equivalent estimate.",
            "Reconcile with observed CLI token/cost fields when the CLI emits them.",
        ],
    }


def reconcile_usage(preflight: dict[str, Any], observed_usage: dict[str, Any] | None) -> dict[str, Any]:
    observed_usage = observed_usage or {}
    estimated_input = ((preflight.get("prompt") or {}) if isinstance(preflight, dict) else {}).get("estimated_tokens")
    observed_input = observed_usage.get("input_tokens") or observed_usage.get("prompt_tokens")
    observed_output = observed_usage.get("output_tokens") or observed_usage.get("completion_tokens")
    result: dict[str, Any] = {
        "schema_version": 1,
        "has_observed_usage": bool(observed_usage),
        "estimated_input_tokens": estimated_input,
        "observed_input_tokens": observed_input,
        "observed_output_tokens": observed_output,
    }
    try:
        if estimated_input and observed_input:
            estimated = float(estimated_input)
            observed = float(observed_input)
            result["input_estimate_error_ratio"] = round((estimated - observed) / observed, 4) if observed else None
            result["input_estimate_accuracy"] = round(1.0 - min(1.0, abs(estimated - observed) / max(observed, 1.0)), 4)
    except (TypeError, ValueError):
        pass
    result["note"] = "Observed usage is CLI-reported when available; missing values are normal for subscription/OAuth CLI sessions."
    return result


def load_pricing_override(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "input" in value and "output" in value:
            result[str(key).lower()] = value
    return result


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-legion-commander token-cost", description="Estimate prompt tokens and shadow API-equivalent cost offline.")
    parser.add_argument("--runtime", choices=("codex-cli", "claude-code"), required=True)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--stage", default="manual")
    args = parser.parse_args(argv)

    class _Agent:
        pass

    agent = _Agent()
    agent.name = args.runtime
    agent.runtime = args.runtime
    agent.provider = args.provider
    agent.model = args.model
    agent.effort = args.effort
    prompt = args.prompt_file.read_text(encoding="utf-8")
    payload = build_prompt_preflight(agent=agent, prompt=prompt, stage=args.stage)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
