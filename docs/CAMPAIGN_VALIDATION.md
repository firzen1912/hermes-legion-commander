# Campaign validation

Version 1.7.0 runs roadmap campaigns through Codex CLI and Claude Code.

For every version, the supervisor records:

- roadmap plan review;
- current research;
- literature review;
- prototype output;
- code-polish output;
- security-assurance output;
- changed paths by stage;
- focused test and experiment commands;
- exit codes, durations, stdout, and stderr;
- result artifacts;
- an idempotent iteration note;
- a human-reviewable roadmap proposal.

Full campaigns run every logical role for every version. Staggered campaigns preserve the rolling pattern while using Codex for both forward research and implementation and Claude for the assurance lane.
