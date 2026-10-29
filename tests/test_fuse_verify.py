"""Shared local/CI check-runner scenarios."""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
from _support import git

REPOSITORY = Path(__file__).parents[1]
SCRIPT_DIR = REPOSITORY / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_checks: Any = importlib.import_module("fuse_checks")
fuse_candidate: Any = importlib.import_module("fuse_candidate")
fuse_config: Any = importlib.import_module("fuse_config")
fuse_record: Any = importlib.import_module("fuse_record")
fuse_verify: Any = importlib.import_module("fuse_verify")


def make_repository(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = tmp_path / "consumer"
    spec = root / ".specs" / "001-demo"
    spec.mkdir(parents=True)
    (root / ".fuse").mkdir()
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (spec / "requirements.md").write_text(
        "### R1 Change application behavior\n", encoding="utf-8"
    )
    (spec / "design.md").write_text("### D-Core\nsatisfies: R1\n", encoding="utf-8")
    (spec / "tasks.md").write_text(
        "- [ ] T1 Implement [id:T1 deps: impl:R1,D-Core]\n", encoding="utf-8"
    )
    config = tomllib.loads(
        (REPOSITORY / ".fuse" / "config.toml").read_text(encoding="utf-8")
    )
    config["checks"] = {
        "a_tests": {
            "command": [sys.executable, "-c", "print('tests')"],
            "prerequisites": [],
            "inputs": ["app.py"],
            "pass_criteria": "exit_code == 0",
        },
        "b_lint": {
            "command": [sys.executable, "-c", "print('lint')"],
            "prerequisites": ["a_tests"],
            "inputs": ["app.py"],
            "pass_criteria": "exit_code == 0",
        },
        "c_docs": {
            "command": [sys.executable, "-c", "print('docs')"],
            "prerequisites": ["b_lint"],
            "inputs": ["app.py"],
            "pass_criteria": "exit_code == 0",
        },
        "z_other": {
            "command": [sys.executable, "-c", "print('other')"],
            "prerequisites": [],
            "inputs": ["app.py"],
            "pass_criteria": "exit_code == 0",
        },
    }
    fuse_config.validate_config(config)
    config_text = (REPOSITORY / ".fuse" / "config.toml").read_text(encoding="utf-8")
    config_prefix = config_text.split("[checks.pytest]", maxsplit=1)[0]
    check_sections: list[str] = []
    for check_id, definition in config["checks"].items():
        check_sections.extend(
            [
                f"[checks.{check_id}]",
                f"command = {json.dumps(definition['command'])}",
                f"prerequisites = {json.dumps(definition['prerequisites'])}",
                f"inputs = {json.dumps(definition['inputs'])}",
                f"pass_criteria = {json.dumps(definition['pass_criteria'])}",
                "",
            ]
        )
    (root / ".fuse" / "config.toml").write_text(
        config_prefix + "\n".join(check_sections), encoding="utf-8"
    )
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Fuse test")
    git(root, "config", "user.email", "fuse@example.invalid")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "fixture")
    return root, spec, config


def test_runner_obeys_check_dag_and_continues_unrelated_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec, config = make_repository(tmp_path)
    started: list[str] = []
    outcomes = {"a_tests": "fail", "z_other": "pass"}

    def fake_run_check(
        _root: Path,
        _spec: Path,
        _config: dict[str, Any],
        _record: dict[str, Any],
        candidate_id: str,
        check_id: str,
    ) -> dict[str, Any]:
        started.append(check_id)
        outcome = outcomes[check_id]
        return {
            "check_id": check_id,
            "candidate_id": candidate_id,
            "outcome": outcome,
            "exit_code": 1 if outcome == "fail" else 0,
            "summary": f"{check_id} {outcome}",
            "evidence_id": f"evd-{check_id}",
        }

    monkeypatch.setattr(fuse_checks, "run_check", fake_run_check)
    report = fuse_verify.run_verification(
        root,
        spec,
        config,
        run_id="run_0123456789abcdef0123456789abcdef",
    )
    expected_record = fuse_record.new_run_record(spec.name, config)
    expected_candidate = fuse_candidate.capture_candidate(
        expected_record, root, spec, config
    )

    results = {item["check_id"]: item for item in report["results"]}
    assert started == ["a_tests", "z_other"]
    assert report["candidate_id"] == expected_candidate["candidate_id"]
    assert report["selected_checks"] == [
        "a_tests",
        "b_lint",
        "c_docs",
        "z_other",
    ]
    assert results["a_tests"]["outcome"] == "fail"
    assert results["b_lint"]["outcome"] == "blocked"
    assert results["b_lint"]["blocked_by"] == ["a_tests"]
    assert results["c_docs"]["outcome"] == "blocked"
    assert results["c_docs"]["blocked_by"] == ["b_lint"]
    assert results["z_other"]["outcome"] == "pass"
    assert report["ok"] is False


