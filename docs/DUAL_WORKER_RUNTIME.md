# Codex CLI and Claude Code runtime

Hermes Legion Commander uses exactly two native workers:

- Codex CLI
- Claude Code

The cooperative council may assign different logical roles to each worker. Checkpoint competition assigns **every role to both workers** so they produce independent candidates and independent comparative judgements.

Provider-private conversation state is never assumed to be shared. Cross-worker continuity comes from the supervisor-owned `shared-context/` memory, immutable per-stage snapshots, patches, result artifacts, and explicit judge reports.

Campaigns created by an incompatible earlier runtime should not be resumed. Start a new run ID so worker ownership, command templates, model/effort metadata, and shared-memory events remain auditable.
