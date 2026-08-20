"""Generic executor invocation for CLI, local, custom, MCP-shim, and HTTP API runtimes."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .legion import AuthKind, AuthProfile, Executor, ExecutorRegistry, LegionError, RuntimeAdapter
from .skill_profile import render_skill_context, roots_for_executor


@dataclass(frozen=True)
class ExecutionResult:
    executor_id: str
    provider: str
    model: str
    runtime: str
    output: str
    returncode: int
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _account_values(profile: AuthProfile, executor: Executor, runtime: RuntimeAdapter) -> dict[str, str]:
    return {
        "account_id": profile.id,
        "account_label": profile.account_label or profile.id,
        "email": profile.email or "",
        "executor": executor.id,
        "provider": executor.provider,
        "runtime": runtime.id,
        "model": executor.model,
    }


def account_environment(
    profile: AuthProfile,
    executor: Executor,
    runtime: RuntimeAdapter,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build an executor-local environment without leaking account data into prompts.

    OAuth/native account isolation is configured on the auth profile through
    ``environment``. This lets two executors use the same CLI binary while
    pointing it at different credential/config roots (for example CODEX_HOME or
    CLAUDE_CONFIG_DIR). Values may reference {account_id}, {account_label},
    {email}, {executor}, {provider}, {runtime}, and {model}.
    """
    env = dict(os.environ if base is None else base)
    values = _account_values(profile, executor, runtime)
    for name, raw in profile.environment.items():
        rendered = str(raw).format(**values)
        rendered = os.path.expandvars(os.path.expanduser(rendered))
        env[str(name)] = rendered
    env["LEGION_ACCOUNT_ID"] = profile.id
    env["LEGION_ACCOUNT_LABEL"] = profile.account_label or profile.id
    if profile.email:
        env["LEGION_ACCOUNT_EMAIL"] = profile.email
    return env


def _account_command(
    runtime: RuntimeAdapter, executor: Executor, profile: AuthProfile, action: str
) -> list[str]:
    template = runtime.login_command if action == "login" else runtime.auth_status_command
    if not template:
        raise LegionError(f"runtime {runtime.id!r} does not define an {action!r} account command")
    values = _account_values(profile, executor, runtime)
    return [str(item).format(**values) for item in template]


