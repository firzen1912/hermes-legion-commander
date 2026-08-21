# Hermes Legion Commander

Hermes Legion Commander (HLC) is a **repository-agnostic, provider-neutral multi-agent software-engineering orchestrator**. It separates model/provider choice from authentication, runtime, account identity, agent role, and campaign topology so the same orchestration layer can use Codex, Claude, API-hosted models, local runtimes, or future adapters without hard-coding a provider into a job.

The primary architecture on `main` is the generic **Legion v2** engine. The older `collaborating`, `competing`, and `alternating` workflows remain available as compatibility presets.

## Current `main` status

Current `main` includes:

- provider/model/runtime/auth/executor separation;
- OAuth, native CLI, and API-key-reference authentication;
- multiple independent accounts per provider;
- a localhost-only account/role GUI;
- arbitrary user-defined or Commander-defined roles;
- resource- and quota-aware executor selection;
- arbitrary campaign DAGs and explicit human gates;
- provider-diverse independent review preference;
- a reviewed 86-skill baseline shared across executors;
- a provider-neutral repository-data egress safeguard;
- legacy council/competition/alternating workflows and governance utilities.

> **Release-state note:** `pyproject.toml`, `__version__`, and the checked-in wheel still identify the last packaged release as **1.7.4**. The `dist/hermes_legion_commander-1.7.4-py3-none-any.whl` artifact was built before the Legion v2, repository-safeguard, and multi-account GUI changes now present on `main`. To use the current architecture, install from source. The 1.7.4 release manifest remains historical release evidence, not proof that the post-release `main` features are in that wheel.

## Architecture

The generic orchestration model is:

```text
Provider
  ↓
Model
  ↓
Runtime / transport
  ↓
Authentication profile
  ↓
Executor (one schedulable account/runtime/model resource)
  ↓
Agent assignment
  ↓
Role contract
  ↓
Campaign DAG
```

These layers are intentionally independent. A provider does not imply a role, and one provider does not imply one account.

For example:

```text
OpenAI
├── Codex OAuth account A → executor: codex-simulation
├── Codex OAuth account B → executor: codex-capability
└── API project C         → executor: openai-overflow

Anthropic
├── Claude account A      → executor: claude-debt
└── Claude account B      → executor: claude-review

Moonshot / compatible API
└── Kimi account          → executor: kimi-research

Qwen / compatible API
└── Qwen account          → executor: qwen-validation
```

The same executor may be assigned to different roles in different campaigns unless the configuration deliberately restricts it.

## Core capabilities

### Provider- and runtime-neutral execution

`RuntimeAdapter` supports CLI, API, local, MCP-shim, and custom execution boundaries. `Executor` binds one model/runtime/authentication resource into a schedulable unit. The scheduler evaluates capabilities, skills, authentication kind, role restrictions, resource state, priority, provider preference, and account preference before dispatch.

The built-in local account GUI currently provides presets for:

- **Codex CLI**;
- **Claude Code**;
- **OpenAI-compatible chat APIs**;
- **Anthropic Messages API**;
- **custom CLI adapters**.

Kimi, Qwen, and other services can use the OpenAI-compatible adapter when the selected endpoint exposes the compatible schema. They are not hard-coded into the role system.

### Arbitrary roles and elastic teams

Roles are data, not enums. A `RoleContract` can define:

- objective and responsibilities;
- required capabilities and skills;
- allowed authentication kinds;
- preferred or forbidden providers;
- preferred, allowed, or forbidden executors;
- required executor labels;
- permissions;
- acceptance criteria;
- independence requirements;
- minimum and maximum agent count.

Team policy can be:

```text
user_defined
commander_defined
hybrid
```

`hybrid` keeps Commander-generated defaults while allowing user-supplied role contracts to replace or extend them.

### Campaign DAGs

The generic campaign engine supports these node kinds:

```text
agent
review
validation
synthesis
checkpoint
human_gate
```

A campaign may provide its own DAG in configuration, or Commander can build a conservative fan-out/fan-in graph from planned assignments.

Worker success is semantic, not just process-level. Stage output must contain an explicit:

```text
PASS
BLOCKED
NEEDS_HUMAN
```

A zero exit code without an explicit verdict is treated as `BLOCKED`.

### Resource-aware scheduling

Executor budgets support subscription allowance, API budgets, cooldowns, and parallelism limits. Current default resource thresholds include:

| Boundary | Default |
| --- | ---: |
| Protected subscription reserve | 25% |
| No new unattended work at or below | 35% remaining |
| Session checkpoint | 4 percentage points consumed |
| Session hard stop | 5 percentage points consumed |
| Daily unattended maximum | 10 percentage points |

The generic doctrine also defines checkpoint-oriented development phases, cheap-to-expensive validation, bounded failed attempts, bounded long runs, and protected human actions. Repository-local instructions may make these constraints stricter.

