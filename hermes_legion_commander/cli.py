"""Unified command-line entry point for Hermes Legion Commander."""
from __future__ import annotations

import argparse
import sys

from . import checkpoint_competition, doctor, github_health, model_council, repo_graph, supervisor, token_cost, workflow_governance


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander",
        description=(
            "Hermes-supervised orchestration for Codex CLI and Claude Code in three modes: "
            "collaborating (council), competing (convergence), and alternating (stop-at-version handoff)."
        ),
    )
    sub = p.add_subparsers(dest="workflow", required=True)
    sub.add_parser("collaborating", help="Collaborative council: multiple roles per version, auto-continues the range")
    sub.add_parser("competing", help="Competitive convergence: two candidates per version, judged and converged")
    sub.add_parser("alternating", help="Rapid alternate: one worker implements one version, then stops and hands off to the other")
    sub.add_parser("supervisor", help="Configure or invoke the Hermes Agent supervisor")
    sub.add_parser("doctor", help="Verify tools, authentication, profiles, configs, repository, and roadmap")
    sub.add_parser("repo-graph", help="Build/query the local repository knowledge graph")
    sub.add_parser("token-cost", help="Estimate prompt tokens and shadow API-equivalent cost offline")
    sub.add_parser("github-health", help="Gate a patch on GitHub Actions workflow success and Dependabot alerts")
    sub.add_parser("governance", help="Risk escalation, PR readiness, regression memory, branch cleanup, and dashboard")
    # Deprecated legacy aliases (kept working): council -> collaborating, checkpoint -> competing.
    sub.add_parser("council", help=argparse.SUPPRESS)
    sub.add_parser("checkpoint", help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        parser().print_help()
        return 0
    workflow = args.pop(0)
    if workflow in {"collaborating", "council"}:
        if workflow == "council":
            print("warning: 'council' is deprecated; use 'collaborating'", file=sys.stderr)
        return model_council.main(args)
    if workflow in {"competing", "checkpoint"}:
        if workflow == "checkpoint":
            print("warning: 'checkpoint' is deprecated; use 'competing'", file=sys.stderr)
        return checkpoint_competition.main(args)
    if workflow == "alternating":
        return model_council.alternate_main(args)
    if workflow == "supervisor":
        return supervisor.main(args)
    if workflow == "doctor":
        return doctor.main(args)
    if workflow == "repo-graph":
        return repo_graph.cli_main(args)
    if workflow == "token-cost":
        return token_cost.cli_main(args)
    if workflow == "github-health":
        return github_health.cli_main(args)
    if workflow == "governance":
        return workflow_governance.cli_main(args)
    parser().error(f"unknown workflow: {workflow}")
    return 2


def legacy_main(argv: list[str] | None = None) -> int:
    print("warning: 'legion-commander' is deprecated; use 'hermes-legion-commander'", file=sys.stderr)
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
