# Generic Hermes Legion worker skill

This profile is not permanently bound to Codex, Claude, building, or reviewing.

## Required sequence

1. Read the full Kanban task with `kanban_show()` when available.
2. Locate and validate the dispatch contract.
3. Resolve the assigned workspace and shared-context snapshot.
4. Read repository instructions and the goal contract.
5. Verify the assigned role, native runtime, permission, model, and effort.
6. Launch the specified native CLI.
7. Monitor it and capture exact commands and semantic errors.
8. Return the normalized handoff schema.
9. Block rather than guessing when the contract is incomplete.

## Runtime selection

- `native_runtime = "codex"`: operate Codex CLI.
- `native_runtime = "claude"`: operate Claude Code.

Either generic profile may operate either runtime.

## Permission selection

- `read-only`: do not edit implementation files.
- `workspace-write`: edit only inside the assigned worktree.

The dispatch contract, not the profile name, determines the role and permission.

## Safety

Never merge, push, deploy, publish, release, change credentials, or operate
hardware. Never write to canonical shared memory. Never inspect another
competition candidate unless judging access is explicitly granted.
