from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_legion_commander.account_registry import (
    AccountRegistryError,
    companion_path,
    load_registry,
    merge_registry_into_raw,
    normalize_account,
    save_registry,
)
from hermes_legion_commander.control_panel import _html, ensure_base_config


def codex(account_id: str, email: str) -> dict:
    return {
        "id": account_id,
        "preset": "codex-cli",
        "provider": "openai",
        "auth_kind": "oauth",
        "email": email,
        "model": "default",
        "roles": ["simulation"],
        "role_mode": "allowed",
        "bound_roles_only": True,
        "budget": {"subscription_remaining_percent": 100, "max_parallel": 1},
        "data_guard": {"mode": "standard", "max_data_class": "confidential"},
    }


def claude(account_id: str, email: str) -> dict:
    row = codex(account_id, email)
    row.update({"preset": "claude-code", "provider": "anthropic", "roles": ["review"]})
    return row


def api_account(account_id: str, provider: str = "moonshot") -> dict:
    return {
        "id": account_id,
        "preset": "openai-compatible-api",
        "provider": provider,
        "auth_kind": "api_key",
        "model": "kimi-k3",
        "endpoint": "https://api.example.test/v1/chat/completions",
        "secret_ref": f"env:{account_id.upper().replace('-', '_')}_KEY",
        "budget": {"max_parallel": 1},
    }


def test_multiple_codex_accounts_keep_independent_oauth_homes():
    registry = {"schema_version": 1, "accounts": [codex("codex-a", "a@example.com"), codex("codex-b", "b@example.com")], "role_bindings": []}
    merged = merge_registry_into_raw({}, registry)
    assert len(merged["runtimes"]) == 1
    assert len(merged["executors"]) == 2
    homes = [row["environment"]["CODEX_HOME"] for row in merged["auth_profiles"]]
    assert len(set(homes)) == 2


def test_multiple_claude_accounts_keep_independent_config_dirs():
    registry = {"schema_version": 1, "accounts": [claude("claude-a", "a@example.com"), claude("claude-b", "b@example.com")], "role_bindings": []}
    merged = merge_registry_into_raw({}, registry)
    dirs = [row["environment"]["CLAUDE_CONFIG_DIR"] for row in merged["auth_profiles"]]
    assert len(set(dirs)) == 2


def test_api_accounts_default_to_strict_external_cloud_guard():
    row = normalize_account(api_account("kimi-review"))
    assert row["data_guard"]["mode"] == "strict"
    assert row["data_guard"]["trust_tier"] == "external_cloud"
    assert row["data_guard"]["max_data_class"] == "internal"
    assert row["data_guard"]["secret_action"] == "block"


def test_raw_api_key_is_rejected():
    row = api_account("qwen-api", "qwen")
    row["secret_ref"] = "sk-this-is-a-raw-secret-value-that-must-not-be-stored"
    with pytest.raises(AccountRegistryError):
        normalize_account(row)


def test_codex_cli_api_key_mode_is_rejected_in_favor_of_api_adapter():
    row = codex("codex-api", "a@example.com")
    row["auth_kind"] = "api_key"
    row["secret_ref"] = "env:OPENAI_API_KEY"
    with pytest.raises(AccountRegistryError):
        normalize_account(row)


def test_role_binding_can_pin_multiple_accounts():
    registry = {
        "schema_version": 1,
        "accounts": [codex("codex-sim", "a@example.com"), claude("claude-sim", "b@example.com")],
        "role_bindings": [{"id": "simulation", "objective": "Own simulation.", "mode": "allowed", "executors": ["codex-sim", "claude-sim"]}],
    }
    merged = merge_registry_into_raw({}, registry)
    role = next(row for row in merged["roles"] if row["id"] == "simulation")
    assert role["allowed_executors"] == ["codex-sim", "claude-sim"]


def test_registry_file_contains_secret_reference_not_secret_value(tmp_path: Path):
    path = tmp_path / "legion.accounts.json"
    save_registry(path, {"schema_version": 1, "accounts": [api_account("kimi-api")], "role_bindings": []})
    text = path.read_text(encoding="utf-8")
    assert "env:KIMI_API_KEY" in text
    assert load_registry(path)["accounts"][0]["secret_ref"] == "env:KIMI_API_KEY"
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_gui_uses_companion_file_and_local_assets_only(tmp_path: Path):
    config = ensure_base_config(tmp_path / "config" / "legion.toml")
    assert companion_path(config) == tmp_path / "config" / "legion.accounts.json"
    html = _html("csrf-test-token")
    assert "Add agent account" in html
    assert "Add role binding" in html
    assert "https://cdn." not in html
    assert "localStorage" not in html
    assert "csrf-test-token" in html
