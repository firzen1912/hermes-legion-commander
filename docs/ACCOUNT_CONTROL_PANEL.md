# Multi-account OAuth/API control panel

Hermes Legion Commander includes a local browser control panel for registering
multiple independent accounts per provider and assigning them to arbitrary roles.

```bash
hermes-legion-commander gui --config config/legion.toml
```

The server binds only to `127.0.0.1`. It opens the local browser by default.
Use `--no-browser` when you want to open the printed localhost URL manually.

## Model

The GUI keeps these concepts separate:

```text
Provider
  -> runtime adapter
  -> auth profile / account
  -> executor
  -> role binding
```

A provider is not a role and one provider is not one account. For example:

```text
OpenAI / Codex CLI
  - codex-simulation   -> ChatGPT email A -> simulation role
  - codex-capability   -> ChatGPT email B -> capability role

Anthropic / Claude Code
  - claude-debt        -> Claude email C -> debt role
  - claude-review      -> Claude email D -> review role

Moonshot-compatible API
  - kimi-research      -> API secret ref -> research role

Qwen-compatible API
  - qwen-validation    -> API secret ref -> validation role
```

There is no hard account-count limit in the registry.

## Storage

The GUI does not rewrite the hand-authored Legion TOML. For a base file:

```text
config/legion.toml
```

it creates:

```text
config/legion.accounts.json
```

The package loader composes the companion registry into the TOML at runtime.
On POSIX systems the companion file is written with mode `0600`.

The companion file may contain account IDs, provider/runtime choice, an email as
an operator-facing label, model/endpoint, role mappings, quota inputs, repository
data-guard policy, and secret references. It must never contain a raw API key.

## OAuth / native subscription accounts

### Codex

The Codex preset creates a per-account home under:

```text
~/.local/share/hermes-legion-commander/accounts/<account-id>/codex
```

and sets it through `CODEX_HOME`. The runtime uses `codex login` and
`codex login status`.

### Claude Code

The Claude preset creates:

```text
~/.local/share/hermes-legion-commander/accounts/<account-id>/claude
```

and sets it through `CLAUDE_CONFIG_DIR`. The runtime uses `claude auth login`
and `claude auth status`.

The configured email is only a human-readable label. The provider-native status
output remains the authority for which account is actually authenticated.

## API accounts

Two built-in API adapters are available:

- OpenAI-compatible chat API
- Anthropic Messages API

The OpenAI-compatible adapter can be used for providers such as Kimi or Qwen when
the selected endpoint exposes the compatible chat schema. Provider name,
endpoint, and model remain user-configurable; no provider is hard-coded as a role.

API secrets support only references:

```text
env:KIMI_API_KEY
file:/protected/path/key
keyring:hermes-legion-commander/kimi-research
```

If Python `keyring` is installed, the GUI exposes **Store key**. The pasted value
is posted only to the loopback process, written into the OS keyring, cleared from
the form, and never persisted in TOML or JSON.

## Role bindings

A role binding can be:

- `allowed`: restrict the role to the selected GUI executor pool plus any
  executors already explicitly authorized in the base TOML.
- `preferred`: use the selected executors first while retaining eligible failover.

An account can also enable **Use this account only for assigned roles**. Commander
then adds that executor to `forbidden_executors` for every other known role.

Multiple accounts may be assigned to the same role, enabling pools such as two
Codex implementation accounts or two independent Claude reviewers.

## Repository data safeguard

Every GUI-created account receives a data-guard policy.

CLI subscription/OAuth presets default to:

```text
mode: standard
trust_tier: controlled_cloud
secret_action: redact
max_data_class: confidential
```

External API presets default to:

```text
mode: strict
trust_tier: external_cloud
secret_action: block
max_data_class: internal
```

The same safeguard applies to Codex, Claude, Kimi, Qwen, custom providers, and
future adapters. Policy is based on the data/execution boundary, not provider
nationality.

Moving a remote CLI to strict/lockdown mode requires a runtime-declared enforced
filesystem sandbox, preventing prompt filtering from being mistaken for file
system isolation.

## Local web security

The control panel binds only to loopback, validates the Host header, requires a
per-process CSRF token for writes, rejects non-local browser origins, sets
`Cache-Control: no-store` and a restrictive Content Security Policy, uses no CDN,
remote JavaScript, cookies, browser local storage, or telemetry, caps JSON request
bodies at 1 MiB, and never logs request bodies.

Removing an account from the GUI does **not** delete the provider-native OAuth
credential store or OS-keyring secret. Credential destruction remains a separate
human-controlled action.

## Example workflow

```bash
hermes-legion-commander gui --config config/legion.toml

hermes-legion-commander legion validate --config config/legion.toml
hermes-legion-commander skills check --config config/legion.toml

hermes-legion-commander legion plan \
  --config config/legion.toml \
  --objective "Implement and independently review the next bounded work packet"
```
