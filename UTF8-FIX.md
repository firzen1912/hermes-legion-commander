# UTF-8 worker transport fix in v0.7.1

Hermes Legion Commander v0.7.0 used Python subprocess text mode for native worker
stdin. On Windows, that can encode prompts with a legacy code page. Codex CLI
requires UTF-8 stdin and rejected roadmap prompts containing characters such as
em dashes with:

```text
Failed to read prompt from stdin: input is not valid UTF-8
```

Version 0.7.1 encodes worker prompts explicitly as UTF-8 bytes and decodes
Codex/Claude output as UTF-8. The fix applies to both `council` and
`checkpoint` workers.

Runs created by v0.7.0 remain resumable after installing v0.7.1 because the
state schema and campaign structure did not change.
