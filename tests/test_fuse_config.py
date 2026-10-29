"""Public CLI tests for project policy configuration and initialization."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "skills" / "scripts" / "fuse_config.py"


def run_config(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        check=False,
        text=True,
    )


def create_config(project_root: Path) -> Path:
    project_root.mkdir(parents=True, exist_ok=True)
    if not (project_root / ".python-version").exists():
        (project_root / ".python-version").write_text("3.11\n", encoding="utf-8")
    if not (project_root / "pyproject.toml").exists():
        (project_root / "pyproject.toml").write_text(
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n',
            encoding="utf-8",
        )
    if not (project_root / "uv.lock").exists():
        (project_root / "uv.lock").write_text(
            'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
    result = run_config("init", str(project_root))
    assert result.returncode == 0, result.stderr
    return project_root / ".fuse" / "config.toml"


def replace_config_text(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def replace_section_value(path: Path, section: str, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    section_marker = f"[{section}]"
    start = content.index(section_marker)
    next_section = content.find("\n[", start + len(section_marker))
    end = next_section if next_section >= 0 else len(content)
    section_text = content[start:end]
    assert old in section_text
    updated = section_text.replace(old, new, 1)
    path.write_text(content[:start] + updated + content[end:], encoding="utf-8")


def test_init_reuses_project_package_manager_and_creates_valid_policy(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    package_files = {
        ".python-version": "3.11\n",
        "pyproject.toml": (
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n'
        ),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
    }
    for name, contents in package_files.items():
        (project_root / name).write_text(contents, encoding="utf-8")

    config_path = create_config(project_root)
    result = run_config("validate", str(config_path))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["roles"]["manager"]["model"] == "latest-astra"
    assert config["roles"]["worker"]["model"] == "latest-luna"
    assert config["roles"]["reviewer"]["reasoning_effort"] == "xhigh"
    assert config["roles"]["rca"]["model"] == "latest-astra"
    assert config["roles"]["rca_reviewer"]["reasoning_effort"] == "max"
    assert config["limits"]["review"]["max_repair_rounds_per_level"] == 2
    assert config["limits"]["rca"]["attempts_per_hard_blocker"] == 1
    assert config["budgets"]["elapsed_time_seconds"] == "unset"
    assert config["budgets"]["model_usage"]["limit"] == "unset"
    assert config["checks"]["pytest"]["command"][0] == "uv"
    assert config["toolchain"] == {
        "runtime": "python",
        "runtime_version": "3.11",
        "runtime_file": ".python-version",
        "package_manager": "uv",
        "manifest": "pyproject.toml",
        "lockfile": "uv.lock",
        "selection_policy": "reuse_existing",
        "restore_policy": "selected_tools_existing_version_and_lock_only",
        "allow_new_package_manager": False,
        "modify_consumer_package_files": False,
    }
    for name, contents in package_files.items():
        assert (project_root / name).read_text(encoding="utf-8") == contents
    for check in config["checks"].values():
        assert check["command"]
        assert "prerequisites" in check
        assert check["inputs"]
        assert check["pass_criteria"]


def test_repeated_init_does_not_overwrite_or_duplicate_configuration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n# operator policy\n",
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    result = run_config("init", str(project_root))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["created"] is False
    assert config_path.read_text(encoding="utf-8") == before
    assert len(tomllib.loads(before)["checks"]) >= 1


def test_unmatched_non_python_project_fails_without_writes(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    package_files = {
        "package.json": '{"name":"consumer"}\n',
        "package-lock.json": '{"lockfileVersion":3}\n',
    }
    for name, content in package_files.items():
        (project_root / name).write_text(content, encoding="utf-8")

    result = run_config("init", str(project_root))

    assert result.returncode != 0
    assert "toolchain assessment/config required" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()
    for name, content in package_files.items():
        assert (project_root / name).read_text(encoding="utf-8") == content


def test_default_template_requires_complete_python_uv_stack_before_writing(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    manifest = project_root / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )

    result = run_config("init", str(project_root))

    assert result.returncode != 0
    assert "toolchain assessment/config required" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()
    assert manifest.read_text(encoding="utf-8").startswith("[project]")


def test_explicit_template_is_validated_and_used_for_matching_stack(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    template_path = tmp_path / "assessed.toml"
    template_path.write_text(
        (Path(__file__).parents[1] / ".fuse" / "config.toml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    for name, contents in {
        ".python-version": "3.11\n",
        "pyproject.toml": (
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n'
        ),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
    }.items():
        path = project_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    result = run_config("init", str(project_root), "--template", str(template_path))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["created"] is True
    assert tomllib.loads((project_root / ".fuse" / "config.toml").read_text()) == (
        tomllib.loads(template_path.read_text(encoding="utf-8"))
    )


def test_invalid_explicit_template_fails_without_writing(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    template_path = tmp_path / "invalid.toml"
    template_path.write_text("schema_version = 1\n", encoding="utf-8")

    result = run_config("init", str(project_root), "--template", str(template_path))

    assert result.returncode != 0
    assert "schema" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()


def test_python_runtime_must_satisfy_manifest_and_lock_constraints(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    files = {
        ".python-version": "3.11\n",
        "pyproject.toml": ('[project]\nname = "consumer"\nrequires-python = ">=4"\n'),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=4"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
    }
    for name, contents in files.items():
        (project_root / name).write_text(contents, encoding="utf-8")

    result = run_config("init", str(project_root))

    assert result.returncode != 0
    assert "does not satisfy" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()


def test_python_lock_constraint_must_match_manifest(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    files = {
        ".python-version": "3.11\n",
        "pyproject.toml": (
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n'
        ),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=3.12"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
    }
    for name, contents in files.items():
        (project_root / name).write_text(contents, encoding="utf-8")

    result = run_config("init", str(project_root))

    assert result.returncode != 0
    assert "same requires-python" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()


def test_unavailable_template_package_manager_fails_without_writes(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    template_path = tmp_path / "unavailable-manager.toml"
    template_path.write_text(
        (Path(__file__).parents[1] / ".fuse" / "config.toml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    replace_section_value(
        template_path,
        "toolchain",
        'package_manager = "uv"',
        'package_manager = "fuse-missing-package-manager-031f7f"',
    )
    for name, contents in {
        ".python-version": "3.11\n",
        "pyproject.toml": (
            '[project]\nname = "consumer"\nrequires-python = ">=3.11"\n'
        ),
        "uv.lock": (
            'version = 1\nrevision = 5\nrequires-python = ">=3.11"\n'
            '\n[[package]]\nname = "consumer"\nversion = "0.1.0"\n'
        ),
    }.items():
        (project_root / name).write_text(contents, encoding="utf-8")

    result = run_config("init", str(project_root), "--template", str(template_path))

    assert result.returncode != 0
    assert "package manager" in result.stderr.lower()
    assert not (project_root / ".fuse" / "config.toml").exists()


def test_malformed_toml_is_rejected_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("schema_version =\n", encoding="utf-8")

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "toml" in result.stderr.lower()


def test_invalid_role_profile_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path, 'reasoning_effort = "xhigh"', 'reasoning_effort = "instant"'
    )

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "reasoning_effort" in result.stderr


def test_fallback_policy_requires_a_configured_fallback(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path,
        'unavailable_policy = "block"',
        'unavailable_policy = "fallback"',
    )

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "fallback" in result.stderr.lower()


def test_check_ids_must_be_valid(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(config_path, "[checks.pytest]", '[checks."Bad ID"]')

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "check" in result.stderr.lower()


def test_unknown_check_prerequisite_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path, "prerequisites = []", 'prerequisites = ["missing"]'
    )

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "missing" in result.stderr.lower()


def test_cyclic_check_prerequisites_are_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_section_value(
        config_path, "checks.pytest", "prerequisites = []", 'prerequisites = ["mypy"]'
    )
    replace_section_value(
        config_path,
        "checks.mypy",
        "prerequisites = []",
        'prerequisites = ["pytest"]',
    )

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "cycle" in result.stderr.lower()


def test_run_overrides_use_configured_fields_and_bounds(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)

    accepted = run_config(
        "validate",
        str(config_path),
        "--overrides",
        '{"limits.review.max_repair_rounds_per_level":1}',
    )
    rejected = run_config(
        "validate",
        str(config_path),
        "--overrides",
        '{"limits.review.max_repair_rounds_per_level":3}',
    )
    unknown = run_config(
        "validate",
        str(config_path),
        "--overrides",
        '{"roles.worker.model":"other-model"}',
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "bound" in rejected.stderr.lower()
    assert unknown.returncode != 0
    assert "allowed" in unknown.stderr.lower()


def test_invalid_fallback_reference_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path,
        'unavailable_policy = "block"',
        'unavailable_policy = "fallback"',
    )
    replace_config_text(
        config_path, "fallback_roles = []", 'fallback_roles = ["absent"]'
    )

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "fallback" in result.stderr.lower()


def test_configured_fallback_is_accepted(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path,
        'unavailable_policy = "block"',
        'unavailable_policy = "fallback"',
    )
    replace_config_text(
        config_path, "fallback_roles = []", 'fallback_roles = ["worker"]'
    )

    result = run_config("validate", str(config_path))

    assert result.returncode == 0, result.stderr


def test_fallback_cycle_is_rejected(tmp_path: Path) -> None:
    project_root = tmp_path / "consumer"
    project_root.mkdir()
    config_path = create_config(project_root)
    replace_config_text(
        config_path,
        'unavailable_policy = "block"',
        'unavailable_policy = "fallback"',
    )
    replace_config_text(
        config_path, "fallback_roles = []", 'fallback_roles = ["worker"]'
    )
    worker_section = config_path.read_text(encoding="utf-8").split("[roles.worker]", 1)
    before, worker_config = worker_section
    worker_config = worker_config.replace(
        'unavailable_policy = "block"', 'unavailable_policy = "fallback"', 1
    )
    worker_config = worker_config.replace(
        "fallback_roles = []", 'fallback_roles = ["manager"]', 1
    )
    config_path.write_text(before + "[roles.worker]" + worker_config, encoding="utf-8")

    result = run_config("validate", str(config_path))

    assert result.returncode != 0
    assert "cycle" in result.stderr.lower()
