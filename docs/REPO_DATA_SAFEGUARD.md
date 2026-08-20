# Repository Data Safeguard

Hermes Legion Commander applies a provider-neutral repository-data egress guard
before every call through `executor_runtime.invoke_executor`.

The safeguard is based on the **execution and data boundary**, not the vendor or
country of origin of a model. Kimi, Qwen, OpenAI, Anthropic, local models, and
future runtimes therefore use the same mechanism.

## Default policy

| Execution boundary | Default mode | Default data ceiling | Secret handling |
| --- | --- | --- | --- |
| Local runtime | `standard` | `confidential` | redact |
| CLI/native cloud worker | `standard` | `confidential` | redact |
| HTTP/API runtime | `strict` | `internal` | block |
| Explicit `external_cloud` / remote backend | `strict` | `internal` | block |
| `lockdown` | `lockdown` | `public` | block |

`standard` is still active for Codex and Claude. There is no vendor bypass.

## What is enforced

1. **Prompt egress filtering**
   - high-confidence private keys and common API/token formats are detected;
   - `standard` mode redacts them before the runtime receives the prompt;
   - `strict` and `lockdown` block the invocation entirely;
   - oversized prompts fail closed instead of silently expanding data exposure.

2. **Safe repository context envelopes**
   - only explicitly requested relative paths are read;
   - path traversal, absolute paths, symlinks, binary files, oversized files,
     and oversized aggregate context are rejected;
   - common credential/key stores and `.git` data are classified `restricted`;
   - user classification rules can raise proprietary paths to `confidential` or
     `restricted`;
   - the same secret scanner runs on file contents before model-bound context is
     created.

3. **Remote CLI boundary**
   - strict remote CLI/custom/MCP runtimes are refused unless the runtime
     declares `sandbox_enforced = true`;
   - setting a working directory is not treated as a filesystem sandbox;
   - this prevents prompt sanitization from being misrepresented as protection
     against an agent that can directly read the host repository.

4. **Audit without sensitive content**
   - decisions are logged as hashes, policy, outcome, and redaction count;
   - prompts, source code, credentials, account email, and auth-profile IDs are
     never written to the safeguard audit log.

The default audit location is:

```text
~/.hermes-legion-commander/safeguard-audit.jsonl
```

Override it with `LEGION_SAFEGUARD_AUDIT`.

## Strict cloud configuration

API runtimes are strict automatically. You can make the boundary explicit:

```toml
[[executors]]
id = "qwen-review"
provider = "qwen"
model = "qwen3-coder"
runtime = "qwen-api"
auth_profile = "qwen-api-key"

[executors.metadata.data_guard]
mode = "strict"
trust_tier = "external_cloud"
remote_backend = true
max_data_class = "internal"
secret_action = "block"
allow_globs = [
  "src/**",
  "tests/**",
  "docs/public/**",
]

[executors.metadata.data_guard.classification_rules]
"src/proprietary/**" = "confidential"
"docs/customer/**" = "restricted"
```

A Kimi/Moonshot API executor can use the same policy:

```toml
[[executors]]
id = "kimi-research"
provider = "moonshot"
model = "kimi"
runtime = "moonshot-api"
auth_profile = "moonshot-api-key"

[executors.metadata.data_guard]
mode = "strict"
trust_tier = "external_cloud"
remote_backend = true
max_data_class = "internal"
```

Nothing in the implementation special-cases these vendors. Any API runtime gets
the strict default unless explicitly configured otherwise.

## Codex and Claude

Native Codex/Claude CLI workers remain under `standard` protection by default,
which sanitizes model-bound prompt data. To require a stricter repository
boundary for a cloud-backed CLI, mark it remote and provide an actual sandbox:

```toml
[[runtimes]]
id = "codex-sandboxed"
provider = "openai"
transport = "cli"
command = ["your-sandbox-wrapper", "codex", "exec", "{prompt}"]
skill_roots = ["~/.codex/skills"]

[runtimes.metadata]
sandbox_enforced = true

[runtimes.metadata.data_guard]
remote_backend = true
mode = "strict"
```

Without `sandbox_enforced = true`, Commander refuses a strict remote CLI
invocation. The flag is an assertion by the runtime adapter; the wrapper must
actually enforce the filesystem/network boundary.

## Data classifications

The ordered classifications are:

```text
public < internal < confidential < restricted
```

Default path classification is `internal`. Known credential/key paths are
`restricted`. Add project-specific rules whenever source or documentation is
more sensitive:

```toml
[executors.metadata.data_guard.classification_rules]
"src/algorithms/**" = "confidential"
"contracts/**" = "restricted"
"docs/public/**" = "public"
```

`strict` defaults to an `internal` ceiling, so the first two paths above cannot
be packaged into model context for that executor.

## Security model

This guard is defense in depth. It is intended to reduce accidental repository
egress and make the model boundary explicit. It does not claim that a vendor,
model, or cloud is inherently safe or unsafe. For high-value proprietary
repositories, combine strict policy with a real OS/container sandbox, network
egress controls, repository-scoped credentials, and human review of what data
is permitted to leave the local environment.
