"""GUI-managed multi-account registry for Hermes Legion Commander.

The registry is a companion JSON file next to a Legion TOML config. It stores
only non-secret metadata and secret references. Raw API keys are forbidden.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALLOWED_AUTH = {"oauth", "api_key", "native"}
_ALLOWED_PRESETS = {"codex-cli", "claude-code", "openai-compatible-api", "anthropic-api", "custom-cli"}
_ALLOWED_ROLE_MODES = {"allowed", "preferred"}
_ALLOWED_GUARD_MODES = {"standard", "strict", "lockdown"}
_ALLOWED_TRUST = {"local", "controlled_cloud", "external_cloud", "restricted"}
_ALLOWED_DATA_CLASSES = {"public", "internal", "confidential", "restricted"}
_SECRET_REF_PREFIXES = ("env:", "file:", "keyring:")


class AccountRegistryError(ValueError):
    """Raised when GUI-managed account metadata is unsafe or inconsistent."""


def companion_path(config_path: Path) -> Path:
    config_path = config_path.expanduser().resolve()
    return config_path.with_name(f"{config_path.stem}.accounts.json")


def empty_registry() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "accounts": [], "role_bindings": []}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise AccountRegistryError(f"expected list, got {type(value).__name__}")


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in _as_list(value) if str(item).strip()]


def _clean_id(value: Any, field: str = "id") -> str:
    result = str(value or "").strip().lower()
    if not _ID_RE.fullmatch(result):
        raise AccountRegistryError(
            f"{field} must match {_ID_RE.pattern!r}; use lowercase letters, digits, dot, underscore, or hyphen"
        )
    return result


def _secret_ref(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    ref = str(value).strip()
    if not ref.startswith(_SECRET_REF_PREFIXES):
        raise AccountRegistryError("API credentials must use env:, file:, or keyring: references; raw keys are forbidden")
    if any(ch in ref for ch in ("\n", "\r", "\x00")):
        raise AccountRegistryError("secret reference contains unsafe control characters")
    return ref


def _argv(value: Any, *, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise AccountRegistryError("command is required")
        return []
    if isinstance(value, list):
        parts = [str(item) for item in value if str(item)]
    else:
        text = str(value).strip()
        parts = shlex.split(text, posix=os.name != "nt") if text else []
    if required and not parts:
        raise AccountRegistryError("command is required")
    return parts


def normalize_account(raw: Mapping[str, Any]) -> dict[str, Any]:
    account_id = _clean_id(raw.get("id"), "account id")
    preset = str(raw.get("preset", "codex-cli")).strip()
    if preset not in _ALLOWED_PRESETS:
        raise AccountRegistryError(f"unsupported account preset {preset!r}")
    auth_kind = str(raw.get("auth_kind", "oauth")).strip()
    if auth_kind not in _ALLOWED_AUTH:
        raise AccountRegistryError(f"unsupported auth kind {auth_kind!r}")

    preset_provider = {
        "codex-cli": "openai",
        "claude-code": "anthropic",
        "anthropic-api": "anthropic",
    }.get(preset)
    provider = str(raw.get("provider") or preset_provider or "").strip().lower()
    if not provider:
        raise AccountRegistryError("provider is required")
    if preset_provider and provider != preset_provider:
        raise AccountRegistryError(f"{preset} requires provider {preset_provider!r}")

    if preset in {"openai-compatible-api", "anthropic-api"} and auth_kind != "api_key":
        raise AccountRegistryError(f"{preset} requires api_key authentication")
    if preset in {"codex-cli", "claude-code"} and auth_kind not in {"oauth", "native"}:
        raise AccountRegistryError(
            f"{preset} uses its native subscription/OAuth credential store in the GUI; "
            "use an API preset for API-key execution"
        )
    if preset == "custom-cli" and auth_kind == "oauth" and not _argv(raw.get("login_command")):
        raise AccountRegistryError("custom OAuth CLI requires login_command")

    endpoint = str(raw.get("endpoint") or "").strip()
    if preset in {"openai-compatible-api", "anthropic-api"} and not endpoint:
        raise AccountRegistryError("API endpoint is required")
    if endpoint and not endpoint.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise AccountRegistryError("API endpoint must use HTTPS, except explicit localhost development endpoints")

    ref = _secret_ref(raw.get("secret_ref"))
    if auth_kind == "api_key" and not ref:
        raise AccountRegistryError("api_key authentication requires a secret reference")
    if auth_kind != "api_key" and ref:
        raise AccountRegistryError("secret_ref is only valid for api_key authentication")

    model = str(raw.get("model") or "default").strip()
    if not model:
        raise AccountRegistryError("model is required")

    guard = dict(raw.get("data_guard", {})) if isinstance(raw.get("data_guard"), Mapping) else {}
    default_guard = {
        "mode": "strict" if preset in {"openai-compatible-api", "anthropic-api"} else "standard",
        "trust_tier": "external_cloud" if preset in {"openai-compatible-api", "anthropic-api"} else "controlled_cloud",
        "max_data_class": "internal" if preset in {"openai-compatible-api", "anthropic-api"} else "confidential",
        "remote_backend": preset != "custom-cli" or bool(raw.get("remote_backend", True)),
        "secret_action": "block" if preset in {"openai-compatible-api", "anthropic-api"} else "redact",
        "audit_enabled": True,
    }
    default_guard.update(guard)
    if str(default_guard.get("mode")) not in _ALLOWED_GUARD_MODES:
        raise AccountRegistryError("invalid data_guard.mode")
    if str(default_guard.get("trust_tier")) not in _ALLOWED_TRUST:
        raise AccountRegistryError("invalid data_guard.trust_tier")
    if str(default_guard.get("max_data_class")) not in _ALLOWED_DATA_CLASSES:
        raise AccountRegistryError("invalid data_guard.max_data_class")

    roles = _strings(raw.get("roles"))
    for role in roles:
        _clean_id(role, "role id")

    budget = dict(raw.get("budget", {})) if isinstance(raw.get("budget"), Mapping) else {}
    normalized_budget = {
        "subscription_remaining_percent": float(budget.get("subscription_remaining_percent", 100.0)),
        "reserve_percent": float(budget.get("reserve_percent", 25.0)),
        "no_new_work_below_percent": float(budget.get("no_new_work_below_percent", 35.0)),
        "daily_unattended_max_percent": float(budget.get("daily_unattended_max_percent", 10.0)),
        "session_checkpoint_percent": float(budget.get("session_checkpoint_percent", 4.0)),
        "session_hard_stop_percent": float(budget.get("session_hard_stop_percent", 5.0)),
        "max_parallel": int(budget.get("max_parallel", 1)),
        "enabled": bool(budget.get("enabled", True)),
    }
    if not (0 <= normalized_budget["subscription_remaining_percent"] <= 100):
        raise AccountRegistryError("subscription_remaining_percent must be between 0 and 100")
    if normalized_budget["max_parallel"] < 1:
        raise AccountRegistryError("max_parallel must be >= 1")

    custom = {}
    if preset == "custom-cli":
        custom = {
            "command": _argv(raw.get("command"), required=True),
            "login_command": _argv(raw.get("login_command")),
            "auth_status_command": _argv(raw.get("auth_status_command")),
            "prompt_transport": str(raw.get("prompt_transport") or "argument"),
            "sandbox_enforced": bool(raw.get("sandbox_enforced", False)),
            "capabilities": _strings(raw.get("capabilities")) or ["repo_read", "repo_write", "shell"],
        }
        if custom["prompt_transport"] not in {"argument", "stdin"}:
            raise AccountRegistryError("custom CLI prompt_transport must be argument or stdin")

    result = {
        "id": account_id,
        "preset": preset,
        "provider": provider,
        "auth_kind": auth_kind,
        "account_label": str(raw.get("account_label") or account_id).strip(),
        "email": str(raw.get("email") or "").strip(),
        "model": model,
        "endpoint": endpoint,
        "secret_ref": ref,
        "priority": int(raw.get("priority", 100)),
        "roles": roles,
        "role_mode": str(raw.get("role_mode") or "preferred"),
        "bound_roles_only": bool(raw.get("bound_roles_only", False)),
        "enabled": bool(raw.get("enabled", True)),
        "budget": normalized_budget,
        "data_guard": default_guard,
        **custom,
    }
    if result["role_mode"] not in _ALLOWED_ROLE_MODES:
        raise AccountRegistryError("role_mode must be allowed or preferred")
    return result


def normalize_role_binding(raw: Mapping[str, Any]) -> dict[str, Any]:
    role_id = _clean_id(raw.get("id"), "role id")
    mode = str(raw.get("mode") or "preferred")
    if mode not in _ALLOWED_ROLE_MODES:
        raise AccountRegistryError("role binding mode must be allowed or preferred")
    executors = [_clean_id(item, "executor id") for item in _strings(raw.get("executors"))]
    if not executors:
        raise AccountRegistryError("role binding requires at least one executor")
    return {
        "id": role_id,
        "objective": str(raw.get("objective") or f"Execute the {role_id} responsibility.").strip(),
        "mode": mode,
        "executors": list(dict.fromkeys(executors)),
    }


def validate_registry(raw: Mapping[str, Any]) -> dict[str, Any]:
    if int(raw.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
        raise AccountRegistryError(f"unsupported account registry schema_version {raw.get('schema_version')!r}")
    accounts = [normalize_account(row) for row in _as_list(raw.get("accounts")) if isinstance(row, Mapping)]
    ids = [row["id"] for row in accounts]
    if len(ids) != len(set(ids)):
        raise AccountRegistryError("duplicate account id in managed account registry")

    roles = [normalize_role_binding(row) for row in _as_list(raw.get("role_bindings")) if isinstance(row, Mapping)]
    role_ids = [row["id"] for row in roles]
    if len(role_ids) != len(set(role_ids)):
        raise AccountRegistryError("duplicate role binding id in managed account registry")
    unknown = sorted({eid for row in roles for eid in row["executors"]} - set(ids))
    if unknown:
        raise AccountRegistryError(f"role binding references unknown account(s): {', '.join(unknown)}")
    return {"schema_version": SCHEMA_VERSION, "accounts": accounts, "role_bindings": roles}


def load_registry(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.is_file():
        return empty_registry()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountRegistryError(f"cannot read account registry {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise AccountRegistryError("account registry root must be a JSON object")
    return validate_registry(raw)


def save_registry(path: Path, registry: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_registry(registry)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return normalized


def _skill_root(account: Mapping[str, Any]) -> str:
    account_id = str(account["id"])
    base = f"~/.local/share/hermes-legion-commander/accounts/{account_id}"
    if account["preset"] == "codex-cli":
        return f"{base}/codex/skills"
    if account["preset"] == "claude-code":
        return f"{base}/claude/skills"
    return f"{base}/skills"


def _runtime_and_auth(account: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    account_id = str(account["id"])
    preset = str(account["preset"])
    provider = str(account["provider"])
    auth_id = f"{account_id}-auth"
    environment: dict[str, str] = {}

    if preset == "codex-cli":
        runtime = {
            "id": "codex-cli",
            "provider": "openai",
            "transport": "cli",
            "command": ["codex", "exec", "--json", "{model_args}", "-"],
            "login_command": ["codex", "login"],
            "auth_status_command": ["codex", "login", "status"],
            "prompt_transport": "stdin",
            "output_format": "codex-jsonl",
            "auth_kinds": ["oauth", "api_key", "native"],
            "capabilities": ["repo_read", "repo_write", "shell"],
            "skill_roots": ["~/.agents/skills"],
            "metadata": {"native_skill_discovery": True},
        }
        environment["CODEX_HOME"] = f"~/.local/share/hermes-legion-commander/accounts/{account_id}/codex"
    elif preset == "claude-code":
        runtime = {
            "id": "claude-code",
            "provider": "anthropic",
            "transport": "cli",
            "command": ["claude", "-p", "{prompt}", "{model_args}"],
            "login_command": ["claude", "auth", "login"],
            "auth_status_command": ["claude", "auth", "status"],
            "prompt_transport": "argument",
            "output_format": "text",
            "auth_kinds": ["oauth", "api_key", "native"],
            "capabilities": ["repo_read", "repo_write", "shell"],
            "skill_roots": ["~/.agents/skills"],
            "metadata": {"native_skill_discovery": True},
        }
        environment["CLAUDE_CONFIG_DIR"] = f"~/.local/share/hermes-legion-commander/accounts/{account_id}/claude"
    elif preset in {"openai-compatible-api", "anthropic-api"}:
        runtime = {
            "id": f"{account_id}-api",
            "provider": provider,
            "transport": "api",
            "endpoint": str(account["endpoint"]),
            "capabilities": ["repo_read"],
            "auth_kinds": ["api_key"],
            "skill_roots": ["~/.agents/skills"],
            "metadata": {"native_skill_discovery": False},
        }
        if preset == "anthropic-api":
            runtime["metadata"].update({
                "request_template": {
                    "model": "{model}",
                    "max_tokens": 8192,
                    "messages": [{"role": "user", "content": "{prompt}"}],
                },
                "headers": {"anthropic-version": "2023-06-01"},
                "auth_header": "x-api-key",
                "auth_prefix": "",
                "response_path": "content.0.text",
            })
    else:
        runtime = {
            "id": f"{account_id}-cli",
            "provider": provider,
            "transport": "cli",
            "command": list(account.get("command", [])),
            "login_command": list(account.get("login_command", [])),
            "auth_status_command": list(account.get("auth_status_command", [])),
            "prompt_transport": str(account.get("prompt_transport", "argument")),
            "output_format": "text",
            "auth_kinds": [str(account["auth_kind"])],
            "capabilities": list(account.get("capabilities", ["repo_read", "repo_write", "shell"])),
            "skill_roots": ["~/.agents/skills"],
            "metadata": {
                "native_skill_discovery": True,
                "sandbox_enforced": bool(account.get("sandbox_enforced", False)),
            },
        }

    auth = {
        "id": auth_id,
        "kind": str(account["auth_kind"]),
        "provider": provider,
        "source": "native" if account["auth_kind"] in {"oauth", "native"} else "reference",
        "account_label": str(account["account_label"]),
        "environment": environment,
        "metadata": {"verify_before_run": True},
    }
    if account.get("email"):
        auth["email"] = str(account["email"])
    if account.get("secret_ref"):
        auth["secret_ref"] = str(account["secret_ref"])

    executor = {
        "id": account_id,
        "provider": provider,
        "model": str(account["model"]),
        "runtime": runtime["id"],
        "auth_profile": auth_id,
        "capabilities": list(runtime["capabilities"]),
        "skill_roots": [_skill_root(account)],
        "priority": int(account.get("priority", 100)),
        "labels": ["gui-managed", provider, preset],
        "enabled": bool(account.get("enabled", True)),
        "metadata": {
            "managed_by_gui": True,
            "data_guard": dict(account["data_guard"]),
        },
        "budget": dict(account["budget"]),
    }
    return runtime, auth, executor


def _same_runtime(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = ("id", "provider", "transport", "command", "login_command", "auth_status_command",
            "endpoint", "prompt_transport", "output_format", "auth_kinds", "capabilities", "skill_roots")
    return all(left.get(key) == right.get(key) for key in keys)


def _merge_unique_table(raw: dict[str, Any], key: str, row: dict[str, Any], *, runtime: bool = False) -> None:
    table = raw.setdefault(key, [])
    if not isinstance(table, list):
        raise AccountRegistryError(f"{key} must be a list in base Legion config")
    existing = next((item for item in table if isinstance(item, Mapping) and item.get("id") == row["id"]), None)
    if existing is None:
        table.append(row)
        return
    if runtime and _same_runtime(existing, row):
        return
    raise AccountRegistryError(
        f"GUI-managed {key[:-1] if key.endswith('s') else key} id {row['id']!r} conflicts with base Legion config"
    )


def merge_registry_into_raw(raw: Mapping[str, Any], registry: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(raw))
    normalized = validate_registry(registry)
    accounts = normalized["accounts"]
    role_bindings = normalized["role_bindings"]

    for account in accounts:
        runtime, auth, executor = _runtime_and_auth(account)
        _merge_unique_table(merged, "runtimes", runtime, runtime=True)
        _merge_unique_table(merged, "auth_profiles", auth)
        _merge_unique_table(merged, "executors", executor)

    roles = merged.setdefault("roles", [])
    if not isinstance(roles, list):
        raise AccountRegistryError("roles must be a list in base Legion config")

    role_index = {
        str(row.get("id")): row
        for row in roles
        if isinstance(row, dict) and row.get("id") is not None
    }
    for binding in role_bindings:
        role = role_index.get(binding["id"])
        if role is None:
            role = {
                "id": binding["id"],
                "objective": binding["objective"],
                "permissions": ["repo_read", "repo_write", "shell"],
            }
            roles.append(role)
            role_index[binding["id"]] = role
        elif not str(role.get("objective") or "").strip():
            role["objective"] = binding["objective"]

        field = "allowed_executors" if binding["mode"] == "allowed" else "preferred_executors"
        existing = [str(item) for item in role.get(field, [])]
        role[field] = list(dict.fromkeys([*binding["executors"], *existing]))

    for account in accounts:
        for role_id in account["roles"]:
            role = role_index.get(role_id)
            if role is None:
                role = {
                    "id": role_id,
                    "objective": f"Execute the {role_id} responsibility.",
                    "permissions": ["repo_read", "repo_write", "shell"],
                }
                roles.append(role)
                role_index[role_id] = role
            field = "allowed_executors" if account["role_mode"] == "allowed" else "preferred_executors"
            current = [str(item) for item in role.get(field, [])]
            role[field] = list(dict.fromkeys([account["id"], *current]))

    known_role_ids = set(role_index)
    for account in accounts:
        if not account["bound_roles_only"]:
            continue
        bound = set(account["roles"])
        for binding in role_bindings:
            if account["id"] in binding["executors"]:
                bound.add(binding["id"])
        for role_id in known_role_ids - bound:
            role = role_index[role_id]
            current = [str(item) for item in role.get("forbidden_executors", [])]
            role["forbidden_executors"] = list(dict.fromkeys([account["id"], *current]))

    return merged


def merge_companion_file(raw: Mapping[str, Any], config_path: Path) -> dict[str, Any]:
    path = companion_path(config_path)
    if not path.is_file():
        return copy.deepcopy(dict(raw))
    return merge_registry_into_raw(raw, load_registry(path))


def install_legion_config_loader(module: Any) -> None:
    """Wrap legion_config.load once so GUI-managed accounts compose with TOML."""
    if getattr(module, "_HLC_ACCOUNT_REGISTRY_INSTALLED", False):
        return
    original_load = module.load

    def load_with_managed_accounts(path: Path):
        path = Path(path)
        text = path.read_text(encoding="utf-8")
        raw = module.toml_loads(text)
        merged = merge_companion_file(raw, path)
        return module.load_dict(merged)

    module._HLC_ORIGINAL_LOAD = original_load
    module.load = load_with_managed_accounts
    module._HLC_ACCOUNT_REGISTRY_INSTALLED = True