## Install current `main`

### Prerequisites

Required for the Python package:

```text
Python 3.11+
git
```

Install only the model runtimes you actually intend to use, for example:

```text
codex
claude
```

API-only or local/custom deployments do not require both CLIs.

Optional dependencies/tools:

- Python `keyring` for storing API secrets in the OS credential store from the GUI;
- `pipx` when the skill installer must install the reviewed Graphify version;
- Hermes Agent when using the optional `supervisor` workflow.

### Linux / macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Optional keyring support:

```bash
python -m pip install keyring
```

Verify the installed command surface:

```bash
hermes-legion-commander --help
```

The old `legion-commander` executable remains a deprecated compatibility alias.

## Quick start: GUI-managed accounts

The easiest current setup path is the local control panel:

```bash
hermes-legion-commander gui --config config/legion.toml
```

If `config/legion.toml` does not exist, the GUI creates a minimal base configuration. By default it binds only to:

```text
http://127.0.0.1:8765/
```

Use another loopback port or suppress browser launch with:

```bash
hermes-legion-commander gui --config config/legion.toml --port 8766
hermes-legion-commander gui --config config/legion.toml --no-browser
```

The GUI keeps hand-authored TOML separate from managed account state:

```text
config/
├── legion.toml
└── legion.accounts.json
```

`legion.accounts.json` stores account metadata and **secret references only**. It must not contain raw API keys. On POSIX systems it is written with mode `0600`.

See [docs/ACCOUNT_CONTROL_PANEL.md](docs/ACCOUNT_CONTROL_PANEL.md) for the full control-panel model and security boundary.

## Multiple OAuth / native CLI accounts

There is no hard account-count limit in the GUI registry.

### Multiple Codex accounts

Each GUI-managed Codex account receives an isolated `CODEX_HOME`:

```text
~/.local/share/hermes-legion-commander/accounts/<account-id>/codex
```

This allows separate native Codex login state for multiple ChatGPT/Codex accounts using the same `codex` binary.

### Multiple Claude accounts

Each GUI-managed Claude account receives an isolated `CLAUDE_CONFIG_DIR`:

```text
~/.local/share/hermes-legion-commander/accounts/<account-id>/claude
```

This allows multiple Claude Code account profiles using the same `claude` binary.

The configured email is an **operator-facing label**, not provider-verified identity and not a credential. Native runtime status remains the authority for the authenticated account.

From the CLI, OAuth/native accounts can also be managed directly:

```bash
hermes-legion-commander legion accounts list --config config/legion.toml

hermes-legion-commander legion accounts login \
  --config config/legion.toml \
  --executor codex-simulation

hermes-legion-commander legion accounts status \
  --config config/legion.toml \
  --executor codex-simulation
```

## API authentication

API accounts use references rather than raw credentials:

```text
env:MY_PROVIDER_API_KEY
file:/protected/path/provider.key
keyring:hermes-legion-commander/account-id
```

The GUI can write an API key into the OS keyring when the optional Python `keyring` package is installed. The generated TOML/JSON stores only the keyring reference.

API endpoints must use HTTPS, except explicit localhost development endpoints.

Removing an account from the GUI does **not** destroy provider-native OAuth state or delete an OS-keyring secret. Credential destruction is intentionally a separate human-controlled operation.

## Role-to-account mapping

GUI-managed accounts can be associated with arbitrary roles in two main ways:

- **Allowed pool** — restrict a role to selected executor accounts.
- **Preferred pool** — prefer selected accounts while preserving eligible failover.

An account can also be marked **Use this account only for assigned roles**, which prevents that executor from being selected for unrelated known roles.

Examples:

```text
simulation-and-demo
├── codex-simulation-a
└── codex-simulation-b

independent-review
├── claude-review-a
└── claude-review-b
```

This makes multiple accounts useful as independent resources rather than accidental global login swaps.

## Generic Legion CLI

Generate the current configuration example:

```bash
hermes-legion-commander legion config-example
```

Validate configuration:

```bash
hermes-legion-commander legion validate --config config/legion.toml
```

Inspect auth profiles, runtimes, and executors:

```bash
hermes-legion-commander legion roster --config config/legion.toml
```

Plan without executing workers:

```bash
hermes-legion-commander legion plan \
  --config config/legion.toml \
  --repo /path/to/target-repo \
  --objective "Implement the next bounded work packet and independently review it" \
  --state-dir /path/to/state
```

Run a campaign:

```bash
hermes-legion-commander legion run \
  --config config/legion.toml \
  --repo /path/to/target-repo \
  --objective "Implement the next bounded work packet and independently review it" \
  --state-dir /path/to/state
```

`legion run` fails closed if the reviewed skill baseline is not synchronized for all effective skill roots.

## Reviewed skill baseline