def run_account_action(
    registry: ExecutorRegistry,
    executor_id: str,
    action: str,
    *,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    interactive: bool | None = None,
) -> dict[str, Any]:
    """Login to or inspect one isolated executor account.

    The configured email is an operator-facing account identifier, not a secret
    and not proof of the provider-side identity. ``status`` returns the native
    CLI output so operators can verify the selected login before scheduling it.
    """
    if action not in {"login", "status"}:
        raise LegionError(f"unsupported account action {action!r}")
    executor = registry.executors.get(executor_id)
    if executor is None:
        raise LegionError(f"unknown executor {executor_id!r}")
    runtime = registry.runtimes[executor.runtime]
    profile = registry.auth_profiles[executor.auth_profile]
    if profile.kind not in {AuthKind.OAUTH, AuthKind.NATIVE}:
        raise LegionError(
            f"executor {executor.id!r} uses {profile.kind.value}; account login/status commands are for OAuth/native CLI accounts"
        )
    command = _account_command(runtime, executor, profile, action)
    process_env = account_environment(profile, executor, runtime, env)
    if interactive is None:
        interactive = action == "login"
    if interactive:
        completed = subprocess.run(command, check=False, timeout=timeout, env=process_env)
        output = ""
    else:
        completed = subprocess.run(
            command, text=True, encoding="utf-8", errors="replace", capture_output=True,
            check=False, timeout=timeout, env=process_env,
        )
        output = ((completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")).strip()
    return {
        "executor": executor.id,
        "provider": executor.provider,
        "runtime": runtime.id,
        "auth_profile": profile.id,
        "account_label": profile.account_label or profile.id,
        "configured_email": profile.email,
        "action": action,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": output,
        "isolation_environment": sorted(profile.environment),
    }


def resolve_secret(profile: AuthProfile, env: dict[str, str] | None = None) -> str | None:
    """Resolve a referenced credential without ever putting it in a worker prompt."""
    if profile.kind in {AuthKind.NONE, AuthKind.NATIVE} or not profile.secret_ref:
        return None
    env = os.environ if env is None else env
    ref = profile.secret_ref
    if ref.startswith("env:"):
        name = ref[4:]
        if not name or name not in env:
            raise LegionError(f"credential environment variable {name!r} is unavailable for auth profile {profile.id!r}")
        return env[name]
    if ref.startswith("file:"):
        path = Path(os.path.expandvars(os.path.expanduser(ref[5:])))
        if not path.is_file():
            raise LegionError(f"credential file does not exist for auth profile {profile.id!r}: {path}")
        return path.read_text(encoding="utf-8").strip()
    if ref.startswith("keyring:"):
        target = ref[8:]
        if "/" not in target:
            raise LegionError("keyring secret_ref must be keyring:<service>/<name>")
        service, name = target.split("/", 1)
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # optional dependency boundary
            raise LegionError("keyring secret source requested but Python keyring package is not installed") from exc
        secret = keyring.get_password(service, name)
        if secret is None:
            raise LegionError(f"keyring entry {service}/{name} is unavailable")
        return secret
    raise LegionError(
        f"unsupported secret reference {ref!r}; use env:, file:, keyring:, or native runtime authentication"
    )


def _format_value(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**values)
    if isinstance(value, list):
        return [_format_value(item, values) for item in value]
    if isinstance(value, dict):
        return {str(key): _format_value(child, values) for key, child in value.items()}
    return value


def _response_path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise LegionError(f"response path {path!r} cannot index list with {part!r}") from exc
        elif isinstance(current, dict):
            if part not in current:
                raise LegionError(f"response path {path!r} missing key {part!r}")
            current = current[part]
        else:
            raise LegionError(f"response path {path!r} traverses non-container at {part!r}")
    return current


def _cli_command(runtime: RuntimeAdapter, executor: Executor, prompt: str, cwd: Path) -> list[str]:
    values = {
        "prompt": prompt,
        "model": executor.model,
        "cwd": str(cwd),
        "provider": executor.provider,
        "executor": executor.id,
        "runtime": runtime.id,
    }
    rendered: list[str] = []
    for item in runtime.command:
        if item == "{model_args}":
            if executor.model:
                rendered.extend(["--model", executor.model])
            continue
        rendered.append(item.format(**values))
    return rendered


def _invoke_cli(
    runtime: RuntimeAdapter,
    executor: Executor,
    profile: AuthProfile,
    prompt: str,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None,
) -> ExecutionResult:
    command = _cli_command(runtime, executor, prompt, cwd)
    process_env = account_environment(profile, executor, runtime, env)
    token = resolve_secret(profile, process_env)
    if token is not None:
        # Generic command adapters can opt in to a credential variable. The
        # value is never interpolated into command arguments or prompt text.
        credential_env = str(runtime.metadata.get("credential_env", "LEGION_AUTH_TOKEN"))
        process_env[credential_env] = token
    stdin = prompt if runtime.prompt_transport == "stdin" else None
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        env=process_env,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"executor {executor.id!r} failed with code {completed.returncode}: {detail}")
    if not output:
        output = (completed.stderr or "").strip()
    if not output:
        raise RuntimeError(f"executor {executor.id!r} completed without textual output")
    return ExecutionResult(
        executor.id,
        executor.provider,
        executor.model,
        runtime.id,
        output,
        completed.returncode,
        metadata={"transport": runtime.transport, "command": [command[0], "<redacted-args>"]},
    )


def _invoke_api(
    runtime: RuntimeAdapter,
    executor: Executor,
    profile: AuthProfile,
    prompt: str,
    timeout: int,
    env: dict[str, str] | None,
) -> ExecutionResult:
    if not runtime.endpoint:
        raise LegionError(f"API runtime {runtime.id!r} has no endpoint")
    process_env = account_environment(profile, executor, runtime, env)
    values = {
        "prompt": prompt,
        "model": executor.model,
        "provider": executor.provider,
        "executor": executor.id,
        "runtime": runtime.id,
    }
    template = runtime.metadata.get("request_template")
    if not isinstance(template, dict):
        # Neutral default suitable for simple OpenAI-compatible adapters; other
        # providers can specify request_template and response_path in TOML.
        template = {"model": "{model}", "messages": [{"role": "user", "content": "{prompt}"}]}
    payload = _format_value(template, values)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    configured_headers = runtime.metadata.get("headers", {})
    if isinstance(configured_headers, dict):
        headers.update({str(k): str(v).format(**values) for k, v in configured_headers.items()})
    token = resolve_secret(profile, process_env)
    if token is not None:
        header = str(runtime.metadata.get("auth_header", "Authorization"))
        prefix = str(runtime.metadata.get("auth_prefix", "Bearer"))
        headers[header] = f"{prefix} {token}".strip()
    request = urllib.request.Request(
        runtime.endpoint,
        data=body,
        headers=headers,
        method=str(runtime.metadata.get("method", "POST")),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"executor {executor.id!r} API request failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"executor {executor.id!r} API request failed: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = raw
    response_path = str(runtime.metadata.get("response_path", "choices.0.message.content"))
    output_value = _response_path(data, response_path) if response_path else data
    output = output_value if isinstance(output_value, str) else json.dumps(output_value, ensure_ascii=False)
    usage_path = str(runtime.metadata.get("usage_path", "usage"))
    usage: dict[str, Any] = {}
    if isinstance(data, (dict, list)) and usage_path:
        try:
            usage_value = _response_path(data, usage_path)
            if isinstance(usage_value, dict):
                usage = usage_value
        except LegionError:
            usage = {}
    return ExecutionResult(
        executor.id,
        executor.provider,
        executor.model,
        runtime.id,
        output.strip(),
        0 if 200 <= status < 300 else status,
        usage=usage,
        metadata={"transport": "api", "status": status, "endpoint": runtime.endpoint},
    )


def invoke_executor(
    registry: ExecutorRegistry,
    executor_id: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: int = 900,
    env: dict[str, str] | None = None,
    active_skills: Iterable[str] = (),
) -> ExecutionResult:
    executor = registry.executors.get(executor_id)
    if executor is None:
        raise LegionError(f"unknown executor {executor_id!r}")
    runtime = registry.runtimes[executor.runtime]
    profile = registry.auth_profiles[executor.auth_profile]
    budget = registry.budgets[executor.id]
    usable, reason = budget.usable()
    if not usable:
        raise LegionError(f"executor {executor.id!r} is not schedulable: {reason}")

    if (
        profile.kind in {AuthKind.OAUTH, AuthKind.NATIVE}
        and runtime.auth_status_command
        and bool(profile.metadata.get("verify_before_run", True))
    ):
        auth_status = run_account_action(
            registry, executor.id, "status", timeout=min(timeout, 60), env=env, interactive=False
        )
        if not auth_status["ok"]:
            raise LegionError(
                f"executor {executor.id!r} account preflight failed for auth profile {profile.id!r}; "
                "run `hermes-legion-commander legion accounts login --config <file> "
                f"--executor {executor.id}`"
            )

    selected_skills = tuple(active_skills)[:3]
    skill_roots = roots_for_executor(registry, executor)
    process_env = account_environment(profile, executor, runtime, env)
    if selected_skills:
        process_env["LEGION_ACTIVE_SKILLS"] = ",".join(selected_skills)
        process_env["LEGION_SKILL_ROOTS"] = os.pathsep.join(str(root) for root in skill_roots)
        native_discovery = bool(runtime.metadata.get("native_skill_discovery", runtime.transport == "cli"))
        if not native_discovery:
            skill_context = render_skill_context(skill_roots, selected_skills)
            prompt = (
                "# HERMES LEGION COMMANDER ACTIVE SKILLS\n\n"
                "Use only the bounded reviewed skills below for this stage. The full baseline remains installed, "
                "but activating additional skills requires a new stage/checkpoint.\n\n"
                + skill_context + "\n\n" + prompt
            )
    if runtime.transport == "api":
        return _invoke_api(runtime, executor, profile, prompt, timeout, process_env)
    return _invoke_cli(runtime, executor, profile, prompt, cwd, timeout, process_env)
