"""Provider-neutral repository-data egress guard for Legion executors."""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class DataGuardViolation(RuntimeError):
    """Fail-closed repository-data safeguard decision."""


class GuardMode(StrEnum):
    STANDARD = "standard"
    STRICT = "strict"
    LOCKDOWN = "lockdown"


class TrustTier(StrEnum):
    LOCAL = "local"
    CONTROLLED_CLOUD = "controlled_cloud"
    EXTERNAL_CLOUD = "external_cloud"
    RESTRICTED = "restricted"


class SecretAction(StrEnum):
    REDACT = "redact"
    BLOCK = "block"


class DataClass(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value: str | int | "DataClass") -> "DataClass":
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper()]


DEFAULT_DENY_GLOBS = (
    ".git/**", "**/.git/**", ".env", ".env.*", "**/.env", "**/.env.*",
    ".aws/**", "**/.aws/**", ".ssh/**", "**/.ssh/**", ".gnupg/**",
    "**/.gnupg/**", ".npmrc", "**/.npmrc", ".pypirc", "**/.pypirc",
    ".netrc", "**/.netrc", "auth.json", "**/auth.json", "credentials.json",
    "**/credentials.json", "*.pem", "**/*.pem", "*.key", "**/*.key",
    "*.p12", "**/*.p12", "*.pfx", "**/*.pfx", "*.jks", "**/*.jks",
    "*.kdbx", "**/*.kdbx", "*.tfstate", "**/*.tfstate", "*.tfstate.*",
    "**/*.tfstate.*",
)
SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.defaults", ".env.dist"}

_SECRET_PATTERNS = (
    ("private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----", re.DOTALL)),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("sk-token", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b")),
)
_ASSIGNMENT_SECRET = re.compile(
    r"""(?ix)\b(api[_-]?key|secret|client[_-]?secret|password|passwd|
    access[_-]?token|refresh[_-]?token|private[_-]?token)(\s*[:=]\s*)
    (["']?)([A-Za-z0-9+/=_\-.]{12,})\3"""
)


@dataclass(frozen=True)
class DataGuardPolicy:
    mode: GuardMode
    trust_tier: TrustTier
    remote_backend: bool
    secret_action: SecretAction
    max_data_class: DataClass
    max_prompt_bytes: int
    max_context_file_bytes: int
    max_context_total_bytes: int
    allow_globs: tuple[str, ...] = ()
    deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS
    classification_rules: tuple[tuple[str, DataClass], ...] = ()
    sandbox_enforced: bool = False
    audit_enabled: bool = True

    @property
    def strict(self) -> bool:
        return self.mode in {GuardMode.STRICT, GuardMode.LOCKDOWN}


@dataclass(frozen=True)
class ProtectedText:
    text: str
    redaction_count: int
    secret_kinds: tuple[str, ...]
    original_sha256: str
    protected_sha256: str


@dataclass(frozen=True)
class ContextEnvelope:
    text: str
    included_paths: tuple[str, ...]
    blocked_paths: tuple[str, ...]
    redaction_count: int
    sha256: str


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    raise DataGuardViolation(f"expected string/list policy value, got {type(value).__name__}")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        if value.casefold() in {"1", "true", "yes", "on"}:
            return True
        if value.casefold() in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def policy_for_executor(executor: Any, runtime: Any) -> DataGuardPolicy:
    runtime_meta = _mapping(getattr(runtime, "metadata", {}))
    table = _mapping(runtime_meta.get("data_guard"))
    table.update(_mapping(_mapping(getattr(executor, "metadata", {})).get("data_guard")))
    transport = str(getattr(runtime, "transport", "")).casefold()
    remote = _as_bool(table.get("remote_backend"), transport == "api")
    trust = TrustTier(str(table["trust_tier"])) if "trust_tier" in table else (
        TrustTier.EXTERNAL_CLOUD if remote else
        TrustTier.LOCAL if transport == "local" else TrustTier.CONTROLLED_CLOUD
    )
    mode = GuardMode(str(table["mode"])) if "mode" in table else (
        GuardMode.STRICT if remote else GuardMode.STANDARD
    )
    secret_action = SecretAction(str(table["secret_action"])) if "secret_action" in table else (
        SecretAction.BLOCK if mode != GuardMode.STANDARD else SecretAction.REDACT
    )
    ceiling = {
        GuardMode.STANDARD: DataClass.CONFIDENTIAL,
        GuardMode.STRICT: DataClass.INTERNAL,
        GuardMode.LOCKDOWN: DataClass.PUBLIC,
    }[mode]
    rules_raw = table.get("classification_rules", {})
    if rules_raw and not isinstance(rules_raw, Mapping):
        raise DataGuardViolation("classification_rules must be a glob->classification table")
    rules = tuple((str(k), DataClass.parse(v)) for k, v in _mapping(rules_raw).items())
    custom_deny = _strings(table.get("deny_globs"))
    return DataGuardPolicy(
        mode=mode,
        trust_tier=trust,
        remote_backend=remote,
        secret_action=secret_action,
        max_data_class=DataClass.parse(table.get("max_data_class", ceiling.name)),
        max_prompt_bytes=int(table.get("max_prompt_bytes", 512 * 1024 if mode == GuardMode.STANDARD else 128 * 1024)),
        max_context_file_bytes=int(table.get("max_context_file_bytes", 512 * 1024 if mode == GuardMode.STANDARD else 256 * 1024)),
        max_context_total_bytes=int(table.get("max_context_total_bytes", 4 * 1024 * 1024 if mode == GuardMode.STANDARD else 1024 * 1024)),
        allow_globs=_strings(table.get("allow_globs")),
        deny_globs=tuple(dict.fromkeys((*DEFAULT_DENY_GLOBS, *custom_deny))),
        classification_rules=rules,
        sandbox_enforced=_as_bool(table.get("sandbox_enforced"), _as_bool(runtime_meta.get("sandbox_enforced"))),
        audit_enabled=_as_bool(table.get("audit_enabled"), True),
    )


