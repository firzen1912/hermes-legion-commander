"""Tests for rapid-alternate mode: worker alternation, handoff documents, run_alternate."""
import json
import subprocess
from pathlib import Path

from hermes_legion_commander import model_council as mc


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _alternate_repo(tmp_path, versions=("1", "2")):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    sections = "\n".join(f"### v{v} — feature {v}\nImplement feature {v} with tests.\n" for v in versions)
    (repo / "docs" / "roadmap.md").write_text(
        f"# Roadmap\n\n## Version-by-version implementation\n\n{sections}", encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    config = tmp_path / "c.toml"
    config.write_text(f'''[council]
repo = "{repo}"
state_dir = "{tmp_path / 'state'}"
roadmap_path = "docs/roadmap.md"
checks = [["true"]]
[roles]
roadmap_plan_reviewer = "codex"
researcher = "codex"
literature_reviewer = "claude"
prototyper = "codex"
code_polisher = "claude"
security_assurance = "claude"
[agents.codex]
runtime = "codex-cli"
provider = "openai"
command = ["codex", "exec", "-"]
[agents.claude]
runtime = "claude-code"
provider = "anthropic"
command = ["claude", "-p", "x"]
[research]
topics = ["x"]
literature_reviewer = "claude"
''')
    return config


# --- next_alternate_worker ----------------------------------------------

def test_next_alternate_worker_auto_two_agents():
    assert mc.next_alternate_worker("codex", ["claude", "codex"], None) == "claude"
    assert mc.next_alternate_worker("claude", ["claude", "codex"], None) == "codex"


def test_next_alternate_worker_explicit_handoff_wins():
    assert mc.next_alternate_worker("codex", ["claude", "codex", "gemini"], "gemini") == "gemini"


def test_next_alternate_worker_ambiguous_requires_explicit():
    try:
        mc.next_alternate_worker("codex", ["codex", "claude", "gemini"], None)
    except mc.CouncilError as exc:
        assert "handoff-to is required" in str(exc)
    else:
        raise AssertionError("ambiguous alternation was allowed without --handoff-to")


# --- build_handoff_document ---------------------------------------------

def test_handoff_document_ping_pong():
    md, summary = mc.build_handoff_document(
        version=51, worker="codex", handoff_to="claude", next_version=52,
        validation_passed=True, changed_count=7, tree_dirty=True,
        continuation_prompt="DO v52", has_next=True, config_hint="config/x.toml",
    )
    assert "v51 → v52" in md or "v51 \u2192 v52" in md
    assert "Next worker: **claude**" in md
    assert "--version 52 --worker claude --handoff-to codex" in md
    assert "DO v52" in md
    assert summary["handoff_to"] == "claude"
    assert summary["next_version"] == 52
    assert summary["has_next"] is True
    assert "Resume at v52 with claude" in summary["handoff_line"]


def test_handoff_document_final_version():
    md, summary = mc.build_handoff_document(
        version=57, worker="claude", handoff_to="codex", next_version=58,
        validation_passed=False, changed_count=3, tree_dirty=False,
        continuation_prompt="", has_next=False,
    )
    assert "Alternation complete" in md
    assert summary["has_next"] is False
    assert summary["handoff_to"] is None
    assert summary["next_version"] is None
    assert "alternation complete" in summary["handoff_line"].lower()


# --- run_alternate ------------------------------------------------------

def test_run_alternate_dry_run_stops_and_hands_off(tmp_path):
    config = _alternate_repo(tmp_path, versions=("1", "2"))
    cfg = mc.load_config(config)
    out = mc.run_alternate(cfg, version=1, worker="codex", handoff_to=None,
                           dry_run=True, run_id="t", to_version=2)
    assert out["mode"] == "rapid-alternate"
    assert out["stopped"] is True
    assert out["implemented_by"] == "codex"
    # auto-alternation to the other configured agent
    assert out["handoff"]["handoff_to"] == "claude"
    assert out["handoff"]["next_version"] == 2
    # handoff artifacts written
    assert Path(out["handoff_document"]).exists()
    summary = json.loads((Path(out["run_dir"]) / "handoff.json").read_text())
    assert summary["implemented_by"] == "codex"
    # review request written (no auto-merge)
    review = json.loads((Path(out["run_dir"]) / "review-request.json").read_text())
    assert review["mode"] == "rapid-alternate"


def test_run_alternate_final_version_reports_completion(tmp_path):
    config = _alternate_repo(tmp_path, versions=("1", "2"))
    cfg = mc.load_config(config)
    out = mc.run_alternate(cfg, version=2, worker="claude", handoff_to=None,
                           dry_run=True, run_id="t2", to_version=2)
    assert out["handoff"]["has_next"] is False
    assert out["handoff"]["handoff_to"] is None


def test_run_alternate_rejects_unknown_worker(tmp_path):
    config = _alternate_repo(tmp_path)
    cfg = mc.load_config(config)
    try:
        mc.run_alternate(cfg, version=1, worker="ghost", dry_run=True, run_id="t3")
    except mc.CouncilError as exc:
        assert "must name a configured agent" in str(exc)
    else:
        raise AssertionError("unknown worker was accepted")


def test_run_alternate_rejects_missing_version_section(tmp_path):
    config = _alternate_repo(tmp_path, versions=("1", "2"))
    cfg = mc.load_config(config)
    try:
        mc.run_alternate(cfg, version=99, worker="codex", dry_run=True, run_id="t4")
    except mc.CouncilError as exc:
        assert "v99" in str(exc)
    else:
        raise AssertionError("missing version section was accepted")
