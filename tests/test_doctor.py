from __future__ import annotations

import json
from pathlib import Path

from hermes_legion_commander import doctor


def test_doctor_parser_accepts_json(tmp_path):
    args = doctor.parser().parse_args([
        "--repo-root", str(tmp_path),
        "--skip-auth",
        "--json",
    ])
    assert args.json
    assert args.skip_auth


def test_toml_check_rejects_invalid(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("not toml [", encoding="utf-8")
    check, data = doctor._toml_check("config:test", path)
    assert not check.ok
    assert data is None


def test_collect_reports_missing_target(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    result = doctor.collect(
        repo_root=tmp_path,
        target_repo=tmp_path / "missing",
        council_config=None,
        checkpoint_config=None,
        skip_auth=True,
    )
    assert not result["ok"]
    assert any(row["name"] == "target-repository" for row in result["checks"])