def protect_text(text: str, policy: DataGuardPolicy) -> ProtectedText:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > policy.max_prompt_bytes:
        raise DataGuardViolation(f"prompt is {len(raw)} bytes; policy limit is {policy.max_prompt_bytes} bytes")
    protected = text
    kinds: list[str] = []
    count = 0
    for kind, pattern in _SECRET_PATTERNS:
        matches = list(pattern.finditer(protected))
        if not matches:
            continue
        kinds.extend([kind] * len(matches))
        if policy.secret_action == SecretAction.BLOCK:
            raise DataGuardViolation(f"high-confidence secret detected in model-bound text ({kind}); strict policy blocks egress")
        protected, n = pattern.subn(f"[REDACTED:{kind}]", protected)
        count += n
    matches = list(_ASSIGNMENT_SECRET.finditer(protected))
    if matches:
        kinds.extend(["credential-assignment"] * len(matches))
        if policy.secret_action == SecretAction.BLOCK:
            raise DataGuardViolation("credential assignment detected in model-bound text; strict policy blocks egress")
        protected, n = _ASSIGNMENT_SECRET.subn(
            lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED:credential]{m.group(3)}",
            protected,
        )
        count += n
    return ProtectedText(
        protected, count, tuple(sorted(set(kinds))),
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(protected.encode("utf-8")).hexdigest(),
    )


def _matches(path: str, patterns: Iterable[str]) -> bool:
    path = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def classify_path(path: str, policy: DataGuardPolicy) -> DataClass:
    normalized = path.replace("\\", "/")
    if _matches(normalized, policy.deny_globs) and Path(normalized).name.casefold() not in SAFE_ENV_EXAMPLES:
        return DataClass.RESTRICTED
    for pattern, classification in policy.classification_rules:
        if fnmatch.fnmatchcase(normalized, pattern):
            return classification
    return DataClass.INTERNAL


def _safe_file(repo: Path, relative: str) -> tuple[Path, str]:
    given = Path(relative)
    if not relative or "\x00" in relative or given.is_absolute() or ".." in given.parts:
        raise DataGuardViolation(f"unsafe repository path: {relative!r}")
    normalized = given.as_posix().lstrip("./")
    root = repo.resolve()
    cursor = root
    for part in Path(normalized).parts:
        cursor /= part
        if cursor.is_symlink():
            raise DataGuardViolation(f"symlinked repository context is forbidden: {normalized}")
    resolved = (root / normalized).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DataGuardViolation(f"repository path escapes root: {relative!r}") from exc
    if not resolved.is_file():
        raise DataGuardViolation(f"repository context path is not a regular file: {normalized}")
    return resolved, normalized


