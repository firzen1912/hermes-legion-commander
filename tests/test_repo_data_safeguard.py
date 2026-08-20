from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_legion_commander.repo_data_safeguard import (
    DataClass,
    DataGuardViolation,
    GuardMode,
    SecretAction,
    TrustTier,
    build_context_envelope,
    build_guarded_invoke,
    enforce_runtime_boundary,
    policy_for_executor,
    protect_text,
)


def _executor(*, executor_meta=None, runtime_meta=None, transport="cli"):
    runtime = SimpleNamespace(id="runtime", transport=transport, metadata=runtime_meta or {})
    executor = SimpleNamespace(id="worker", runtime="runtime", metadata=executor_meta or {})
    registry = SimpleNamespace(executors={"worker": executor}, runtimes={"runtime": runtime})
    return registry, executor, runtime


def test_standard_guard_redacts_high_confidence_secrets() -> None:
    _, executor, runtime = _executor()
    policy = policy_for_executor(executor, runtime)
    protected = protect_text(
        "password=correct-horse-battery-staple sk-proj-abcdefghijklmnopqrstuv",
        policy,
    )
    assert policy.mode is GuardMode.STANDARD
    assert policy.trust_tier is TrustTier.CONTROLLED_CLOUD
    assert policy.secret_action is SecretAction.REDACT
    assert "correct-horse-battery-staple" not in protected.text
    assert "sk-proj-" not in protected.text
    assert protected.redaction_count == 2


def test_api_defaults_to_strict_external_cloud() -> None:
    _, executor, runtime = _executor(transport="api")
    policy = policy_for_executor(executor, runtime)
    assert policy.mode is GuardMode.STRICT
    assert policy.trust_tier is TrustTier.EXTERNAL_CLOUD
    assert policy.max_data_class is DataClass.INTERNAL
    with pytest.raises(DataGuardViolation, match="blocks egress"):
        protect_text("api_key=abcdefghijklmnop123456", policy)


def test_remote_cli_strict_requires_enforced_sandbox() -> None:
    _, executor, runtime = _executor(
        runtime_meta={"data_guard": {"remote_backend": True, "mode": "strict"}}
    )
    policy = policy_for_executor(executor, runtime)
    with pytest.raises(DataGuardViolation, match="sandbox_enforced=true"):
        enforce_runtime_boundary(runtime, policy)


def test_remote_cli_strict_accepts_declared_sandbox() -> None:
    _, executor, runtime = _executor(
        runtime_meta={
            "sandbox_enforced": True,
            "data_guard": {"remote_backend": True, "mode": "strict"},
        }
    )
    policy = policy_for_executor(executor, runtime)
    enforce_runtime_boundary(runtime, policy)
    assert policy.sandbox_enforced is True


def test_context_envelope_blocks_sensitive_traversal_binary_and_symlink(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "safe.py").write_text("answer = 42\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=abcdefghijklmnop123456\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"\x00\x01\x02")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)

    _, executor, runtime = _executor()
    policy = policy_for_executor(executor, runtime)
    envelope = build_context_envelope(
        repo,
        ["src/safe.py", ".env", "../outside.txt", "binary.bin", "link.txt"],
        policy,
    )
    assert envelope.included_paths == ("src/safe.py",)
    assert set(envelope.blocked_paths) == {
        ".env",
        "../outside.txt",
        "binary.bin",
        "link.txt",
    }
    assert "answer = 42" in envelope.text


def test_classification_ceiling_blocks_confidential_context_for_strict_cloud(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "proprietary.py").write_text("valuable = True\n", encoding="utf-8")
    _, executor, runtime = _executor(
        transport="api",
        executor_meta={
            "data_guard": {
                "classification_rules": {"src/proprietary.py": "confidential"}
            }
        },
    )
    policy = policy_for_executor(executor, runtime)
    envelope = build_context_envelope(repo, ["src/proprietary.py"], policy)
    assert envelope.included_paths == ()
    assert envelope.blocked_paths == ("src/proprietary.py",)


def test_guarded_invoke_never_passes_secret_to_underlying_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("LEGION_SAFEGUARD_AUDIT", str(audit))
    registry, _, _ = _executor()
    seen = {}

    def original(registry, executor_id, prompt, *, cwd, timeout=900, env=None, active_skills=()):
        seen["prompt"] = prompt
        return "ok"

    guarded = build_guarded_invoke(original)
    result = guarded(
        registry,
        "worker",
        "password=correct-horse-battery-staple",
        cwd=tmp_path,
    )
    assert result == "ok"
    assert "correct-horse-battery-staple" not in seen["prompt"]
    row = json.loads(audit.read_text(encoding="utf-8").strip())
    assert row["outcome"] == "redacted"
    assert "correct-horse-battery-staple" not in audit.read_text(encoding="utf-8")


def test_strict_guard_blocks_before_underlying_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEGION_SAFEGUARD_AUDIT", str(tmp_path / "audit.jsonl"))
    registry, _, _ = _executor(transport="api")
    called = False

    def original(*args, **kwargs):
        nonlocal called
        called = True
        return "unsafe"

    guarded = build_guarded_invoke(original)
    with pytest.raises(DataGuardViolation):
        guarded(registry, "worker", "secret=abcdefghijklmnop123456", cwd=tmp_path)
    assert called is False


def test_prompt_size_is_fail_closed() -> None:
    _, executor, runtime = _executor(executor_meta={"data_guard": {"max_prompt_bytes": 10}})
    policy = policy_for_executor(executor, runtime)
    with pytest.raises(DataGuardViolation, match="policy limit"):
        protect_text("01234567890", policy)
