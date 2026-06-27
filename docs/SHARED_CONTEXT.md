# Provider-neutral shared context

Codex CLI and Claude Code do not share private provider conversation state. Hermes Legion Commander maintains explicit cross-agent memory under each run's `shared-context/` directory.

## Contents

- `CONTEXT.md`: immutable worker contract.
- `campaign-brief.md`: job metadata and initial repository snapshot.
- `shared-memory.md`: chronological normalized stage summaries.
- `stage-index.jsonl`: machine-readable event catalog.
- `artifacts/`: normalized prior stage outputs.
- `events/`: hashes, runtime/session metadata, Git snapshots, scope assessment, and routing decisions.
- `scope-routing-ledger.jsonl`: compact history of request scope and selected worker/model/effort.
- `scope-routing-summary.md`: prompt-injected summary of recent scope routing decisions.
- `routing-decisions/`: auditable per-stage routing decisions.
- `repo-map/graph.json`: local repository knowledge graph with file, symbol, call, import, entrypoint, community, confidence/provenance, hotspot, and language facts.
- `repo-map/graph.html`: interactive local graph browser with search, filters, neighbors, and hotspots.
- `repo-map/GRAPH_REPORT.md` / `repo-map/REPO_MAP.md`: concise navigation report for workers and the supervisor.
- `repo-map/wiki/`: markdown pages for communities and indexed files.
- `repo-context-pack.md`: task-specific start-here file selection generated from the current stage request.
- `runtime.json`: current worker invocation metadata.

Before each worker starts, Commander creates a stage-local `worker-context/` snapshot. The prompt embeds the same memory excerpt and the CLI receives that snapshot through its allowed directory mechanism. Commander applies read-only permissions and hashes the snapshot before and after execution. A worker mutation fails the stage but cannot corrupt canonical memory.

This shares explicit facts and artifacts only. Hidden reasoning, private system prompts, provider-side caches, and native conversation history are not transferable. Material decisions must be written to stage output or repository artifacts.

## Usage and roadmap learning

Each completed stage now refreshes `learning-ledger.jsonl`, `learning-summary.json`, and `prompt-lessons.md` under the run's `shared-context/` directory. These files track observed Codex/Claude token usage, cost fields when emitted by the CLI, prompt/output hashes, quality signals, and roadmap-version alignment. Later prompts include `prompt-lessons.md` so workers can reduce repeated context and cite exact prior artifacts. See `docs/USAGE_LEARNING.md`.



## Scope-aware routing

Before each native CLI invocation, Commander classifies the current request from observable facts: prompt size, task type, risk flags, version span, roadmap references, and repository roadmap availability. It then compares the scope bucket against previous `learning-ledger.jsonl` and `scope-routing-ledger.jsonl` rows from the state directory. Only configured agents and configured model names are eligible; the router does not invent provider models.

Each stage receives:

- `scope-assessment.json` — deterministic request scope and base effort.
- `routing-decision.json` — selected worker, runtime, configured model, selected effort, candidate scores, matched historical rows, and rationale.
- `shared-context/routing-decisions/<stage>.json` — canonical copy for later runs.

Checkpoint competition keeps agent switching disabled for isolated competitor lanes, but still allows effort adjustment when the request scope and risk permit it. Council mode may reorder the requested worker when learned outcomes show another configured worker/runtime has better evidence for the same scope.

## Repository graph navigation

Before prompt assembly, Commander refreshes a local Graphify-style repository knowledge graph and injects a task-specific `repo-context-pack.md`. The pack gives Codex CLI and Claude Code likely files, symbols, tests, docs, config, schema, communities, entrypoints, confidence/provenance hints, and immediate graph neighbors so they can navigate from a compact map instead of reading the repository broadly. Scope assessment also records `repo_facts` so effort selection can account for repository size, language mix, and multimodal assets. See `docs/REPO_GRAPH.md`.


## Competitive cross-validation artifacts

Checkpoint competition publishes candidate patches and evaluation data before opponent review. Codex reviews Claude's candidate and Claude reviews Codex's candidate under `shared-context/artifacts/cross-validation/`. Each owner then receives the opponent findings through shared context and runs a bounded polish pass in its own candidate worktree before judging.

After convergence, Commander publishes `shared-context/artifacts/converged/converged.json` and both workers run read-only final verification, producing `shared-context/artifacts/final-verification/*.json`. These artifacts are used by the final human review and are never treated as automatic merge, release, or deployment approval.


## Prompt preflight and shadow API cost

Before each Codex CLI or Claude Code invocation, Commander writes `prompt-preflight.json` in the stage directory and appends `shared-context/prompt-preflight-ledger.jsonl`. These records estimate input tokens, expected output tokens, subscription/OAuth auth mode, and API-equivalent USD cost before the prompt is sent. `prompt-cost-summary.md` is included in later shared context so agents can reduce repeated context and choose cheaper evidence paths. See `docs/TOKEN_COST_PREFLIGHT.md`.

## Anchored truth preflight

Every worker-context snapshot includes `ANCHORED_TRUTH.md` and an
`anchored-truth/` artifact directory. Commander refreshes these files immediately
before building a Codex CLI or Claude Code prompt, so roadmap implementation
stages see the current Git state, hashed anchor-source excerpts, hard-boundary
lines, and non-blocking GitHub/Dependabot status before the task text. See
`docs/ANCHORED_TRUTH.md`.