def build_context_envelope(repo: Path, paths: Iterable[str], policy: DataGuardPolicy) -> ContextEnvelope:
    included, blocked, chunks = [], [], []
    total = redactions = 0
    for requested in paths:
        try:
            file_path, normalized = _safe_file(repo, str(requested))
            classification = classify_path(normalized, policy)
            if classification > policy.max_data_class:
                raise DataGuardViolation(f"{normalized} exceeds data-class ceiling")
            if policy.allow_globs and not _matches(normalized, policy.allow_globs):
                raise DataGuardViolation(f"{normalized} is outside allow_globs")
            if file_path.stat().st_size > policy.max_context_file_bytes:
                raise DataGuardViolation(f"{normalized} exceeds per-file context limit")
            data = file_path.read_bytes()
            if b"\x00" in data[:8192]:
                raise DataGuardViolation(f"binary repository context is forbidden: {normalized}")
            protected = protect_text(
                data.decode("utf-8", errors="strict"),
                replace(policy, max_prompt_bytes=max(policy.max_prompt_bytes, policy.max_context_file_bytes)),
            )
            rendered = f"## FILE: {normalized}\n{protected.text}\n"
            size = len(rendered.encode("utf-8"))
            if total + size > policy.max_context_total_bytes:
                raise DataGuardViolation("context envelope exceeds total byte limit")
            chunks.append(rendered)
            included.append(normalized)
            total += size
            redactions += protected.redaction_count
        except (DataGuardViolation, UnicodeDecodeError):
            blocked.append(str(requested))
    text = "\n".join(chunks)
    return ContextEnvelope(text, tuple(included), tuple(blocked), redactions, hashlib.sha256(text.encode()).hexdigest())


def enforce_runtime_boundary(runtime: Any, policy: DataGuardPolicy) -> None:
    transport = str(getattr(runtime, "transport", "")).casefold()
    if policy.strict and policy.remote_backend and transport in {"cli", "custom", "mcp"} and not policy.sandbox_enforced:
        raise DataGuardViolation(
            "strict remote-backend executor uses a host-filesystem runtime without "
            "sandbox_enforced=true; prompt filtering cannot prevent tool-driven repo reads"
        )


def _audit_path() -> Path:
    override = os.environ.get("LEGION_SAFEGUARD_AUDIT")
    return Path(os.path.expandvars(os.path.expanduser(override))) if override else (
        Path.home() / ".hermes-legion-commander" / "safeguard-audit.jsonl"
    )


def _audit(executor_id: str, runtime_id: str, policy: DataGuardPolicy, outcome: str, **fields: Any) -> None:
    if not policy.audit_enabled:
        return
    target = _audit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "executor": executor_id,
        "runtime": runtime_id,
        "mode": policy.mode.value,
        "trust_tier": policy.trust_tier.value,
        "remote_backend": policy.remote_backend,
        "outcome": outcome,
        **fields,
    }
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def guard_invocation(registry: Any, executor_id: str, prompt: str, cwd: Path) -> str:
    executor = getattr(registry, "executors", {}).get(executor_id)
    if executor is None:
        raise DataGuardViolation(f"unknown executor {executor_id!r}")
    runtime = getattr(registry, "runtimes", {}).get(getattr(executor, "runtime", ""))
    if runtime is None:
        raise DataGuardViolation(f"executor {executor_id!r} references unavailable runtime")
    policy = policy_for_executor(executor, runtime)
    runtime_id = str(getattr(runtime, "id", getattr(executor, "runtime", "")))
    prompt_hash = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    try:
        enforce_runtime_boundary(runtime, policy)
        if not Path(cwd).resolve().is_dir():
            raise DataGuardViolation(f"executor working directory is not a directory: {cwd}")
        protected = protect_text(prompt, policy)
        _audit(
            executor_id, runtime_id, policy,
            "redacted" if protected.redaction_count else "allowed",
            prompt_sha256=protected.original_sha256,
            protected_sha256=protected.protected_sha256,
            redaction_count=protected.redaction_count,
        )
        return protected.text
    except DataGuardViolation as exc:
        _audit(executor_id, runtime_id, policy, "blocked", prompt_sha256=prompt_hash, reason=str(exc))
        raise


def build_guarded_invoke(original: Any) -> Any:
    @functools.wraps(original)
    def guarded(
        registry: Any, executor_id: str, prompt: str, *, cwd: Path,
        timeout: int = 900, env: dict[str, str] | None = None,
        active_skills: Iterable[str] = (),
    ) -> Any:
        return original(
            registry, executor_id, guard_invocation(registry, executor_id, prompt, cwd),
            cwd=cwd, timeout=timeout, env=env, active_skills=active_skills,
        )
    setattr(guarded, "__legion_repo_data_guard__", True)
    return guarded


def install_executor_runtime_guard(module: Any) -> None:
    current = getattr(module, "invoke_executor", None)
    if current is None:
        raise DataGuardViolation("executor runtime has no invoke_executor boundary")
    if not getattr(current, "__legion_repo_data_guard__", False):
        module.invoke_executor = build_guarded_invoke(current)