def test_selecting_one_check_adds_prerequisites_but_not_unrelated_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spec, config = make_repository(tmp_path)
    started: list[str] = []

    def fake_run_check(
        _root: Path,
        _spec: Path,
        _config: dict[str, Any],
        _record: dict[str, Any],
        candidate_id: str,
        check_id: str,
    ) -> dict[str, Any]:
        started.append(check_id)
        return {
            "check_id": check_id,
            "candidate_id": candidate_id,
            "outcome": "pass",
            "exit_code": 0,
            "summary": "ok",
            "evidence_id": f"evd-{check_id}",
        }

    monkeypatch.setattr(fuse_checks, "run_check", fake_run_check)
    report = fuse_verify.run_verification(root, spec, config, check_ids=["b_lint"])

    assert report["selected_checks"] == ["a_tests", "b_lint"]
    assert started == ["a_tests", "b_lint"]
    assert report["ok"] is True


def test_local_and_ci_refer_to_one_verification_entrypoint() -> None:
    config = fuse_config.load_config(REPOSITORY / ".fuse" / "config.toml")
    workflow = (REPOSITORY / ".github" / "workflows" / "verify.yml").read_text(
        encoding="utf-8"
    )
    assert "python skills/scripts/fuse_verify.py" in workflow
    assert "pull_request:" in workflow
    assert "merge_group:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0" in workflow
    assert "astral-sh/setup-uv@07a047e608db84b3d8c06cd9a7c7dcc9c355c18" in workflow
    assert (
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in workflow
    )
    assert "pytest -q" not in workflow
    assert "ruff check" not in workflow
    assert not any(
        f"--check-id {check_id}" in workflow for check_id in config["checks"]
    )
    assert {"traceability", "planner"} <= set(config["checks"])


def test_config_setup_docs_use_plugin_lock_and_explicit_consumer_paths() -> None:
    guide = (REPOSITORY / "docs" / "user-guide.md").read_text(encoding="utf-8")
    assert (
        'uv run --project "$PLUGIN_ROOT" --directory "$PLUGIN_ROOT" --locked' in guide
    )
    assert (
        'python "$PLUGIN_ROOT/skills/scripts/fuse_config.py" init "$REPO_ROOT"' in guide
    )
    assert 'validate "$REPO_ROOT/.fuse/config.toml"' in guide


def test_cli_reports_json_and_fails_when_a_check_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, spec, config = make_repository(tmp_path)

    def unavailable(
        _root: Path,
        _spec: Path,
        _config: dict[str, Any],
        _record: dict[str, Any],
        candidate_id: str,
        check_id: str,
    ) -> dict[str, Any]:
        return {
            "check_id": check_id,
            "candidate_id": candidate_id,
            "outcome": "unavailable",
            "exit_code": None,
            "summary": "tool is unavailable",
            "evidence_id": "evd-placeholder",
        }

    monkeypatch.setattr(fuse_checks, "run_check", unavailable)
    status = fuse_verify.main(
        [
            "--repo-root",
            str(root),
            "--spec-dir",
            str(spec),
            "--check-id",
            "a_tests",
            "--run-id",
            "run_0123456789abcdef0123456789abcdef",
        ]
    )
    report = cast(dict[str, Any], json.loads(capsys.readouterr().out))

    assert status == 1
    assert report["results"][0]["outcome"] == "unavailable"
    assert report["record_persisted"] is False