Every generic Legion executor must expose a skill root. Commander maintains an exact reviewed profile containing **86 skills**, sourced from pinned revisions of seven skill packs plus **Graphify 0.9.43**.

Inspect the reviewed manifest:

```bash
hermes-legion-commander skills manifest
```

Check configured executor roots:

```bash
hermes-legion-commander skills check --config config/legion.toml
```

Install/synchronize the reviewed baseline:

```bash
hermes-legion-commander skills install --config config/legion.toml
```

The installer:

- stages pinned upstream sources;
- restricts Caveman to the reviewed subset;
- enforces Graphify `0.9.43`;
- rejects missing or unexpected skills;
- rejects forbidden hook patterns;
- preserves `.system` directories;
- backs up existing roots before synchronization.

At most **three relevant skills** are activated for a single generic campaign stage. Runtimes without native skill discovery receive selected `SKILL.md` context directly.

## Repository-data safeguard

HLC installs a provider-neutral model-bound data guard at the executor invocation boundary. The policy is based on **execution/data trust**, not provider nationality or brand.

Default behavior:

| Execution boundary | Default mode | Data ceiling | Secret handling |
| --- | --- | --- | --- |
| Local runtime | `standard` | `confidential` | redact |
| CLI/native cloud worker | `standard` | `confidential` | redact |
| HTTP/API runtime | `strict` | `internal` | block |
| Explicit external-cloud / remote backend | `strict` | `internal` | block |
| Lockdown | `lockdown` | `public` | block |

The guard provides defense in depth through:

- high-confidence secret detection in model-bound text;
- redaction in standard mode;
- fail-closed blocking in strict/lockdown mode;
- prompt-size ceilings;
- safe repository-context envelopes;
- traversal, absolute-path, symlink, binary, and oversized-file rejection;
- data classifications: `public < internal < confidential < restricted`;
- default denial of common credential/key stores and `.git` material;
- content-free audit records containing hashes and policy decisions rather than source text or secrets.

Default safeguard audit location:

```text
~/.hermes-legion-commander/safeguard-audit.jsonl
```

Override with `LEGION_SAFEGUARD_AUDIT`.

A strict remote CLI/custom/MCP adapter must declare an actually enforced filesystem sandbox with `sandbox_enforced = true`. Merely setting a working directory is not treated as isolation.

See [docs/REPO_DATA_SAFEGUARD.md](docs/REPO_DATA_SAFEGUARD.md) for configuration examples and limitations.

## GUI security model

The account control panel is intentionally local-only. It:

- binds to `127.0.0.1`;
- validates the Host header;
- requires a per-process CSRF token for writes;
- rejects non-local browser origins;
- sets `Cache-Control: no-store`;
- uses a restrictive Content Security Policy;
- uses no CDN or remote JavaScript;
- uses no cookies or browser local storage;
- caps JSON request bodies at 1 MiB;
- never writes raw API keys into Legion TOML/JSON.

This is a local configuration interface, not a remote management service.

## Protected actions and human gates

The generic Legion doctrine treats these as protected actions:

```text
merge
push
deploy
tag
publish
release
credential_change
hardware_operation
live_actuation
```

The generic campaign engine does not silently cross explicit human gates. Repository-local instructions may impose additional restrictions.

For high-value proprietary repositories, the data guard should be combined with real OS/container isolation, network egress controls, repository-scoped credentials, and human review of what may leave the local environment.

## Legacy compatibility workflows

The historical workflows remain implemented for compatibility and for existing configurations. They are **presets**, not the primitive architecture of Legion v2.

| Command | Purpose |
| --- | --- |
| `collaborating` | collaborative council workflow |
| `competing` | competitive candidate/convergence workflow |
| `alternating` | sequential worker handoff workflow |

Deprecated aliases remain:

```text
council    → collaborating
checkpoint → competing
```

Legacy configuration examples currently checked into `config/` are:

```text
config/checkpoint_competition.example.toml
config/hermes_supervisor.example.toml
config/model_council.example.toml
config/model_council.multi-provider.example.toml
```

Useful compatibility documentation includes:

- [docs/ALTERNATING_MODE.md](docs/ALTERNATING_MODE.md)
- [docs/CHECKPOINT_COMPETITION.md](docs/CHECKPOINT_COMPETITION.md)
- [docs/CROSS_VALIDATION.md](docs/CROSS_VALIDATION.md)
- [docs/GOAL_CONTRACT.md](docs/GOAL_CONTRACT.md)
- [docs/HANDOFF_SCHEMA.md](docs/HANDOFF_SCHEMA.md)

Some older documents describe the historical two-worker Codex/Claude path. Treat those as compatibility documentation; use `hermes-legion-commander legion ...` and the account control panel for the provider-neutral architecture.

## Other command surfaces

The unified CLI also retains these supporting tools:

| Command | Role |
| --- | --- |
| `supervisor` | optional Hermes Agent supervisor/profile workflow |
| `doctor` | environment, authentication, configuration, and repository diagnostics |
| `repo-graph` | local repository knowledge-graph operations |
| `token-cost` | offline prompt-token and shadow API-equivalent cost estimates |
| `github-health` | GitHub Actions / dependency-health gating utilities |
| `governance` | risk escalation, PR readiness, regression memory, branch cleanup, dashboard |
| `routing` / `router` | legacy routing-context helpers; prefer generic `legion plan` for new routing |

See [docs/GITHUB_HEALTH.md](docs/GITHUB_HEALTH.md) and [docs/BRANCH_PR_WORKFLOW.md](docs/BRANCH_PR_WORKFLOW.md) for the corresponding legacy/governance workflows.

## Repository layout

Important current files:

```text
hermes-legion-commander/
├── hermes_legion_commander/
│   ├── legion.py                 # provider-neutral contracts, registry, planner
│   ├── legion_config.py          # generic Legion TOML loader
│   ├── executor_runtime.py       # CLI/API/custom invocation boundary
│   ├── campaign_engine.py        # arbitrary campaign DAG execution
│   ├── doctrine.py               # repository-agnostic resource/development doctrine
│   ├── skill_profile.py          # exact reviewed 86-skill baseline
│   ├── repo_data_safeguard.py    # repository/model egress protection
│   ├── account_registry.py       # GUI-managed multi-account companion registry
│   ├── control_panel.py          # localhost OAuth/API account GUI
│   ├── cli.py                    # unified command entry point
│   ├── doctor.py
│   ├── supervisor.py
│   ├── model_council.py          # legacy collaborating/alternating path
│   └── checkpoint_competition.py # legacy competing path
├── config/                       # checked-in legacy/example configs
├── docs/
├── profiles/
├── scripts/
├── tests/
├── dist/                         # last packaged 1.7.4 artifact; predates current main
├── pyproject.toml                # package metadata currently still 1.7.4
└── README.md
```

A user-created `config/legion.toml` and its GUI companion `config/legion.accounts.json` are runtime configuration, not part of the checked-in example set on current `main`.

## Release and verification state

The repository currently has two distinct notions of state:

1. **Last packaged release: 1.7.4**
   - represented by `pyproject.toml`, `__version__`, `dist/`, `RELEASE-MANIFEST.json`, and `SHA256SUMS.txt`;
   - the release manifest records the validation performed for that historical artifact.

2. **Current `main` source**
   - includes Legion v2, multi-account OAuth/API support, the GUI, and repository safeguard added after the 1.7.4 artifact was built;
   - should be installed from source until a new release is cut and its wheel/manifest are regenerated.

Do not cite the 1.7.4 wheel's historical validation as validation of post-release `main` changes.

## Design principles

HLC follows a few durable rules:

- **Provider is not role.** Scheduling policy should not hard-code a job to a vendor.
- **Account is a resource.** Multiple logins for the same provider are independent executors.
- **Available capacity is not authorization to spend it.** Resource budgets and cooldowns constrain unattended work.
- **Builder self-report is provisional.** Completion should be independently reviewed when required.
- **Secrets do not belong in prompts or configuration.** Use native auth or secret references.
- **Prompt filtering is not a sandbox.** Strict remote CLI work requires a real isolation boundary.
- **Human gates remain sovereign.** Protected actions are not implied by an agent's ability to perform them.
- **Legacy modes are compatibility presets.** The generic Legion registry, roles, and DAG are the extensible foundation.

## Recommended first workflow

For a fresh checkout of current `main`:

```bash
# 1. Install current source
python -m pip install -e .

# 2. Open the local multi-account control panel
hermes-legion-commander gui --config config/legion.toml

# 3. Add OAuth/native or API accounts and role bindings in the GUI
#    then validate the composed configuration
hermes-legion-commander legion validate --config config/legion.toml

# 4. Synchronize the reviewed skill baseline
hermes-legion-commander skills install --config config/legion.toml

# 5. Inspect the resulting executor pool
hermes-legion-commander legion roster --config config/legion.toml

# 6. Plan before executing
hermes-legion-commander legion plan \
  --config config/legion.toml \
  --repo /path/to/target-repo \
  --objective "Implement a bounded change and independently review it"

# 7. Run only after the plan, account status, skills, and data boundary are acceptable
hermes-legion-commander legion run \
  --config config/legion.toml \
  --repo /path/to/target-repo \
  --objective "Implement a bounded change and independently review it"
```

For detailed account security and repository-data controls, start with:

- [Multi-account OAuth/API control panel](docs/ACCOUNT_CONTROL_PANEL.md)
- [Repository Data Safeguard](docs/REPO_DATA_SAFEGUARD.md)
